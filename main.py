# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError

from config import BOT_TOKEN, DB_FILE, TZ, TZNAME, SOURCE_CHAT_ID, PREVIEW_CHAT_ID
from database import init_db, save_draft, list_drafts, mark_deleted, restore_draft
from keyboards import kb_main, text_main, kb_settings, text_settings
from core_utils import temp_notice, deep_link_for_channel_message, parse_nuke_selection, human_eta, is_command_text
from scheduler import schedule_ids, cmd_programados as _cmd_programados, cmd_desprogramar as _cmd_desprogramar, SCHEDULES
from publisher import (
    publicar_todo_activos, publicar_ids, get_active_targets,
    STATS, SCHEDULED_LOCK, set_active_backup, is_active_backup
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

init_db(DB_FILE)

# ===== Helper LOCAL (robusto y sin dependencias) =====
def __extract_id_from_text_local(txt: Optional[str]) -> Optional[int]:
    """
    Soporta:
      /cancelar 12345
      /cancelar id:12345
      /deshacer 12345
      /deshacer id:12345
    """
    if not txt:
        return None
    parts = txt.strip().split()
    # buscar un dígito puro después del comando
    for p in parts[1:]:
        if p.isdigit():
            try:
                return int(p)
            except Exception:
                pass
        if p.lower().startswith("id:"):
            val = p.split(":", 1)[1]
            if val.isdigit():
                try:
                    return int(val)
                except Exception:
                    pass
    return None

# ===== Comandos =====
async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts_all = list_drafts(DB_FILE)  # [(id, snip)]
    drafts = [(did, snip) for (did, snip) in drafts_all if did not in SCHEDULED_LOCK]

    if not drafts:
        out = ["📋 Borradores pendientes: 0"]
    else:
        out = ["📋 Borradores pendientes:"]
        for i, (did, snip) in enumerate(drafts, start=1):
            s = (snip or "").strip()
            if len(s) > 60:
                s = s[:60] + "…"
            out.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did})")

    if not SCHEDULES:
        out.append("\n🗒 Programaciones pendientes: 0")
    else:
        out.append("\n🗒 Programaciones pendientes:")
        for pid, rec in sorted(SCHEDULES.items()):
            when = rec["when"].astimezone(TZ).strftime("%Y-%m-%d %H:%M")
            ids = rec["ids"]
            out.append(f"• #{pid} — {when} ({TZNAME}) — {len(ids)} mensajes")

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))

async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """Quita de la cola sin borrar del canal. Acepta argumento o reply."""
    # 1) intentar por argumento
    mid = __extract_id_from_text_local(txt)
    # 2) si no viene, intentar por reply
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id

    if not mid:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ Usa: /cancelar <id> o responde con /cancelar al mensaje a cancelar."
        )
        return

    # marcar como cancelado (deleted=1) y quitar de locks de programación
    mark_deleted(DB_FILE, int(mid))
    SCHEDULED_LOCK.discard(int(mid))
    STATS["cancelados"] += 1

    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context.bot, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)

async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """BORRA del canal y lo quita de la cola definitivamente."""
    # (sin cambios)
    mid = __extract_id_from_text_local(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=int(mid))
    except TelegramError as e:
        ok_del = False
        logger.warning(f"No pude borrar en el canal id:{mid} → {e}")

    # Borrado real de la DB
    import sqlite3
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM drafts WHERE message_id = ?", (int(mid),))
    con.commit(); con.close()

    SCHEDULED_LOCK.discard(int(mid))
    STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await temp_notice(context.bot, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)

async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """Revierte un /cancelar (solo aplica a cancelados, no a eliminados)."""
    # 1) intentar por argumento
    mid = __extract_id_from_text_local(txt)
    # 2) si no viene, intentar por reply
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    # 3) si tampoco, tomar el último cancelado
    if not mid:
        from database import get_last_deleted
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await temp_notice(context.bot, "ℹ️ No hay nada para deshacer.", ttl=5)
        return

    restore_draft(DB_FILE, int(mid))
    SCHEDULED_LOCK.discard(int(mid))  # por si estaba programado
    if STATS["cancelados"] > 0:
        STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context.bot, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)

async def _cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    # (sin cambios)
    import sqlite3
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    rows_full = list(cur.execute(
        "SELECT message_id, snippet, raw_json FROM drafts WHERE sent=0 AND deleted=0 ORDER BY message_id ASC"
    ).fetchall())
    con.close()
    rows = [(m, t, r) for (m, t, r) in rows_full if m not in SCHEDULED_LOCK]
    if not rows:
        await temp_notice(context.bot, "🧪 Preview: 0 mensajes.", ttl=4);  return
    ids = [m for (m, _t, _r) in rows]
    pubs, fails, _ = await publicar_ids(context, ids=ids, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

# (el resto del archivo queda IGUAL: callbacks, handler del canal, main, etc.)
# Asegúrate de conservar tu contenido original a partir de aquí.
