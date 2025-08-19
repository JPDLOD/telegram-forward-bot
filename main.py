# -*- coding: utf-8 -*-
# BORRADOR (-1002859784457) -> PRINCIPAL (-1002679848195)

import os
import re
import json
import logging
from datetime import datetime
from typing import Tuple, Optional, List

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.error import (
    TelegramError, Forbidden, BadRequest, RetryAfter, TimedOut
)
from asyncio import sleep

from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, delete_draft, list_drafts
)

# =========================
# CONFIG
# =========================
BOT_TOKEN      = os.environ["BOT_TOKEN"]                    # token solo por ENV
SOURCE_CHAT_ID = -1002859784457                             # canal BORRADOR
TARGET_CHAT_ID = -1002679848195                             # canal PRINCIPAL
DB_FILE        = "drafts.db"

PAUSE_BETWEEN  = float(os.getenv("PAUSE", "0.7"))           # seg entre envíos
MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "5"))         # reintentos por msg

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
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

    if is_quiz and p.get("correct_option_id") is not None:
        kwargs["type"] = "quiz"
        kwargs["correct_option_id"] = int(p["correct_option_id"])

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

    if is_quiz and p.get("explanation"):
        kwargs["explanation"] = str(p["explanation"])

    return kwargs, is_quiz

def _label_from_raw(raw_json: str, fallback_text: str) -> str:
    """
    Para /listar: genera una etiqueta amigable si el texto está vacío.
    """
    try:
        d = json.loads(raw_json or "{}")
    except Exception:
        d = {}
    if "poll" in d:
        return "[encuesta]"
    for media_key, label in [
        ("photo", "[imagen]"),
        ("video", "[video]"),
        ("animation", "[gif]"),
        ("document", "[archivo]"),
        ("audio", "[audio]"),
        ("voice", "[nota de voz]"),
    ]:
        if d.get(media_key):
            return label
    t = (fallback_text or "").strip()
    return t if t else "[mensaje]"

async def _send_one(context: ContextTypes.DEFAULT_TYPE, mid: int, raw_json: str) -> None:
    """
    Envía 1 mensaje (o encuesta) con reintentos, manejando:
      - TimedOut
      - RetryAfter (flood control)
      - BadRequest específicos
    Lanza excepción si, tras reintentos, no se logró.
    """
    # ¿Es encuesta?
    data = {}
    try:
        data = json.loads(raw_json or "{}")
    except Exception:
        pass

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if "poll" in data:
                kwargs, _ = _poll_payload_from_raw(data)
                await context.bot.send_poll(**kwargs)
            else:
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid
                )
            # éxito
            return

        except RetryAfter as e:
            wait = max(int(getattr(e, "retry_after", 3)), 3)
            logger.warning(f"[{mid}] Flood control (RetryAfter). Esperando {wait}s (intento {attempt}/{MAX_RETRIES})")
            await sleep(wait)

        except TimedOut:
            wait = 3 + attempt  # backoff suave
            logger.warning(f"[{mid}] Timed out. Reintentando en {wait}s (intento {attempt}/{MAX_RETRIES})")
            await sleep(wait)

        except BadRequest as e:
            msg = str(e)
            # Parseo de "Flood control exceeded. Retry in X seconds"
            m = re.search(r"Retry in (\d+) seconds", msg)
            if m:
                wait = max(int(m.group(1)), 3)
                logger.warning(f"[{mid}] Flood control (429). Esperando {wait}s (intento {attempt}/{MAX_RETRIES})")
                await sleep(wait)
                continue

            if "Message to copy not found" in msg:
                # No existe en el canal origen (borrado). No vale la pena reintentar más.
                raise

            # Otros BadRequest: un pequeño backoff y reintento
            wait = min(5 + attempt, 10)
            logger.warning(f"[{mid}] BadRequest: {msg}. Esperando {wait}s (intento {attempt}/{MAX_RETRIES})")
            await sleep(wait)

        except TelegramError as e:
            # Cualquier otro error de red temporal
            wait = min(5 + 2*attempt, 20)
            logger.warning(f"[{mid}] TelegramError: {e}. Esperando {wait}s (intento {attempt}/{MAX_RETRIES})")
            await sleep(wait)

    # Si salió del bucle, no se logró
    raise TelegramError(f"Fallo definitivo tras {MAX_RETRIES} intentos")

# -------------------------------------------------------
# Publicar todos los borradores pendientes en orden
# -------------------------------------------------------
async def _publicar_todo(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int, List[int]]:
    rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not rows:
        return 0, 0, []

    ok, fail = 0, 0
    enviados_ids: List[int] = []
    fallidos_ids: List[int] = []

    for mid, _t, raw in rows:
        try:
            await _send_one(context, mid, raw)
            mark_sent(DB_FILE, [mid])
            enviados_ids.append(mid)
            ok += 1
            # Pausa preventiva entre mensajes
            await sleep(PAUSE_BETWEEN)

        except Exception as e:
            fail += 1
            fallidos_ids.append(mid)
            logger.error(f"Error publicando {mid}: {e}")

    return ok, fail, fallidos_ids

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
        drafts = list_drafts(DB_FILE)  # [(message_id, text, raw_json)]
        if not drafts:
            await context.bot.send_message(SOURCE_CHAT_ID, "📂 No hay borradores.")
            return

        out = ["📋 Borradores pendientes:"]
        # Enumeración 1..N (no IDs), con etiquetas para medios/encuestas
        for idx, (mid, snip, rawj) in enumerate(drafts, start=1):
            label = _label_from_raw(rawj, snip)
            shown = (label[:60] + "…") if len(label) > 60 else label
            out.append(f"• {idx:02d} — {shown}  (id:{mid})")

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
        ok, fail, fallidos = await _publicar_todo(context)
        resumen = f"✅ Publicados {ok}. "
        if fail:
            resumen += f"⚠️ Fallidos: {fail}. Pendientes: {', '.join(map(str, fallidos[:10]))}"
            if len(fallidos) > 10:
                resumen += "…"
        total = ok + fail
        resumen += f"\n📦 Resultado: {ok}/{total} enviados."
        await context.bot.send_message(SOURCE_CHAT_ID, resumen)
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
                ok, fail, _ = await _publicar_todo(ctx)
                msg2 = f"⏱️ Programación ejecutada. Publicados {ok}. Fallidos {fail}."
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
            "• /listar — muestra borradores (enumerados 1..N)\n"
            "• /borrar <message_id> — elimina de la cola\n"
            "• /enviar — publica ahora (con reintentos y pausa)\n"
            "• /programar YYYY-MM-DD HH:MM — programa el envío\n"
            "• /id — muestra IDs"
        )
        return

    # --------- SI NO ES COMANDO → GUARDAR BORRADOR ----------
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info(f"Guardado en borrador: {msg.message_id}")

# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)

# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # En canales se usa MessageHandler con ChatType.CHANNEL
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))

    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
