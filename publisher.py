# -*- coding: utf-8 -*-
import json
from typing import Tuple, List, Dict

from telegram.ext import ContextTypes

from database import get_unsent_drafts, mark_sent
from config import SOURCE_CHAT_ID, TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID, PAUSE
from core_utils import send_with_backoff, poll_payload_from_raw, get_rows_by_ids

# Estado compartido
ACTIVE_BACKUP = True         # principal siempre ON fijo; esto controla si se suma BACKUP
STATS = {"cancelados": 0, "eliminados": 0}
LAST_BATCH: Dict[int, List[int]] = {}  # para posibles futuras funciones
SCHEDULED_LOCK = set()       # IDs bloqueados por programaciones pendientes (no se mezclan)

def get_active_targets() -> List[int]:
    t = [TARGET_CHAT_ID]
    if ACTIVE_BACKUP and BACKUP_CHAT_ID:
        t.append(BACKUP_CHAT_ID)
    return t


async def _publicar_rows(context: ContextTypes.DEFAULT_TYPE, *, rows: List[tuple], targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
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
                base_kwargs, _ = poll_payload_from_raw(data)
                kwargs = dict(base_kwargs)
                kwargs["chat_id"] = dest
                ok, msg = await send_with_backoff(lambda k=kwargs: context.bot.send_poll(**k), base_pause=PAUSE)
            else:
                ok, msg = await send_with_backoff(
                    lambda d=dest, m=mid: context.bot.copy_message(chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m),
                    base_pause=PAUSE
                )
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


async def publicar(context: ContextTypes.DEFAULT_TYPE, *, targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    """Envía la cola completa EXCLUYENDO bloqueados por programación."""
    all_rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not all_rows:
        return 0, 0, {t: [] for t in targets}
    rows = [(m, t, r) for (m, t, r) in all_rows if m not in SCHEDULED_LOCK]
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)


async def publicar_ids(context: ContextTypes.DEFAULT_TYPE, *, ids: List[int], targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    rows = get_rows_by_ids(ids)
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)
