# -*- coding: utf-8 -*-
import json
import logging
import sqlite3

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

from config import BOT_TOKEN, SOURCE_CHAT_ID, TZ, TZNAME
from database import init_db, save_draft, list_drafts, mark_deleted, restore_draft, get_unsent_drafts
from core_utils import (
    is_command_text, temp_notice, extract_id_from_text, delete_command_message,
    parse_nuke_selection, deep_link_for_channel_message
)
from publisher import (
    publicar_todo_activos, STATS, SCHEDULED_LOCK, LAST_BATCH,
    ACTIVE_BACKUP, get_active_targets
)
from scheduler import cmd_programar, cmd_programados, cmd_desprogramar, SCHEDULES
from ui import kb_main, text_main, handle_callback, cmd_listar, text_settings, kb_settings, kb_schedule, text_schedule

from config import DB_FILE

# =========================
# LOGGING + DB
# =========================
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

init_db(DB_FILE)
logger.info("SQLite listo. DB=%s TZ=%s", DB_FILE, TZNAME)

# =========================
# Comandos "manuales"
# =========================

async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    SCHEDULED_LOCK.discard(mid)
    STATS["cancelados"] = STATS.get("cancelados", 0) + 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)
    await delete_command_message(update, context)


async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    from telegram.error import TelegramError
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
    except TelegramError:
        ok_del = False

    # Borrado duro de la tabla
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute("DELETE FROM drafts WHERE message_id = ?", (mid,))
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass

    SCHEDULED_LOCK.discard(mid)
    STATS["eliminados"] = STATS.get("eliminados", 0) + 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await temp_notice(context, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)
    await delete_command_message(update, context)


async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    from database import get_last_deleted
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await temp_notice(context, "ℹ️ No hay nada para deshacer.", ttl=5)
        await delete_command_message(update, context)
        return

    restore_draft(DB_FILE, mid)
    if STATS.get("cancelados", 0) > 0:
        STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)
    await delete_command_message(update, context)


async def _cmd_nuke(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    parts = (txt or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""

    drafts = list_drafts(DB_FILE)
    if not drafts:
        await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.")
        await delete_command_message(update, context)
        return

    victims = parse_nuke_selection(arg, drafts)
    if not victims:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "Usa: /nuke all | /nuke todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N"
        )
        await delete_command_message(update, context)
        return

    borrados = 0
    from telegram.error import TelegramError
    for mid in sorted(victims, reverse=True):
        try:
            await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
        except TelegramError:
            pass
        try:
            con = sqlite3.connect(DB_FILE)
            con.execute("DELETE FROM drafts WHERE message_id = ?", (mid,))
            con.commit()
        finally:
            try:
                con.close()
            except Exception:
                pass
        SCHEDULED_LOCK.discard(mid)
        borrados += 1

    STATS["eliminados"] = STATS.get("eliminados", 0) + borrados
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")
    await delete_command_message(update, context)


async def _cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    # Si se responde a un mensaje, muestra su id
    if update.channel_post and update.channel_post.reply_to_message and len((txt or "").split()) == 1:
        rid = update.channel_post.reply_to_message.message_id
        await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
        await delete_command_message(update, context)
        return

    mid = extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
        await delete_command_message(update, context)
        return
    mid = int(mid)

    # Levanta info del mensaje en BD
    snippet = "[contenido]"
    tipo = "desconocido"
    fecha = ""
    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.execute("SELECT snippet, raw_json, created_at FROM drafts WHERE message_id=?", (mid,))
        row = cur.fetchone()
    finally:
        try:
            con.close()
        except Exception:
            pass

    if row:
        try:
            text, raw_json, created_at = row
            if text:
                snippet = (text.strip()[:180] + "…") if len(text.strip()) > 180 else text.strip()
            if raw_json:
                d = json.loads(raw_json)
                if "poll" in d:
                    tipo = "quiz" if (d.get("poll", {}).get("type") == "quiz") else "encuesta"
                elif "photo" in d:
                    tipo = "foto"
                elif "document" in d:
                    tipo = "documento"
                elif "video" in d:
                    tipo = "video"
                else:
                    tipo = "texto"
            if created_at:
                from datetime import datetime
                dt = datetime.fromtimestamp(int(created_at), tz=TZ)
                fecha = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    link = deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
    out = f"🆔 {mid}\n• Tipo: {tipo}\n• Snippet: {snippet}\n• Fecha: {fecha}\n• Enlace: {link}"
    await context.bot.send_message(SOURCE_CHAT_ID, out)
    await delete_command_message(update, context)


async def _send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main())


async def _send_full_help(context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🛠️ Comandos:\n"
        "• /listar — muestra borradores pendientes (y programaciones)\n"
        "• /cancelar <id> — o responde con /cancelar (no borra del canal)\n"
        "• /deshacer [id] — revierte un /cancelar (o responde)\n"
        "• /eliminar <id> — o responde (BORRA del canal y de la cola)  [alias: /del]\n"
        "• /nuke all|todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N (últimos)\n"
        "• /enviar — publica ahora (targets activos)\n"
        "• /preview — manda la cola a PREVIEW sin marcar como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa el envío (formato 24 h)\n"
        "• /programados — lista programaciones pendientes (con tiempo restante)\n"
        "• /desprogramar <id|all> — cancela una o todas las programaciones\n"
        "• /id [id] — info del mensaje o, si respondes, te dice el ID\n"
        "• /canales — IDs + estado de targets (alias: /targets, /where)\n"
        "• /backup on|off — alterna el backup\n"
    )
    await context.bot.send_message(SOURCE_CHAT_ID, txt, reply_markup=kb_main())


# =========================
# HANDLER DE CANAL
# =========================
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    txt = (msg.text or "").strip()

    # --------- COMANDOS ----------
    if is_command_text(txt):
        low = txt.lower().strip()

        if low.startswith("/listar"):
            await cmd_listar(context);  await delete_command_message(update, context);  return

        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt);  return

        if low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt);  return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt);  return

        if low.startswith("/nuke"):
            await _cmd_nuke(update, context, txt);  return
        if low.strip() in ("/all", "/todos"):
            await _cmd_nuke(update, context, "/nuke all");  return

        if low.startswith("/enviar"):
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
            ok, fail = await publicar_todo_activos(context)
            extras = []
            if STATS.get("cancelados"):
                extras.append(f"Cancelados: {STATS['cancelados']}")
            if STATS.get("eliminados"):
                extras.append(f"Eliminados: {STATS['eliminados']}")
            msg_out = f"✅ Publicados {ok}."
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            STATS["cancelados"] = 0
            STATS["eliminados"] = 0
            await delete_command_message(update, context)
            return

        if low.startswith("/preview"):
            # Preview simple
            from publisher import _publicar_rows
            rows_full = get_unsent_drafts(DB_FILE)
            pubs, fails, _ = await _publicar_rows(
                context, rows=rows_full, targets=[from config import PREVIEW_CHAT_ID], mark_as_sent=False
            )
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
            await delete_command_message(update, context)
            return

        if low.startswith("/programar"):
            parts = txt.split()
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await cmd_programar(context, when_str)
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /programar YYYY-MM-DD HH:MM  (formato 24 h)")
            await delete_command_message(update, context)
            return

        if low.startswith("/programados"):
            await cmd_programados(context);  await delete_command_message(update, context);  return

        if low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await cmd_desprogramar(context, arg);  await delete_command_message(update, context);  return

        if low.startswith("/id"):
            await _cmd_id(update, context, txt);  return

        if low.startswith(("/canales", "/targets", "/where")):
            await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
            await delete_command_message(update, context)
            return

        if low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            arg = (parts[1] if len(parts) > 1 else "").strip().lower()
            import publisher as _pub
            if arg in ("on", "1", "true", "si", "sí"):
                _pub.ACTIVE_BACKUP = True
            elif arg in ("off", "0", "false", "no"):
                _pub.ACTIVE_BACKUP = False
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
                await delete_command_message(update, context)
                return
            await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
            await delete_command_message(update, context)
            return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await _send_full_help(context)
            await delete_command_message(update, context)
            return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        await delete_command_message(update, context)
        return

    # --------- BORRADOR ----------
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info("Guardado en borrador: %s", msg.message_id)


# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)


# ========= ARRANQUE =========
async def _post_init(app: Application):
    # Opcional: publica la lista de comandos para el menú del cliente
    try:
        await app.bot.set_my_commands([
            ("listar", "Muestra borradores pendientes"),
            ("enviar", "Publica ahora (targets activos)"),
            ("preview", "Manda cola a PREVIEW"),
            ("programar", "Programa envío (24 h)"),
            ("programados", "Ver programaciones pendientes"),
            ("desprogramar", "Cancelar programación"),
            ("cancelar", "Quita de la cola sin borrar"),
            ("deshacer", "Revierte un /cancelar"),
            ("eliminar", "Borra del canal y de la cola"),
            ("nuke", "Eliminación masiva"),
            ("id", "Info del mensaje"),
            ("canales", "IDs + estado targets"),
            ("backup", "Backup on/off"),
            ("comandos", "Ayuda completa"),
        ])
    except Exception:
        pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
