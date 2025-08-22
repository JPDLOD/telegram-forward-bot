# -*- coding: utf-8 -*-
import re
import json
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional, Tuple, List

from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError
from telegram.ext import ContextTypes

from config import SOURCE_CHAT_ID, TZ, PAUSE, DB_FILE

logger = logging.getLogger(__name__)

# ---------- Utilidades BD locales (no tocan database.py) ----------
def hard_delete_draft(mid: int) -> None:
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("DELETE FROM drafts WHERE message_id = ?", (mid,))
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_draft_row(mid: int):
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute(
            "SELECT message_id, text, raw_json, sent, deleted, created_at "
            "FROM drafts WHERE message_id = ?",
            (mid,)
        )
        row = cur.fetchone()
        return row
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_rows_by_ids(ids: List[int]) -> List[Tuple[int, str, str]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT message_id, text, raw_json FROM drafts "
        f"WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders}) "
        f"ORDER BY created_at ASC"
    )
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute(sql, ids)
        return cur.fetchall()
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


# ---------- Encuestas ----------
def poll_payload_from_raw(raw: dict) -> Tuple[dict, bool]:
    p = raw.get("poll") or {}
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


# ---------- Utilidades varias ----------
async def safe_sleep(seconds: float):
    try:
        await asyncio.sleep(max(0.0, seconds))
    except Exception:
        pass


async def send_with_backoff(func_coro_factory, *, base_pause: float = PAUSE):
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
            await safe_sleep(wait + 1.0)
            tries += 1
        except (TimedOut, NetworkError):
            await safe_sleep(3.0)
            tries += 1
        except TelegramError as e:
            if "Flood control exceeded" in str(e):
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 5
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


async def temp_notice(context: ContextTypes.DEFAULT_TYPE, text: str, ttl: int = 6):
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


def human_eta(target_dt: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(tz=TZ)
    sec = max(0, int((target_dt - now).total_seconds()))
    mins = sec // 60
    if mins < 60:
        return f"en {mins} min"
    hours = mins // 60
    mins = mins % 60
    if hours < 24:
        return f"en {hours} h {mins} m" if mins else f"en {hours} h"
    days = hours // 24
    hours = hours % 24
    return f"en {days} d {hours} h" if hours else f"en {days} d"


def parse_programar_when(text: str) -> Optional[str]:
    """
    Extrae 'YYYY-MM-DD HH:MM' de un texto, permitiendo H:MM (1–2 dígitos).
    Devuelve una cadena normalizada 'YYYY-MM-DD HH:MM' o None si no matchea.
    """
    m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})', text)
    if not m:
        return None
    ymd, hh, mm = m.group(1), m.group(2), m.group(3)
    try:
        h = int(hh)
        if not (0 <= h <= 23):
            return None
    except Exception:
        return None
    return f"{ymd} {h:02d}:{mm}"


async def delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mid: int, delay: int = 4):
    async def _run():
        await safe_sleep(delay)
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    asyncio.create_task(_run())


def deep_link_for_channel_message(chat_id: int, mid: int) -> str:
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"
