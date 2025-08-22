# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, CallbackQueryHandler, filters
from telegram.error import TelegramError

from config import (
    BOT_TOKEN, SOURCE_CHAT_ID, DB_FILE, TZ, TZNAME,
    PREVIEW_CHAT_ID, BACKUP_CHAT_ID, TARGET_CHAT_ID, PAUSE
)
from database import init_db, save_draft, list_drafts, restore_draft, get_last_deleted, mark_deleted
from publisher import publicar_todo
from scheduler import schedule_ids, list_programados, desprogramar, SCHEDULES
from core_utils import is_command_text, human_eta
from ui import kb_main, text_main, kb_schedule, text_schedule, kb_settings, text_settings

# ====== Logging ======
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== Estado ======
ACTIVE_BACKUP = True
STATS = {"cancelados": 0, "eliminados": 0}

# ====== Init DB ======
init_db(DB_FILE)

# ====== Helpers ======
async def temp_notice(context: ContextTypes.DEFAULT_TYPE, text: str, ttl: int = 6):
    try:
        m = await context.bot.send_message(SOURCE_CHAT_ID, text, disable_notification=True)
    except Exception:
        return
    async def _auto_del():
        await asyncio.sleep(ttl)
        try:
            await context.bot.delete_message(SOURCE_CHAT_ID, m.message_id)
        except Exception:
            pass
    asyncio.create_task(_auto_del())

def extract_id_from_text(txt: str) -> Optional[int]:
    parts = (txt or "").split()
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
        if p.lower().startswith("id:"):
            n = p.split(":", 1)[1]
            if n.isdigit():
                return int(n)
    return None

# ====== Comandos ======
async def cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts = list_drafts(DB_FILE)
    out = []
    if drafts:
        out.append("📋 Borradores pendientes:")
        for i, (did, snip) in enumerate(drafts, start=1):
            s = (snip or "").strip()
            if len(s) > 60:
                s = s[:60] + "…"
            out.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did})")
    else:
        out.append("📁 No hay borradores.")

    # Mostrar programaciones
    if not SCHEDULES:
        out.append("\n🗂 Programaciones pendientes: 0")
    else:
        out.append("\n🗂 Programaciones pendientes:")
        now = datetime.now(tz=TZ)
        for pid, rec in sorted(SCHEDULES.items()):
            when = rec["when"]
            ids = rec["ids"]
            out.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {human_eta(when, now)} — {len(ids)} mensajes")

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await temp_notice(context, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)

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

async def cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    # Sólo copia al PREVIEW sin marcar enviados
    from publisher import publicar_rows, get_unsent_drafts
    rows = get_unsent_drafts(DB_FILE)
    if not rows:
        await context.bot.send_message(SOURCE_CHAT_ID, "🧪 Preview: no hay borradores.")
        return
    pubs, fails = await publicar_rows(context, rows=rows, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

async def cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Formato inválido. Usa: /programar YYYY-MM-DD HH:MM  (formato 24h)")
        return
    ids = [did for (did, _snip) in list_drafts(DB_FILE)]
    await schedule_ids(context, when, ids, active_backup=ACTIVE_BACKUP)

async def cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    await list_programados(context)

async def cmd_desprogramar(context: ContextTypes.DEFAULT_TYPE, arg: str):
    await desprogramar(context, arg)

async def cmd_enviar(context: ContextTypes.DEFAULT_TYPE):
    pubs, fails = await publicar_todo(context, active_backup=ACTIVE_BACKUP, mark_as_sent=True)
    msg_out = f"✅ Publicados {pubs}."
    if fails:
        msg_out += f"\n📦 Fallidos: {fails}."
    if STATS["cancelados"] or STATS["eliminados"]:
        extras = []
        if STATS["cancelados"]:
            extras.append(f"Cancelados: {STATS['cancelados']}")
        if STATS["eliminados"]:
            extras.append(f"Eliminados: {STATS['eliminados']}")
        msg_out += "\n📦 " + " · ".join(extras) + "."
    await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
    STATS["cancelados"] = 0
    STATS["eliminados"] = 0

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    # Modo responder (sin argumentos)
    if update.channel_post and update.channel_post.reply_to_message and len((txt or "").split()) == 1:
        rid = update.channel_post.reply_to_message.message_id
        await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
        return

    # Con argumento
    parts = (txt or "").split()
    mid = None
    if len(parts) > 1 and parts[1].isdigit():
        mid = int(parts[1])

    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
        return

    # Leer de la BD
    import sqlite3
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = con.execute("SELECT text, raw_json, sent, deleted, created_at FROM drafts WHERE message_id=?", (mid,))
    row = cur.fetchone()
    con.close()

    snippet = "[contenido]"
    tipo = "desconocido"
    fecha = ""

    if row:
        try:
            text, raw_json, _sent, _deleted, created_at = row
            if text:
                snippet = (text.strip()[:180] + "…") if len(text.strip()) > 180 else text.strip()
            if raw_json:
                d = json.loads(raw_json)
                if "poll" in d:
                    tipo = "encuesta" if (d.get("poll", {}).get("type") != "quiz") else "quiz"
                elif "photo" in d:
                    tipo = "foto"
                elif "document" in d:
                    tipo = "documento"
                elif "video" in d:
                    tipo = "video"
                else:
                    tipo = "texto"
            # === FECHA: soporta epoch entero o ISO ===
            from datetime import datetime as _dt
            try:
                if isinstance(created_at, (int, float)) or str(created_at).isdigit():
                    fecha = _dt.fromtimestamp(int(created_at), tz=TZ).strftime("%Y-%m-%d %H:%M")
                else:
                    fecha = _dt.fromisoformat(str(created_at)).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                fecha = str(created_at)
        except Exception:
            pass

    link = deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
    out = f"🆔 {mid}\n• Tipo: {tipo}\n• Snippet: {snippet}\n• Fecha: {fecha}\n• Enlace: {link}"
    await context.bot.send_message(SOURCE_CHAT_ID, out)

def deep_link_for_channel_message(chat_id: int, mid: int) -> str:
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"

# ====== UI callbacks ======
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
            await cmd_enviar(context)
        elif data == "m:preview":
            await temp_notice(context, "⏳ Generando preview…", ttl=3)
            await cmd_preview(context)
        elif data == "m:sched":
            await q.edit_message_text(text_schedule(), reply_markup=kb_schedule())
        elif data == "m:settings":
            await q.edit_message_text(text_settings(ACTIVE_BACKUP), reply_markup=kb_settings(ACTIVE_BACKUP))
        elif data == "m:toggle_backup":
            global ACTIVE_BACKUP
            ACTIVE_BACKUP = not ACTIVE_BACKUP
            await q.edit_message_text(text_settings(ACTIVE_BACKUP), reply_markup=kb_settings(ACTIVE_BACKUP))
        elif data == "m:back":
            await q.edit_message_text(text_main(), reply_markup=kb_main())
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
                await list_programados(context); return
            elif data == "s:clear":
                await desprogramar(context, "all"); return
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM`  *(formato 24h)*\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown", reply_markup=kb_schedule()
                )
                return
            if when:
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
                else:
                    await schedule_ids(context, when, ids, active_backup=ACTIVE_BACKUP)
    except Exception as e:
        logger.exception(f"Error en callback: {e}")

# ====== Handler de canal ======
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

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await cmd_deshacer(update, context, txt);  return

        if low.startswith("/enviar"):
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
            await cmd_enviar(context);  return

        if low.startswith("/preview"):
            await cmd_preview(context);  return

        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await cmd_programar(context, when_str)
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /programar YYYY-MM-DD HH:MM  (formato 24h)")
            return

        if low.startswith("/programados"):
            await cmd_programados(context);  return

        if low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await cmd_desprogramar(context, arg);  return

        if low.startswith("/id"):
            await cmd_id(update, context, txt);  return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main());  return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        return

    # Guardar borrador
    snippet = msg.text or msg.caption or ""
    raw_json = msg.to_json()  # igual a json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info(f"Guardado en borrador: {msg.message_id}")

# ====== Main ======
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    # Defaults para que scheduler pueda usarlos
    app.bot_data['source_chat_id'] = SOURCE_CHAT_ID
    app.bot_data['db_file'] = DB_FILE

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
