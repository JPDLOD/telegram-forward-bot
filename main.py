# -*- coding: utf-8 -*-
# Canal BORRADOR -> Canal PRINCIPAL
# Guarda todo lo que publiques en el BORRADOR en SQLite y, al usar /enviar o /programar,
# lo publica en el PRINCIPAL copiando (sin "Forwarded from...") y respetando el orden.

import os
import json
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, delete_draft, list_drafts
)

# =========================
# CONFIG (usa ENV si existen; si no, usa estos valores)
# =========================
BOT_TOKEN      = os.getenv("BOT_TOKEN", "8400444635:AAFPehmdHwvL2Ho2WE_81GwlEaNhYfmE4vs")
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "-1002859784457"))   # BORRADOR
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002679848195"))  # PRINCIPAL
DB_FILE = "drafts.db"

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DB =========
init_db(DB_FILE)
logger.info(f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  PRINCIPAL={TARGET_CHAT_ID}")

# -------------------------------------------------------
# Publicar todos los borradores pendientes en orden
# -------------------------------------------------------
async def _publicar_todo(context: ContextTypes.DEFAULT_TYPE) -> int:
    rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not rows:
        return 0

    enviados = []
    for mid, _t, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        try:
            # Si es encuesta, reconstruir
            if "poll" in data:
                p = data["poll"]
                question = p.get("question", "Pregunta")
                options = [o.get("text", "") for o in p.get("options", [])]
                is_anon = p.get("is_anonymous", True)
                poll_type = p.get("type", "regular")
                kwargs = dict(chat_id=TARGET_CHAT_ID, question=question, options=options, is_anonymous=is_anon)
                if poll_type == "quiz":
                    kwargs["type"] = "quiz"
                    if p.get("correct_option_id") is not None:
                        kwargs["correct_option_id"] = p["correct_option_id"]
                await context.bot.send_poll(**kwargs)
            else:
                # Cualquier otro mensaje: copiar tal cual
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid
                )
            enviados.append(mid)
        except Exception as e:
            logger.exception(f"Error publicando {mid}: {e}")

    if enviados:
        mark_sent(DB_FILE, enviados)
    return len(enviados)

# -------------------------------------------------------
# Handler único de POSTS en el CANAL BORRADOR
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post  # IMPORTANTÍSIMO: en canales es channel_post
    if not msg:
        return
    if msg.chat_id != SOURCE_CHAT_ID:
        # Ignorar otros canales donde el bot pueda ser admin
        return

    txt = (msg.text or "").strip()

    # --------- COMANDOS (como posts del canal) ----------
    if txt.startswith("/listar"):
        drafts = list_drafts(DB_FILE)
        if not drafts:
            await context.bot.send_message(SOURCE_CHAT_ID, "📂 No hay borradores.")
            return
        out = ["📋 Borradores pendientes:"]
        for did, snip in drafts:
            s = (snip or "")
            if len(s) > 40:
                s = s[:40] + "…"
            out.append(f"• {did} — {s}")
        await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))
        return

    if txt.startswith("/borrar"):
        parts = txt.split()
        if len(parts) == 2 and parts[1].isdigit():
            delete_draft(DB_FILE, int(parts[1]))
            await context.bot.send_message(SOURCE_CHAT_ID, f"🗑️ Borrador {parts[1]} eliminado.")
        else:
            await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /borrar <message_id>")
        return

    if txt.startswith("/enviar") or txt.startswith("/enviar_casos_clinicos"):
        n = await _publicar_todo(context)
        await context.bot.send_message(SOURCE_CHAT_ID, f"✅ Publicados {n} mensaje(s).")
        return

    if txt.startswith("/programar"):
        # /programar YYYY-MM-DD HH:MM
        parts = txt.split()
        if len(parts) < 3:
            await context.bot.send_message(SOURCE_CHAT_ID, "⏰ Usa: /programar YYYY-MM-DD HH:MM")
            return
        try:
            when = datetime.strptime(parts[1] + " " + parts[2], "%Y-%m-%d %H:%M")
            seconds = max(0, int((when - datetime.now()).total_seconds()))

            async def job(ctx: ContextTypes.DEFAULT_TYPE):
                n = await _publicar_todo(ctx)
                await ctx.bot.send_message(SOURCE_CHAT_ID, f"⏱️ Programación ejecutada. Publicados {n}.")

            context.job_queue.run_once(lambda ctx: job(ctx), when=seconds)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🗓️ Programado para {when:%Y-%m-%d %H:%M}.")
        except Exception:
            await context.bot.send_message(SOURCE_CHAT_ID, "❌ Formato inválido. Ej: /programar 2025-08-20 07:00")
        return

    if txt.startswith("/id"):
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            f"BORRADOR: `{SOURCE_CHAT_ID}`\nPRINCIPAL: `{TARGET_CHAT_ID}`",
            parse_mode="Markdown"
        )
        return

    if txt.startswith("/ayuda") or txt.startswith("/start"):
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "Comandos:\n"
            "• /listar — muestra borradores\n"
            "• /borrar <message_id> — elimina de la cola\n"
            "• /enviar — publica ahora\n"
            "• /programar YYYY-MM-DD HH:MM — programa el envío\n"
            "• /id — muestra IDs"
        )
        return

    # --------- SI NO ES COMANDO → GUARDAR BORRADOR ----------
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info(f"Guardado en borrador: {msg.message_id}")

# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # En canales se usa MessageHandler con ChatType.CHANNEL
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
