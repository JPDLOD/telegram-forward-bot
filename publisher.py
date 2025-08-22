# -*- coding: utf-8 -*-
import json
import logging
from typing import List, Tuple, Dict, Set
from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError

from config import DB_FILE, SOURCE_CHAT_ID, TARGET_CHAT_ID, BACKUP_CHAT_ID, PAUSE
from database import get_unsent_drafts, mark_sent
from utils import safe_sleep

logger = logging.getLogger(__name__)

# Estado de targets
ACTIVE_BACKUP: bool = True  # por defecto ON al iniciar

def get_active_targets() -> List[int]:
    targets = [TARGET_CHAT_ID]
    if ACTIVE_BACKUP and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

# Contadores
STATS = {"cancelados": 0, "eliminados": 0}

# Lock de IDs programados (no se envían con /enviar ni /preview)
SCHEDULED_LOCK: Set[int] = set()

# ------------- envío con backoff -------------
async def _send_with_backoff(func_coro_factory, *, base_pause: float):
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            await safe_sleep(base_pause)
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                import re
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
                import re
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

# ------------- payload de encuestas -------------
def _poll_payload_from_raw(raw: dict):
    p = raw.get("poll") or {}
    question = p.get("question", "Pregunta")
    options_src = p.get("options", []) or []
    options  = [o.get("text", "") for o in options_src]

    is_anon  = p.get("is_anonymous", True)
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

# ------------- publicar -------------
async def publicar_rows(context, *, rows: List[Tuple[int, str, str]], targets: List[int], mark_as_sent: bool):
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
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)
            else:
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)

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

async def publicar(context, *, targets: List[int], mark_as_sent: bool):
    """Envía la cola completa EXCLUYENDO los bloqueados (SCHEDULED_LOCK)."""
    all_rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not all_rows:
        return 0, 0, {t: [] for t in targets}
    rows = [(m, t, r) for (m, t, r) in all_rows if m not in SCHEDULED_LOCK]
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)

async def publicar_ids(context, *, ids: List[int], targets: List[int], mark_as_sent: bool):
    from database import _conn  # para query ad-hoc
    if not ids:
        return 0, 0, {t: [] for t in targets}
    placeholders = ",".join("?" for _ in ids)
    sql = f"SELECT message_id, snippet, raw_json FROM drafts WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders}) ORDER BY message_id ASC"
    c = _conn(DB_FILE)
    cur = c.execute(sql, ids)
    rows = list(cur.fetchall())
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)

async def publicar_todo_activos(context):
    pubs, fails, _posted = await publicar(context, targets=get_active_targets(), mark_as_sent=True)
    return pubs, fails
