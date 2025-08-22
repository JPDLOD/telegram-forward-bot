# -*- coding: utf-8 -*-
import json
import logging
from typing import List, Tuple, Dict

from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError
from telegram.ext import ContextTypes

from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PAUSE, SOURCE_CHAT_ID, DB_FILE
from database import get_unsent_drafts, mark_sent

logger = logging.getLogger(__name__)

# ===== Estado de backup (runtime) =====
ACTIVE_BACKUP: bool = True

def is_active_backup() -> bool:
    """Devuelve el estado actual del backup (True/False)."""
    return ACTIVE_BACKUP

def set_active_backup(value: bool) -> None:
    """Actualiza el estado global del backup de forma segura."""
    global ACTIVE_BACKUP
    ACTIVE_BACKUP = bool(value)

def get_active_targets() -> List[int]:
    """Targets activos según el estado de backup."""
    targets = [TARGET_CHAT_ID]
    if is_active_backup() and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

# ===== Helpers internos de envío =====
async def _safe_sleep(context, seconds: float):
    # Este helper está aquí para mantener compatibilidad; el sleep real lo maneja el caller si lo necesita
    pass

async def _send_with_backoff(func_coro_factory, *, base_pause: float):
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            # La pausa la maneja el caller de publisher si lo requiere; aquí no dormimos
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                wait = 3
            logging.warning(f"RetryAfter: {wait}s …")
            import asyncio
            await asyncio.sleep(wait + 1.0)
            tries += 1
        except TimedOut:
            logging.warning("TimedOut: 3s …")
            import asyncio
            await asyncio.sleep(3.0)
            tries += 1
        except NetworkError:
            logging.warning("NetworkError: 3s …")
            import asyncio
            await asyncio.sleep(3.0)
            tries += 1
        except TelegramError as e:
            if "Flood control exceeded" in str(e):
                import asyncio
                await asyncio.sleep(5.0)
                tries += 1
            else:
                logging.error(f"TelegramError no recuperable: {e}")
                return False, None
        except Exception as e:
            logging.exception(f"Error enviando: {e}")
            return False, None

        if tries >= 5:
            logging.error("Demasiados reintentos; abandono este mensaje.")
            return False, None

# ===== Publicadores =====
async def _poll_payload_from_raw(raw: dict):
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

async def _publicar_rows(context: ContextTypes.DEFAULT_TYPE, *, rows: List[Tuple[int, str, str]],
                         targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
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
                base_kwargs, _ = await _poll_payload_from_raw(data)
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

async def publicar(context: ContextTypes.DEFAULT_TYPE, *, targets: List[int], mark_as_sent: bool):
    """Publica todo lo pendiente (sin los bloqueados; eso lo maneja el caller)."""
    rows = get_unsent_drafts(DB_FILE)
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)

async def publicar_ids(context: ContextTypes.DEFAULT_TYPE, *, ids: List[int],
                       targets: List[int], mark_as_sent: bool):
    # En esta versión modular, main/scheduler ya filtran los rows específicos
    from database import _conn  # usar acceso directo para query puntual sin duplicar lógica pública
    c = _conn(DB_FILE)
    if not ids:
        return 0, 0, {t: [] for t in targets}
    placeholders = ",".join("?" for _ in ids)
    q = f"SELECT message_id, snippet, raw_json FROM drafts WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders}) ORDER BY message_id ASC"
    rows = list(c.execute(q, ids).fetchall())
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)
