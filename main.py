# -*- coding: utf-8 -*-
"""
main.py
-------
Punto de entrada. Enruta comandos, callbacks y usa los módulos:
- database.py
- publisher.py
- scheduler.py
- ui.py
- utils.py
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, CallbackQueryHandler, filters

from config import BOT_TOKEN, SOURCE_CHAT_ID, TZNAME, TZ, DB_FILE, PREVIEW_CHAT_ID
from database import init_db, save_draft, list_drafts
import publisher
import ui
import scheduler  # si tu módulo de programación está separado; si no, ignóralo

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# init DB
init_db(DB_FILE)


# ===== utilidades locales =====
def is_command_text(txt: str | None) -> bool:
    return bool(txt and txt.strip().startswith("/"))

async def temp_notice(context: ContextTypes.DEFAULT_TYPE, text: str, ttl: int = 6):
    try:
        m = await context.bot.send_message(SOURCE_CHAT_ID, text, disable_notification=True)
    except Exception:
        return

    async def _auto_del():
        try:
            await asyncio.sleep(ttl)
            await context.bot.delete_message(SOURCE_CHAT_ID, m.message_id)
        except Exception:
            pass

    asyncio.create_task(_auto_del())


# ======= CALLBACKS (inline) =======
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
            pubs, fails, posted = await publisher.publicar(context, asyncio)
            publisher.LAST_BATCH = posted
            extras = []
            if publisher.STATS["cancelados"]:
                extras.append(f"Cancelados: {publisher.STATS['cancelados']}")
            if publisher.STATS["eliminados"]:
                extras.append(f"Eliminados: {publisher.STATS['eliminados']}")
            msg_out = f"✅ Publicados {pubs}."
            if fails:
                extras.append(f"Fallidos: {fails}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            publisher.STATS["cancelados"] = 0
            publisher.STATS["eliminados"] = 0

        elif data == "m:preview":
            await temp_notice(context, "⏳ Generando preview…", ttl=3)
            pubs, fails, _ = await publisher.publicar_rows(
                context=context,
                rows=[r for r in (await _rows_all()) if r[0] not in publisher.SCHEDULED_LOCK],
                targets=[PREVIEW_CHAT_ID],
                mark_as_sent=False,
                asyncio_mod=asyncio,
            )
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

        elif data == "m:sched":
            await q.edit_message_text("⏰ Programar envío de **los borradores actuales**.\n"
                                      "Elige un atajo o usa `/programar YYYY-MM-DD HH:MM`.\n"
                                      "⚠️ Si no hay borradores, no se programa nada.",
                                      reply_markup=ui.kb_schedule(), parse_mode="Markdown")

        elif data == "m:settings":
            await q.edit_message_text(ui.text_settings(), reply_markup=ui.kb_settings(), parse_mode="Markdown")

        elif data == "m:toggle_backup":
            # <<<< AQUÍ está el fix principal
            publisher.toggle_active_backup()
            await q.edit_message_text(ui.text_settings(), reply_markup=ui.kb_settings(), parse_mode="Markdown")

        elif data == "m:back":
            await q.edit_message_text(ui.text_main(), reply_markup=ui.kb_main())

        # Atajos de programación rápida
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
                await scheduler.cmd_programados(context)
            elif data == "s:clear":
                await scheduler.cmd_desprogramar(context, "all")
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM` (formato 24 h)\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown", reply_markup=ui.kb_schedule()
                )

            if when:
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
                else:
                    await scheduler.schedule_ids(context, when, ids)

    except Exception as e:
        logger.exception(f"Error en callback: {e}")


# ======= COMANDOS =======
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
    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))


async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    txt = (msg.text or "").strip()

    if is_command_text(txt):
        low = txt.lower()

        if low.startswith("/listar") or low.startswith("/lista"):
            await cmd_listar(context);  return

        if low.startswith("/enviar"):
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
            pubs, fails, posted = await publisher.publicar(context, asyncio)
            publisher.LAST_BATCH = posted
            extras = []
            if publisher.STATS["cancelados"]:
                extras.append(f"Cancelados: {publisher.STATS['cancelados']}")
            if publisher.STATS["eliminados"]:
                extras.append(f"Eliminados: {publisher.STATS['eliminados']}")
            msg_out = f"✅ Publicados {pubs}."
            if fails:
                extras.append(f"Fallidos: {fails}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            publisher.STATS["cancelados"] = 0
            publisher.STATS["eliminados"] = 0
            return

        if low.startswith("/preview"):
            await temp_notice(context, "⏳ Generando preview…", ttl=3)
            # usamos la misma función base que en el callback
            pubs, fails, _ = await publisher.publicar_rows(
                context=context,
                rows=[r for r in (await _rows_all()) if r[0] not in publisher.SCHEDULED_LOCK],
                targets=[PREVIEW_CHAT_ID],
                mark_as_sent=False,
                asyncio_mod=asyncio,
            )
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
            return

        if low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await ui.handle_backup_command(context, arg);  return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await ui.send_help_with_buttons(context);  return

        # … aquí mantienes el resto de comandos que ya tienes (nuke, cancelar, eliminar,
        # programar, programados, desprogramar, id, canales, etc.) sin cambios …

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        return

    # --------- Guarda borrador ----------
    snippet = msg.text or msg.caption or ""
    raw_json = msg.to_dict()
    import json
    save_draft(DB_FILE, msg.message_id, snippet, json.dumps(raw_json, ensure_ascii=False))


# ===== helpers internos =====
async def _rows_all():
    # usar database.get_unsent_drafts sin filtrar bloqueados
    from database import get_unsent_drafts
    return get_unsent_drafts(DB_FILE)


# ===== MAIN =====
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
