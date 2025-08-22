# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError

from config import SOURCE_CHAT_ID, TZ, TZNAME

logger = logging.getLogger(__name__)

# ---------------------------
# Pequeñas utilidades comunes
# ---------------------------

async def safe_sleep(seconds: float):
    try:
        await asyncio.sleep(max(0.0, seconds))
    except Exception:
        pass


async def send_with_backoff(func_coro_factory, *, base_pause: float):
    """
    Ejecuta la corrutina de envío con control de flood y reintentos.
    `func_coro_factory` debe ser un lambda que, al llamarse, devuelva la corrutina de envío.
    Devuelve (ok: bool, result: Message|None)
    """
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            await safe_sleep(base_pause)
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 3
            logger.warning(f"RetryAfter: esperando {wait}s …")
            await safe_sleep(wait + 1.0)
            tries += 1
        except TimedOut:
            logger.warning("TimedOut: esperando 3s …")
            await safe_sleep(3.0)
            tries += 1
        except NetworkError:
            logger.warning("NetworkError: esperando 3s …")
            await safe_sleep(3.0)
            tries += 1
        except TelegramError as e:
            if "Flood control exceeded" in str(e):
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 5
                logger.warning(f"Flood control: esperando {wait}s …")
                await safe_sleep(wait + 1.0)
                tries += 1
            else:
                logger.error(f"TelegramError no recuperable: {e}")
                return False, None
        except Exception as e:
            logger.exception(f"Error enviando: {e}")
            return False, None

        if tries >= 5:
            logger.error("Demasiados reintentos; abandono este mensaje.")
            return False, None


async def temp_notice(context, text: str, ttl: int = 6):
    """Envía un aviso temporal al canal de borradores y lo borra tras `ttl` s."""
    try:
        m = await context.bot.send_message(SOURCE_CHAT_ID, text, disable_notification=True)
    except Exception:
        return
    async def _auto_del():
        await safe_sleep(ttl)
        try:
            await context.bot.delete_message(SOURCE_CHAT_ID, m.message_id)
        except Exception:
            pass
    asyncio.create_task(_auto_del())


def extract_id_from_text(txt: str) -> Optional[int]:
    parts = (txt or "").split()
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
        if p.lower().startswith("id:"):
            n = p.split(":", 1)[1]
            if n.isdigit():
                return int(n)
    return None


def deep_link_for_channel_message(chat_id: int, mid: int) -> str:
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"


def is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))


def human_eta(target_dt, now=None) -> str:
    """Texto corto tipo 'en 27 min' / 'en 1 h 15 m' / 'en 2 d 3 h'."""
    from datetime import datetime
    now = now or datetime.now(tz=TZ)
    sec = max(0, int((target_dt - now).total_seconds()))
    mins = sec // 60
    if mins < 60:
        return f"en {mins} min"
    hours = mins // 60
    mins = mins % 60
    if hours < 24:
        if mins:
            return f"en {hours} h {mins} m"
        return f"en {hours} h"
    days = hours // 24
    hours = hours % 24
    if hours:
        return f"en {days} d {hours} h"
    return f"en {days} d"


def parse_nuke_selection(arg: str, drafts: List[Tuple[int, str]]) -> Set[int]:
    arg = (arg or "").strip().lower()
    ids_in_order = [did for (did, _snip) in drafts]
    result: Set[int] = set()

    if not arg:
        return result
    if arg in ("all", "todos"):
        result.update(ids_in_order)
        return result
    if arg.isdigit():
        n = int(arg)
        if n > 0:
            result.update(ids_in_order[-n:])
        return result

    # Acepta "1,2,3" y "1, 2, 3" (con o sin espacios)
    pieces = [p.strip() for p in arg.split(",") if p.strip()]
    for p in pieces:
        if re.fullmatch(r"\d+-\d+", p):
            a, b = p.split("-")
            a, b = int(a), int(b)
            if a <= 0 or b <= 0:
                continue
            lo, hi = min(a, b), max(a, b)
            for pos in range(lo, hi + 1):
                idx = pos - 1
                if 0 <= idx < len(ids_in_order):
                    result.add(ids_in_order[idx])
        elif p.isdigit():
            pos = int(p)
            idx = pos - 1
            if 0 <= idx < len(ids_in_order):
                result.add(ids_in_order[idx])
    return result


def poll_payload_from_raw(raw: dict) -> Tuple[dict, bool]:
    """
    Extrae un payload *base* listo para send_poll a partir de raw['poll'] (sin chat_id).
    Devuelve (kwargs_parciales, is_quiz).
    """
    p = (raw or {}).get("poll") or {}
    question = p.get("question", "Pregunta")
    options_src = p.get("options", []) or []
    options = [o.get("text", "") for o in options_src]

    is_anon = p.get("is_anonymous", True)
    allows_multiple = p.get("allows_multiple_answers", False)
    ptype = (p.get("type") or "regular").lower().strip()
    is_quiz = (ptype == "quiz")

    kwargs = dict(question=question, options=options, is_anonymous=is_anon)

    if not is_quiz:
        kwargs["allows_multiple_answers"] = bool(allows_multiple)

    if is_quiz:
        kwargs["type"] = "quiz"
        cid = p.get("correct_option_id")
        try:
            cid = int(cid) if cid is not None else None
        except Exception:
            cid = None
        if cid is None or cid < 0 or cid >= len(options):
            cid = 0
        kwargs["correct_option_id"] = cid

    if p.get("open_period") is not None and p.get("close_date") is None:
        try:
            kwargs["open_period"] = int(p["open_period"])
        except Exception:
            pass
    elif p.get("close_date") is not None:
        try:
            kwargs["close_date"] = int(p["close_date"])
        except Exception:
            pass

    if is_quiz and p.get("explanation"):
        kwargs["explanation"] = str(p["explanation"])

    return kwargs, is_quiz


async def delete_command_message(update, context, delay: int = 2):
    """Borra el mensaje del comando en el canal (deja la confirmación)."""
    try:
        await safe_sleep(delay)
        if update and update.channel_post:
            await context.bot.delete_message(update.channel_post.chat_id, update.channel_post.message_id)
    except Exception:
        pass
