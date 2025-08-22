# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError

from config import (
    BOT_TOKEN, DB_FILE, TZNAME, TZ,
    SOURCE_CHAT_ID, TARGET_CHAT_ID, PREVIEW_CHAT_ID
)
from database import (
    init_db, save_draft, get_unsent_drafts, list_drafts,
    mark_deleted, restore_draft
)
from keyboards import kb_main, text_main, kb_settings, text_settings
from publisher import (
    publicar_todo_activos, publicar_ids, publicar, get_active_targets,
    STATS, SCHEDULED_LOCK, set_active_backup, is_active_backup
)
from utils import temp_notice, extract_id_from_text, deep_link_for_channel_message, parse_nuke_selection

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
def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))

# -------------------------------------------------------
# Comandos
# -------------------------------------------------------
async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    """Lista borradores (excluyendo programados) y al final muestra programaciones pendientes."""
    from scheduler import SCHEDULES  # lectura
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

    # Programaciones
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

async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    SCHEDULED_LOCK.discard(mid)
    STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context.bot, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)

async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
    except TelegramError as e:
        ok_del = False
        logger.warning(f"No pude borrar en el canal id:{mid} → {e}")

    # Borrado real de la DB
    try:
        import sqlite3
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("DELETE FROM drafts WHERE message_id = ?", (mid,))
        con.commit()
        con.close()
    except Exception:
        pass

    SCHEDULED_LOCK.discard(mid)
    STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await temp_notice(context.bot, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)

async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    from database import get_last_deleted
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await temp_notice(context.bot, "ℹ️ No hay nada para deshacer.", ttl=5)
        return

    restore_draft(DB_FILE, mid)
    if STATS["cancelados"] > 0:
        STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context.bot, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)

async def _cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    rows_full = get_unsent_drafts(DB_FILE)
    rows = [(m, t, r) for (m, t, r) in rows_full if m not in SCHEDULED_LOCK]
    if not rows:
        await temp_notice(context.bot, "🧪 Preview: 0 mensajes.", ttl=4)
        return
    pubs, fails, _ = await publicar_ids(
        context, ids=[m for (m, _t, _r) in rows], targets=[PREVIEW_CHAT_ID], mark_as_sent=False
    )
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
    # Refrescar panel
    await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

# -------------------------------------------------------
# Menús / botones (callbacks)
# -------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from scheduler import schedule_ids, cmd_programados, cmd_desprogramar
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
            from keyboards import kb_schedule, text_schedule
            await q.edit_message_text(text_schedule(), reply_markup=kb_schedule())

        elif data == "m:settings":
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

        elif data == "m:toggle_backup":
            set_active_backup(not is_active_backup())
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

        elif data == "m:back":
            await q.edit_message_text(text_main(), reply_markup=kb_main())

        # Programación rápida
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
                from keyboards import kb_schedule
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
# Handler principal del canal (BORRADOR)
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from scheduler import cmd_programar, cmd_programados, cmd_desprogramar
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    txt = (msg.text or "").strip()

    # --------- COMANDOS ----------
    if _is_command_text(txt):
        low = txt.lower()

        if low.startswith("/listar") or low.startswith("/lista"):
            await _cmd_listar(context);  return

        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt);  return

        if low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt);  return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt);  return

        if low.startswith("/nuke") or low.strip() in ("/all", "/todos"):
            parts = (txt or "").split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ("all" if low.strip() in ("/all", "/todos") else "")
            drafts = list_drafts(DB_FILE)
            victims = parse_nuke_selection(arg, drafts)
            if not drafts:
                await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.");  return
            if not victims:
                await context.bot.send_message(
                    SOURCE_CHAT_ID,
                    "Usa: /nuke all | /nuke todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N"
                );  return
            borrados = 0
            import sqlite3
            for mid in sorted(victims, reverse=True):
                try:
                    await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
                except TelegramError:
                    pass
                con = sqlite3.connect(DB_FILE)
                cur = con.cursor()
                cur.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
                con.commit(); con.close()
                SCHEDULED_LOCK.discard(mid)
                borrados += 1
            STATS["eliminados"] += borrados
            restantes = len(list_drafts(DB_FILE))
            await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")
            return

        if low.startswith("/enviar"):
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
            return

        if low.startswith("/preview"):
            await _cmd_preview(context);  return

        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await cmd_programar(context, when_str)
            else:
                await context.bot.send_message(
                    SOURCE_CHAT_ID,
                    "Usa: `/programar YYYY-MM-DD HH:MM` (24h: 00:00–23:59, sin '(24h)' ni AM/PM).",
                    parse_mode="Markdown"
                )
            return

        if low.startswith("/programados"):
            await cmd_programados(context);  return

        if low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await cmd_desprogramar(context, arg);  return

        if low.startswith("/id"):
            if msg.reply_to_message and len((txt or "").split()) == 1:
                rid = msg.reply_to_message.message_id
                await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
            else:
                mid = extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
                if not mid:
                    await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
                else:
                    mid = int(mid)
                    link = deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
                    await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 {mid}\n• Enlace: {link}")
            return

        if low.startswith(("/canales", "/targets", "/where")):
            await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
            return

        if low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await _cmd_backup(context, arg);  return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main());  return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        return

    # --------- NO COMANDO → GUARDAR BORRADOR ----------
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info(f"Guardado en borrador: {msg.message_id}")

# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
