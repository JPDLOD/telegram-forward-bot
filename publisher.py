# -*- coding: utf-8 -*-
"""
publisher.py
-------------
Funciones de publicación y estado compartido para targets.
"""

from typing import List, Dict, Tuple, Set
import json
import logging

from telegram.error import TelegramError, RetryAfter, TimedOut, NetworkError

from config import (
    SOURCE_CHAT_ID,
    TARGET_CHAT_ID,
    BACKUP_CHAT_ID,
    PAUSE,
    DB_FILE,
)
from database import get_unsent_drafts, mark_sent

logger = logging.getLogger(__name__)

# ========= ESTADO COMPARTIDO =========
ACTIVE_BACKUP: bool = True  # única fuente de la verdad
STATS = {"cancelados": 0, "eliminados": 0}
LAST_BATCH: Dict[int, List[int]] = {}        # {chat_id_destino: [message_ids publicados allá]}
SCHEDULED_LOCK: Set[int] = set()             # ids bloqueados por programación


# ---------- helpers de encuestas ----------
def _poll_payload_from_raw(raw: dict) -> Tuple[dict, bool]:
    p = raw.get("poll") or {}
    question = p.get("question", "Pregunta")
    options_src = p.get("options", []) or []
    options = [o.get("text", "") for o in options_src]

    is_anon = p.get("is_anonymous", True)
    allows_multiple = p.get("allows_multiple_answers", False)
    ptype = (p.get("type") or "regular").lower().strip()
    is_quiz = (ptype == "quiz")

    kwargs = dict(
        question=question,
        options=options,
        is_anonymous=is_anon,
    )

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


# ========= TARGETS =========
def get_active_targets() -> List[int]:
    targets = [TARGET_CHAT_ID]
    if ACTIVE_BACKUP and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

def is_backup_on() -> bool:
    return ACTIVE_BACKUP

def set_active_backup(on: bool) -> bool:
    global ACTIVE_BACKUP
    ACTIVE_BACKUP = bool(on)
    return ACTIVE_BACKUP

def toggle_active_backup() -> bool:
    global ACTIVE_BACKUP
    ACTIVE_BACKUP = not ACTIVE_BACKUP
    return ACTIVE_BACKUP


# ========= utilidades internas =========
async def _safe_sleep(seconds: float, asyncio_mod):
    try:
        await asyncio_mod.sleep(max(0.0, seconds))
    except Exception:
        pass


async def _send_with_backoff(func_coro_factory, base_pause: float, asyncio_mod):
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            await _safe_sleep(base_pause, asyncio_mod)
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                import re
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 3
            await _safe_sleep(wait + 1.0, asyncio_mod)
            tries += 1
        except (TimedOut, NetworkError):
            await _safe_sleep(3.0, asyncio_mod)
            tries += 1
        except TelegramError as e:
            if "Flood control exceeded" in str(e):
                import re
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 5
                await _safe_sleep(wait + 1.0, asyncio_mod)
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


# ========= publicación =========
async def publicar_rows(context, rows: List[Tuple[int, str, str]], targets: List[int], mark_as_sent: bool, asyncio_mod) -> Tuple[int, int, Dict[int, List[int]]]:
    publicados = 0
    fallidos = 0
    enviados_ids: List[int] = []
    posted_by_target: Dict[int, List[int]] = {t: [] for t in targets}

    for mid, _t, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        any_success = False
        for dest in targets:
            if "poll" in data:
                base_kwargs, _ = _poll_payload_from_raw(data)
                kwargs = dict(base_kwargs)
                kwargs["chat_id"] = dest
                coro_factory = lambda k=kwargs: context.bot.send_poll(**k)
                ok, msg = await _send_with_backoff(coro_factory, PAUSE, asyncio_mod)
            else:
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, msg = await _send_with_backoff(coro_factory, PAUSE, asyncio_mod)

            if ok:
                any_success = True
                if msg and getattr(msg, "message_id", None):
                    posted_by_target[dest].append(msg.message_id)

        if any_success:
            publicados += 1
            if mark_as_sent:
                enviados_ids.append(mid)
        else:
            fallidos += 1

    if enviados_ids and mark_as_sent:
        mark_sent(DB_FILE, enviados_ids)

    return publicados, fallidos, posted_by_target


async def publicar(context, asyncio_mod):
    """Publica todo lo pendiente EXCLUYENDO bloqueados por programación."""
    all_rows = get_unsent_drafts(DB_FILE)
    if not all_rows:
        return 0, 0, {t: [] for t in get_active_targets()}
    rows = [(m, t, r) for (m, t, r) in all_rows if m not in SCHEDULED_LOCK]
    if not rows:
        return 0, 0, {t: [] for t in get_active_targets()}
    return await publicar_rows(context, rows, get_active_targets(), True, asyncio_mod)


async def publicar_ids(context, ids: List[int], asyncio_mod, mark_as_sent: bool = True):
    if not ids:
        return 0, 0, {t: [] for t in get_active_targets()}
    # filtramos desde DB para preservar orden y estado
    from database import _conn  # uso interno para query simple
    import sqlite3
    try:
        con = _conn(DB_FILE)
        ph = ",".join("?" for _ in ids)
        cur = con.execute(
            f"SELECT message_id, snippet, raw_json FROM drafts "
            f"WHERE sent=0 AND deleted=0 AND message_id IN ({ph}) "
            f"ORDER BY created_at ASC",
            ids
        )
        rows = list(cur.fetchall())
    except sqlite3.Error:
        rows = []
    return await publicar_rows(context, rows, get_active_targets(), mark_as_sent, asyncio_mod)
