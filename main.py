import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from database import init_db, save_draft, get_unsent_drafts, mark_sent, delete_draft, list_drafts
import asyncio
from datetime import datetime, timedelta

# ====== CONFIGURACIÓN ======
BOT_TOKEN = "8400444635:AAFPehmdHwvL2Ho2WE_81GwlEaNhYfmE4vs"
SOURCE_CHAT_ID = -1002679848195   # Canal borrador
TARGET_CHAT_ID = -1002859784457   # Canal destino
DB_FILE = "drafts.db"

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ====== INICIALIZAR DB ======
init_db(DB_FILE)

# ====== MANEJADORES ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Soy tu bot de reenvío. "
        "Manda mensajes al canal borrador y yo los guardaré.\n\n"
        "Comandos:\n"
        "/enviar → enviar pendientes\n"
        "/programar 07:00 → programa envío\n"
        "/listar → listar borradores pendientes\n"
        "/borrar <id> → borra un borrador específico\n"
    )

async def recibir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda cualquier mensaje del canal borrador"""
    if update.effective_chat.id == SOURCE_CHAT_ID:
        msg = update.message
        save_draft(DB_FILE, msg.message_id, msg.text_html or "", msg.to_dict())
        logger.info(f"Mensaje guardado: {msg.message_id}")
    else:
        await update.message.reply_text("⚠️ Solo acepto mensajes del canal borrador.")

async def enviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reenvía todos los borradores pendientes"""
    drafts = get_unsent_drafts(DB_FILE)
    if not drafts:
        await update.message.reply_text("✅ No hay borradores pendientes.")
        return

    for d in drafts:
        await context.bot.copy_message(
            chat_id=TARGET_CHAT_ID,
            from_chat_id=SOURCE_CHAT_ID,
            message_id=d[0]  # message_id original
        )
    mark_sent(DB_FILE, [d[0] for d in drafts])
    await update.message.reply_text(f"📨 Enviados {len(drafts)} mensajes en orden.")

async def programar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Programa un envío a cierta hora"""
    if not context.args:
        await update.message.reply_text("⏰ Usa: /programar HH:MM")
        return

    hora = context.args[0]
    try:
        now = datetime.now()
        h, m = map(int, hora.split(":"))
        target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target_time < now:
            target_time += timedelta(days=1)

        delay = (target_time - now).total_seconds()

        await update.message.reply_text(
            f"⏳ Mensajes programados para {target_time.strftime('%H:%M')}."
        )

        async def job():
            drafts = get_unsent_drafts(DB_FILE)
            for d in drafts:
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=d[0]
                )
            mark_sent(DB_FILE, [d[0] for d in drafts])

        context.application.create_task(asyncio.sleep(delay, result=None))
        context.application.create_task(job())

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista los borradores pendientes"""
    drafts = list_drafts(DB_FILE)
    if not drafts:
        await update.message.reply_text("📂 No hay borradores.")
        return

    txt = "📋 Borradores pendientes:\n\n"
    for d in drafts:
        txt += f"🆔 {d[0]} → {d[1][:40]}...\n"
    await update.message.reply_text(txt)

async def borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Borra un borrador específico"""
    if not context.args:
        await update.message.reply_text("❌ Usa: /borrar <id>")
        return
    try:
        draft_id = int(context.args[0])
        delete_draft(DB_FILE, draft_id)
        await update.message.reply_text(f"🗑️ Borrador {draft_id} eliminado.")
    except:
        await update.message.reply_text("⚠️ Error al borrar.")

# ====== MAIN ======
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("enviar", enviar))
    app.add_handler(CommandHandler("programar", programar))
    app.add_handler(CommandHandler("listar", listar))
    app.add_handler(CommandHandler("borrar", borrar))
    app.add_handler(MessageHandler(filters.ALL, recibir))

    logger.info("Bot iniciado 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()