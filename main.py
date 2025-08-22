# -*- coding: utf-8 -*-
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Set

from telegram import Update
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError

from config import (
    BOT_TOKEN, SOURCE_CHAT_ID, DB_FILE, TZ, TZNAME, PREVIEW_CHAT_ID
)
from database import (
    init_db, save_draft, list_drafts, mark_deleted, restore_draft, get_last_deleted
)
from utils import temp_notice, human_eta, extract_id_from_text, deep_link_for_channel_message
from ui import kb_main, text_main, kb_settings, text_settings
from scheduler import schedule_ids, programados_text, desprogramar, schedules_count
from publisher import publicar, publicar_ids, get_active_targets, set_active_backup, is_active_backup

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= Estado auxiliar =========
STATS = {"cancelados": 0, "eliminados": 0}

# ========= Inicializa DB =========
init_db(DB_FILE)

# ========= Helpers de publicación =========
async def publicar_todo_activos(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int]:
    # Publica todo lo no bloqueado (los bloqueos los maneja scheduler)
    from scheduler import blocked_ids_snapshot
    all_rows = list_drafts(DB_FILE)  # [(id, snippet)]
    if not all_rows:
        return 0, 0
    ids = [did for (did, _s) in all_rows if did not in blocked_ids_snapshot()]
    if not ids:
        return 0, 0
    pubs, fails, _ = await publicar_ids(context, ids=ids, targets=get_active_targets(), mark_as_sent=True)
    return pubs, fails

# ========= Comandos base =========
async def cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts = list_drafts(DB_FILE)
    if not drafts:
        await context.bot.send_message(SOURCE_CHAT_ID, "📁 No hay borradores.")
        return

    out = ["📋 Borradores pendientes:"]
    for i, (did, snip) in enumerate(drafts, start=1):
        s = (snip or "").strip()
        if len(s) > 60:
            s = s[:60] + "…"
        out.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did})")

    # Programaciones pendientes
    from scheduler import SCHEDULES
    if not SCHEDULES:
        out.append("\n🗂️ Programaciones pendientes: 0")
    else:
        out.append("\n" + programados_text())

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    from scheduler import unlock_id_if_scheduled
    unlock_id_if_scheduled(mid)
    STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)

async def cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    from database import _conn
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

    # hard delete
    c = _conn(DB_FILE)
    c.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
    c.commit()

    from scheduler import unlock_id_if_scheduled
    unlock_id_if_scheduled(mid)
    STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await temp_notice(context, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)

async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await temp_notice(context, "ℹ️ No hay nada para deshacer.", ttl=5)
        return

    restore_draft(DB_FILE, mid)
    if STATS["cancelados"] > 0:
        STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)

# ========= Programación =========
async def cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Formato inválido. Usa: /programar YYYY-MM-DD HH:MM  (formato 24 h)")
        return

    ids = [did for (did, _snip) in list_drafts(DB_FILE)]
    await schedule_ids(context, when, ids)

async def cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, programados_text())

async def cmd_desprogramar(context: ContextTypes.DEFAULT_TYPE, arg: str):
    txt = (arg or "").strip()
    await context.bot.send_message(SOURCE_CHAT_ID, await desprogramar(txt))

# ========= Preview =========
async def cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    await temp_notice(context, "⏳ Generando preview…", ttl=3)
    # enviar copia a PREVIEW sin marcar como enviado
    from database import _conn
    c = _conn(DB_FILE)
    rows = list(c.execute(
        "SELECT message_id, snippet, raw_json FROM drafts WHERE sent=0 AND deleted=0 ORDER BY message_id ASC"
    ).fetchall())
    if not rows:
        await context.bot.send_message(SOURCE_CHAT_ID, "🧪 Preview: enviados 0, fallidos 0.")
        return
    targets = [PREVIEW_CHAT_ID]
    pubs, fails, _ = await publicar_ids(context, ids=[r[0] for r in rows], targets=targets, mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

# ========= Panels =========
async def send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main())

async def send_settings_panel(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

# ========= Callbacks =========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    try:
        if data == "m:list":
            await cmd_listar(context)
        elif data == "m:send":
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
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
            await cmd_preview(context)
        elif data == "m:sched":
            from ui import InlineKeyboardButton, InlineKeyboardMarkup
            text = (
                "⏰ Programar envío de **los borradores actuales**.\n"
                "Elige un atajo o usa `/programar YYYY-MM-DD HH:MM` (formato 24 h).\n"
                "⚠️ Si no hay borradores, no se programa nada."
            )
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⏳ +5 min", callback_data="s:+5"),
                     InlineKeyboardButton("⏳ +15 min", callback_data="s:+15")],
                    [InlineKeyboardButton("🕗 Hoy 20:00", callback_data="s:today20"),
                     InlineKeyboardButton("🌅 Mañana 07:00", callback_data="s:tom07")],
                    [InlineKeyboardButton("🗒 Ver programados", callback_data="s:list"),
                     InlineKeyboardButton("❌ Cancelar todos", callback_data="s:clear")],
                    [InlineKeyboardButton("✍️ Custom", callback_data="s:custom"),
                     InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
                ]
            )
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
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
                await context.bot.send_message(SOURCE_CHAT_ID, await desprogramar("all"))
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM` (formato 24 h)\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown"
                )

            if when:
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
                else:
                    await schedule_ids(context, when, ids)

    except Exception as e:
        logger.exception(f"Error en callback: {e}")

# ========= Handler del canal =========
def is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))

async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    txt = (msg.text or "").strip()

    if is_command_text(txt):
        low = txt.lower()

        if low.startswith("/listar") or low.startswith("/lista"):
            await cmd_listar(context);  return

        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await cmd_cancelar(update, context, txt);  return

        if low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await cmd_eliminar(update, context, txt);  return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await cmd_deshacer(update, context, txt);  return

        if low.startswith("/nuke"):
            from utils import parse_nuke_selection
            drafts = list_drafts(DB_FILE)
            victims = parse_nuke_selection((txt.split(maxsplit=1)[1] if len(txt.split()) > 1 else ""), drafts)
            if not drafts:
                await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.");  return
            if not victims:
                await context.bot.send_message(
                    SOURCE_CHAT_ID,
                    "Usa: /nuke all | /nuke todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N"
                );  return
            borrados = 0
            for mid in sorted(victims, reverse=True):
                try:
                    await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
                except TelegramError:
                    pass
                from database import _conn
                c = _conn(DB_FILE)
                c.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
                c.commit()
                from scheduler import unlock_id_if_scheduled
                unlock_id_if_scheduled(mid)
                borrados += 1
            STATS["eliminados"] += borrados
            restantes = len(list_drafts(DB_FILE))
            await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")
            return

        if low.startswith("/enviar"):
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
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
            await cmd_preview(context);  return

        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await cmd_programar(context, when_str)
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /programar YYYY-MM-DD HH:MM  (formato 24 h)")
            return

        if low.startswith("/programados"):
            await cmd_programados(context);  return

        if low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await cmd_desprogramar(context, arg);  return

        if low.startswith("/id"):
            # Info del mensaje/ID o, si respondes, te dice el ID
            if msg.reply_to_message and len((txt or "").split()) == 1:
                rid = msg.reply_to_message.message_id
                await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
                return
            mid = extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
            if not mid:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.");  return
            mid = int(mid)
            link = deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 {mid}\n• Enlace: {link}")
            return

        if low.startswith(("/canales", "/targets", "/where", "/backup")):
            # /backup on|off
            if low.startswith("/backup"):
                parts = txt.split(maxsplit=1)
                arg = (parts[1] if len(parts) > 1 else "").strip().lower()
                if arg in ("on", "1", "true", "si", "sí"):
                    set_active_backup(True)
                elif arg in ("off", "0", "false", "no"):
                    set_active_backup(False)
                else:
                    await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
                    return
            await send_settings_panel(context);  return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await send_help_with_buttons(context);  return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        return

    # ===== Borrador =====
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    from database import save_draft
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
