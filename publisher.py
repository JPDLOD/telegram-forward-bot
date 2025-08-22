# -*- coding: utf-8 -*-
import json
import logging
import sqlite3
from typing import Dict, List, Set, Tuple

from telegram.error import TelegramError

from config import (
    DB_FILE, SOURCE_CHAT_ID, TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID,
    PAUSE
)
from database import get_unsent_drafts, mark_sent, list_drafts
from core_utils import send_with_backoff, poll_payload_from_raw

logger = logging.getLogger(__name__)

# ======= ESTADO PÚBLICO =======
ACTIVE_BACKUP: bool = True                           # se puede alternar desde /backup o Ajustes
STATS: Dict[str, int] = {"cancelados": 0, "eliminados": 0}
LAST_BATCH: Dict[int, List[int]] = {}                # {chat_id_destino: [message_ids]}
SCHEDULED_LOCK: Set[int] = set()                     # IDs bloqueados por programación

# ======= utils DB locales =======
def _get_rows_by_ids(ids: List[int]) -> List[Tuple[int, str, str]]:
    """Devuelve [(message_id, snippet, raw_json)] para esos IDs que siguen pendientes."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = f"""SELECT message_id, snippet, raw_json
              FROM drafts
              WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders})
              ORDER BY message_id ASC"""
    con = sqlite3.connect(DB_FILE)
    try:
        cur = con.execute(sql, ids)
        return list(cur.fetchall())
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_active_targets() -> List[int]:
    targets = [TARGET_CHAT_ID]
    if ACTIVE_BACKUP and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets


# ======= núcleo de publicación =======
async def _publicar_rows(context, *, rows: List[Tuple[int, str, str]], targets: List[int], mark_as_sent: bool):
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
                coro_factory = lambda k=kwargs: context.bot.send_poll(**k)
                ok, msg = await send_with_backoff(coro_factory, base_pause=PAUSE)
            else:
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, msg = await send_with_backoff(coro_factory, base_pause=PAUSE)

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


async def publicar(context, *, mark_as_sent: bool = True):
    """Envía la cola completa EXCLUYENDO los bloqueados (programados)."""
    all_rows = get_unsent_drafts(DB_FILE)  # [(message_id, snippet, raw_json)]
    if not all_rows:
        return 0, 0, {t: [] for t in get_active_targets()}
    rows = [(m, t, r) for (m, t, r) in all_rows if m not in SCHEDULED_LOCK]
    if not rows:
        return 0, 0, {t: [] for t in get_active_targets()}
    pubs, fails, posted = await _publicar_rows(
        context, rows=rows, targets=get_active_targets(), mark_as_sent=mark_as_sent
    )
    return pubs, fails, posted


async def publicar_ids(context, *, ids: List[int], mark_as_sent: bool = True):
    rows = _get_rows_by_ids(ids)
    if not rows:
        return 0, 0, {t: [] for t in get_active_targets()}
    return await _publicar_rows(
        context, rows=rows, targets=get_active_targets(), mark_as_sent=mark_as_sent
    )


async def publicar_todo_activos(context):
    pubs, fails, posted = await publicar(context, mark_as_sent=True)
    global LAST_BATCH
    LAST_BATCH = posted
    return pubs, fails
