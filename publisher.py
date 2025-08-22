# -*- coding: utf-8 -*-
import json
import asyncio
import logging
from typing import List, Tuple, Dict

from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError

from database import get_unsent_drafts, mark_sent
from config import (
    SOURCE_CHAT_ID, TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID,
    PAUSE, DB_FILE
)

logger = logging.getLogger(__name__)

def get_active_targets(active_backup: bool) -> List[int]:
    targets = [TARGET_CHAT_ID]
    if active_backup and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

async def _safe_sleep(seconds: float):
    try:
        await asyncio.sleep(max(0.0, seconds))
    except Exception:
        pass

async def _send_with_backoff(func_coro_factory, *, base_pause: float):
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            await _safe_sleep(base_pause)
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                wait = 3
            await _safe_sleep(wait + 1.0)
            tries += 1
        except (TimedOut, NetworkError):
            await _safe_sleep(3.0)
            tries += 1
        except TelegramError as e:
            if "Flood control exceeded" in str(e):
                await _safe_sleep(5.0)
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

def _poll_payload_from_raw(raw: dict):
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
    else:
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

async def publicar_rows(context, *, rows: List[Tuple[int, str, str]], targets: List[int], mark_as_sent: bool):
    publicados = 0
    fallidos = 0
    enviados_ids: List[int] = []

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
                ok, _msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)
            else:
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, _msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)

            if ok:
                any_success = True

        if any_success:
            publicados += 1
            if mark_as_sent:
                enviados_ids.append(mid)
        else:
            fallidos += 1

    if enviados_ids and mark_as_sent:
        mark_sent(DB_FILE, enviados_ids)

    return publicados, fallidos

async def publicar_todo(context, *, active_backup: bool, mark_as_sent: bool):
    targets = get_active_targets(active_backup)
    all_rows = get_unsent_drafts(DB_FILE)
    if not all_rows:
        return 0, 0
    return await publicar_rows(context, rows=all_rows, targets=targets, mark_as_sent=mark_as_sent)

async def publicar_ids(context, *, ids: List[int], active_backup: bool, mark_as_sent: bool):
    # Este módulo asume que main/scheduler ya filtran rows por ids.
    from database import _conn  # evitar ciclo de import
    con = _conn(DB_FILE)
    placeholders = ",".join("?" for _ in ids)
    cur = con.execute(
        f"SELECT message_id, snippet, raw_json FROM drafts "
        f"WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders}) "
        f"ORDER BY created_at ASC",
        ids
    )
    rows = list(cur.fetchall())
    targets = get_active_targets(active_backup)
    if not rows:
        return 0, 0
    return await publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)
