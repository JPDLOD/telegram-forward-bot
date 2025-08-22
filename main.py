# -*- coding: utf-8 -*-
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, CallbackQueryHandler, filters
from telegram.error import TelegramError

from config import (
    BOT_TOKEN, DB_FILE, TZNAME, TZ,
    SOURCE_CHAT_ID, TARGET_CHAT_ID, PREVIEW_CHAT_ID
)
from database import (
    init_db, save_draft, get_unsent_drafts, list_drafts,
    mark_deleted, restore_draft
)
from keyboards import kb_main, text_main, kb_settings, text_settings, kb_schedule, text_schedule
from publisher import (
    publicar_todo_activos, publicar_rows, publicar, get_active_targets,
    STATS, SCHEDULED_LOCK, set_active_backup, is_active_backup
)
from scheduler import schedule_ids, cmd_programar, cmd_programados, cmd_desprogramar, SCHEDULES
from utils import temp_notice, extract_id_from_text, deep_link_for_channel_message, parse_nuke_selection, safe_sleep

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DB =========
init_db(DB_FILE)
logger.info(
    f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  PRINCIPAL={TARGET_CHAT_ID}  "
    f"PREVIEW={PREVIEW_CHAT_ID}  TZ={TZNAME}"
)

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
async def _delete_user_command_if_possible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update and update.channel_post:
            await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=update.channel_post.message_id)
    except TelegramError:
        pass

def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))

# -------------------------------------------------------
# Comandos (solo muestro los que toco + backup; el resto es igual que ya tienes)
# -------------------------------------------------------
async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts_all = list_drafts(DB_FILE)
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
        from datetime import datetime as _dt
        now = _dt.now(tz=TZ)
        out.append("\n🗒 Programaciones pendientes:")
        for pid, rec in sorted(SCHEDULES.items()):
            when = rec["when"].astimezone(TZ).strftime("%Y-%m-%d %H:%M")
            ids = rec["ids"]
            out.append(f"• #{pid} — {when} ({TZNAME}) — {len(ids)} mensajes")

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))

async def _cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    rows_full = get_unsent_drafts(DB_FILE)
    rows = [(m, t, r) for (m, t, r) in rows_full if m not in SCHEDULED_LOCK]
    if not rows:
        await temp_notice(context.bot, "🧪 Preview: 0 mensajes.", ttl=4)
        return
    pubs, fails, _ = await publicar_rows(context, rows=rows, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

async def _cmd_backup(context: ContextTypes.DEFAULT_TYPE, arg: str):
    v = (arg or "").strip().lower()
    if v in ("on", "1", "true", "si", "sí"):
        set_active_backup(True)
    elif v in ("off", "0", "false", "no"):
        set_active_backup(False)
    else:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
        return
    await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

# ---------- NUKE helpers ----------
def _parse_nuke_selection_wrapper(arg: str):
    drafts = list_drafts(DB_FILE)
    from utils import parse_nuke_selection as _p
    return _p(arg, drafts), drafts

# (… aquí permanecen iguales _cmd_cancelar, _cmd_eliminar, _cmd_deshacer, _cmd_nuke, etc.)

# -------------------------------------------------------
# Callbacks (cambio en toggle_backup)
# -------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    try:
        if data == "m:list":
            await _cmd_listar(context)
        elif data == "m:send":
            await temp_notice(context.bot, "⏳ Procesando envío…", ttl=4)
            ok, fail = await publicar_todo_activos(context)
            extras = []
            if STATS["cancelados"]:
                extras.append(f"Cancelados: {STATS['cancelados']}")
            if STATS["eliminados"]:
                extras.append(f"Eliminados: {STATS['eliminados']}")
            msg_out = f"✅ Publicados {ok}."
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            STATS["cancelados"] = 0
            STATS["eliminados"] = 0
        elif data == "m:preview":
            await _cmd_preview(context)
        elif data == "m:sched":
            await q.edit_message_text(text_schedule(), reply_markup=kb_schedule())
        elif data == "m:settings":
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        elif data == "m:toggle_backup":
            set_active_backup(not is_active_backup())
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        elif data == "m:back":
            await q.edit_message_text(text_main(), reply_markup=kb_main())
        # (resto de callbacks de programación rápida igual que ya tienes…)
        elif data.startswith("s:"):
            now = datetime.now(tz=TZ)
            when = None
            if data == "s:+5":
                when = now + timedelta(minutes=5)
            elif data == "s:+15":
                when = now + timedelta(minutes=15)
            elif data == "s:today20":
                when = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if when <= now:
                    when = when + timedelta(days=1)
            elif data == "s:tom07":
                when = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            elif data == "s:list":
                await cmd_programados(context)
            elif data == "s:clear":
                await cmd_desprogramar(context, "all")
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual (24h):\n"
                    "`/programar YYYY-MM-DD HH:MM`\n"
                    "Ejemplos: `/programar 2025-08-22 09:30`, `/programar 2025-08-22 21:45`.\n"
                    "No pongas '(24h)' ni AM/PM.\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown", reply_markup=kb_schedule()
                )
            if when:
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await temp_notice(context.bot, "📭 No hay borradores para programar.", ttl=6)
                else:
                    await schedule_ids(context, when, ids)
    except Exception as e:
        logger.exception(f"Error en callback: {e}")

# -------------------------------------------------------
# Handler del canal (resto igual que ya tienes, sin tocar lógica)
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return
    txt = (msg.text or "").strip()
    # (… aquí mantén tus handlers de comandos y guardado de borradores tal cual)

    # --------- NO COMANDO → GUARDAR BORRADOR ----------
    if not _is_command_text(txt):
        snippet = msg.text or msg.caption or ""
        raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
        save_draft(DB_FILE, msg.message_id, snippet, raw_json)
        logger.info(f"Guardado en borrador: {msg.message_id}")
        return

    # (si es comando, tu bloque grande existente va aquí sin cambios)

# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)

# ========= set bot commands =========
async def _set_bot_commands(app: Application):
    try:
        await app.bot.set_my_commands([
            ("comandos", "Ver ayuda y botones"),
            ("listar", "Mostrar borradores pendientes (excluye programados)"),
            ("enviar", "Publicar ahora a targets activos"),
            ("preview", "Enviar cola a PREVIEW (no marca enviada)"),
            ("programar", "Programar (24h: YYYY-MM-DD HH:MM)"),
            ("programados", "Ver programaciones pendientes"),
            ("desprogramar", "Cancelar una programación (id|all)"),
            ("cancelar", "Quitar de la cola (no borra del canal)"),
            ("deshacer", "Revertir el último /cancelar"),
            ("eliminar", "Borrar del canal y de la cola"),
            ("nuke", "Borrar varios (all | 1,3,5 | 1-10 | N)"),
            ("id", "Mostrar ID del mensaje"),
            ("canales", "Ver IDs y estado de targets"),
            ("backup", "ON/OFF para backup"),
        ])
    except Exception:
        pass

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(on_error)
    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.post_init = _set_bot_commands
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
