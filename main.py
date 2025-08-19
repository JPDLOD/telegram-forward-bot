# -*- coding: utf-8 -*-
# BORRADOR (-1002859784457) -> PRINCIPAL (-1002679848195)
# Guarda todo lo que publiques en BORRADOR y, al usar /enviar o /programar,
# lo publica en PRINCIPAL en el MISMO ORDEN, sin "Forwarded from...".
# Reconstruye encuestas (quiz/regular) y copia el resto de mensajes.

import os
import json
import logging
from datetime import datetime
from typing import Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.error import TelegramError, Forbidden, BadRequest

from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, delete_draft, list_drafts
)

# =========================
# CONFIG (TOKEN SOLO POR ENV)
# =========================
# IMPORTANTE: No hay valor por defecto. Si falta, revienta con error claro.
BOT_TOKEN = os.environ["BOT_TOKEN"]  # <- Solo Render Environment
SOURCE_CHAT_ID = -1002859784457      # BORRADOR (fijo, como pediste)
TARGET_CHAT_ID = -1002679848195      # PRINCIPAL (fijo, como pediste)
DB_FILE = "drafts.db"

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DB =========
init_db(DB_FILE)
logger.info(f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  PRINCIPAL={TARGET_CHAT_ID}")

# -------------------------------------------------------
# helpers
# -------------------------------------------------------
def _poll_payload_from_raw(raw: dict) -> Tuple[dict, bool]:
    """
    Extrae un payload listo para send_poll a partir de raw['poll'].
    Devuelve (kwargs, is_quiz).
    """
    p = raw.get("poll") or {}
    question = p.get("question", "Pregunta")
    options  = [o.get("text", "") for o in p.get("options", [])]
    is_anon  = p.get("is_anonymous", True)
    allows_multiple = p.get("allows_multiple_answers", False)
    ptype = p.get("type", "regular")
    is_quiz = (ptype == "quiz")

    kwargs = dict(
        chat_id=TARGET_CHAT_ID,
        question=question,
        options=options,
        is_anonymous=is_anon,
        allows_multiple_answers=allows_multiple
    )

    # Quiz: respuesta correcta
    if is_quiz and p.get("correct_option_id") is not None:
        kwargs["type"] = "quiz"
        kwargs["correct_option_id"] = int(p["correct_option_id"])

    # Tiempos (si existieran)
    if p.get("open_period") is not None:
        try:
            kwargs["open_period"] = int(p["open_period"])
        except Exception:
            pass
    if p.get("close_date") is not None:
        try:
            kwargs["close_date"] = int(p["close_date"])
        except Exception:
            pass

    # Explicación (solo quiz)
    if is_quiz and p.get("explanation"):
        kwargs["explanation"] = str(p["explanation"])

    return kwargs, is_quiz

# -------------------------------------------------------
# Publicar todos los borradores pendientes en orden
# -------------------------------------------------------
async def _publicar_todo(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int]:
    rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not rows:
        return 0, 0

    publicados, fallidos = 0, 0
    enviados_ids = []

    for mid, _t, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        try:
            # ---- Encuesta: RECONSTRUIR ----
            if "poll" in data:
                kwargs, _is_quiz = _poll_payload_from_raw(data)
                await context.bot.send_poll(**kwargs)
                publicados += 1

            # ---- Resto: copiar tal cual ----
            else:
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid
                )
                publicados += 1

            enviados_ids.append(mid)

        except (Forbidden, BadRequest) as e:
            fallidos += 1
            logger.error(f"Falló publicar {mid}: {e}")
        except TelegramError as e:
            fallidos += 1
            logger.error(f"TelegramError publicando {mid}: {e}")
        except Exception as e:
            fallidos += 1
            logger.exception(f"Error publicando {mid}: {e}")

    if enviados_ids:
        mark_sent(DB_FILE, enviados_ids)
    return publicados, fallidos

# -------------------------------------------------------
# Handler único de POSTS en el CANAL BORRADOR
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post  # En canales es channel_post
    if not msg:
        return
    if msg.chat_id != SOURCE_CHAT_ID:
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
        ok, fail = await _publicar_todo(context)
        msg_out = f"✅ Publicados {ok} mensaje(s)."
        if fail:
            msg_out += f" ⚠️ Fallidos: {fail} (verifica permisos en el canal destino, especialmente ENCUESTAS)."
        await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
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
                ok, fail = await _publicar_todo(ctx)
                msg2 = f"⏱️ Programación ejecutada. Publicados {ok}."
                if fail:
                    msg2 += f" Fallidos: {fail}."
                await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)

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

# ========= ERROR HANDLER (para ver tracebacks bonitos) =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)

# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # En canales se usa MessageHandler con ChatType.CHANNEL
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))

    # Registrar error handler
    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
