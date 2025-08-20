# -*- coding: utf-8 -*-
# BORRADOR (-1002859784457) -> PRINCIPAL (-1002679848195)
# Guarda todo lo que publiques en BORRADOR y, al usar /enviar o /programar,
# lo publica en PRINCIPAL en el MISMO ORDEN, sin "Forwarded from...".
# Reconstruye encuestas y copia el resto de mensajes.
#
# Cambios clave:
# - No guarda ni envía comandos (cualquier texto que empiece con '/').
# - /remover por respuesta o por id; confirma y muestra conteo restante.
# - /listar con índice limpio (#1, #2...) + id real para borrar exacto.
# - Auto-rate-limit (PAUSE) + manejo 429 RetryAfter + reintentos TimedOut.
# - /programar arreglado (YYYY-MM-DD HH:MM).

import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Tuple

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.error import TelegramError, Forbidden, BadRequest, RetryAfter, TimedOut

from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, delete_draft, list_drafts
)

# =========================
# CONFIG
# =========================
# TOKEN solo por ENV (si falta, peta con error claro)
BOT_TOKEN = os.environ["BOT_TOKEN"]

# IDs fijos como pediste (borra o cambia si algún día quieres leerlos de ENV)
SOURCE_CHAT_ID = -1002859784457      # BORRADOR
TARGET_CHAT_ID = -1002679848195      # PRINCIPAL

# Pausa base entre envíos para no gatillar flood control (se puede ajustar en Render)
PAUSE = float(os.getenv("PAUSE", "0.6"))
if PAUSE < 0:
    PAUSE = 0.0

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

def _snippet_from_msg(msg) -> str:
    """Texto corto para /listar cuando no hay caption/text."""
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if getattr(msg, "poll", None):
        return "[encuesta]"
    if getattr(msg, "photo", None):
        return "[foto]"
    if getattr(msg, "video", None):
        return "[video]"
    if getattr(msg, "document", None):
        return "[documento]"
    if getattr(msg, "audio", None):
        return "[audio]"
    if getattr(msg, "voice", None):
        return "[nota de voz]"
    return "[mensaje]"

def _parse_id_arg(txt: str) -> int | None:
    """Soporta '/remover 303', '/remover id:303', '/remover id=303'."""
    parts = txt.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip()
    for pref in ("id:", "id=", "ID:", "ID="):
        if raw.startswith(pref):
            raw = raw[len(pref):].strip()
            break
    return int(raw) if raw.isdigit() else None

# -------------------------------------------------------
# Envío con control de tasa y reintentos
# -------------------------------------------------------
async def _send_with_guard(coro_func, *, description: str) -> bool:
    """
    Ejecuta una corrutina que llama al API de Telegram con manejo de:
      - RetryAfter (429) -> espera e intenta de nuevo
      - TimedOut -> reintentos con backoff
    Devuelve True si se logró enviar, False si fallo definitivo.
    """
    # backoff para TimedOut
    max_retries = 3
    attempt = 0
    while True:
        try:
            await coro_func()
            # pausa corta entre envíos siempre
            if PAUSE > 0:
                await asyncio.sleep(PAUSE)
            return True

        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 1)) + 1
            logger.warning(f"429 RetryAfter en {description}. Esperando {wait}s …")
            await asyncio.sleep(wait)
            # y reintenta

        except TimedOut:
            attempt += 1
            if attempt > max_retries:
                logger.error(f"Timed out repetido en {description}. Abortando.")
                return False
            wait = max(PAUSE, 0.5) * (2 ** attempt)
            logger.warning(f"Timed out en {description}. Reintento {attempt}/{max_retries} en {wait:.1f}s …")
            await asyncio.sleep(wait)

# -------------------------------------------------------
# Publicar todos los borradores pendientes en orden
# -------------------------------------------------------
async def _publicar_todo(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int, int]:
    rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    total = len(rows)
    if not rows:
        return 0, 0, 0

    publicados, fallidos, descartados = 0, 0, 0
    enviados_ids = []

    for mid, _t, raw in rows:
        # parse del raw
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        try:
            # ---- Encuesta: RECONSTRUIR ----
            if "poll" in data:
                kwargs, _ = _poll_payload_from_raw(data)
                ok = await _send_with_guard(
                    lambda: context.bot.send_poll(**kwargs),
                    description=f"send_poll(mid={mid})"
                )
                if ok:
                    publicados += 1
                    enviados_ids.append(mid)
                else:
                    fallidos += 1

            # ---- Resto: copiar tal cual ----
            else:
                async def do_copy():
                    return await context.bot.copy_message(
                        chat_id=TARGET_CHAT_ID,
                        from_chat_id=SOURCE_CHAT_ID,
                        message_id=mid
                    )

                ok = await _send_with_guard(do_copy, description=f"copy_message(mid={mid})")
                if ok:
                    publicados += 1
                    enviados_ids.append(mid)
                else:
                    fallidos += 1

        except BadRequest as e:
            # 400 típicos: "Message to copy not found" -> lo quitamos de la cola
            emsg = str(e)
            if "Message to copy not found" in emsg:
                logger.error(f"BadRequest: original {mid} ya no existe. Se elimina de la cola.")
                delete_draft(DB_FILE, mid)
                descartados += 1
            else:
                logger.error(f"BadRequest publicando {mid}: {e}")
                fallidos += 1

        except (Forbidden,) as e:
            fallidos += 1
            logger.error(f"Forbidden publicando {mid}: {e}")

        except TelegramError as e:
            fallidos += 1
            logger.error(f"TelegramError publicando {mid}: {e}")

        except Exception as e:
            fallidos += 1
            logger.exception(f"Error publicando {mid}: {e}")

    if enviados_ids:
        mark_sent(DB_FILE, enviados_ids)
    return publicados, fallidos, descartados

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
    is_command = txt.startswith("/")

    # --------- COMANDOS ----------
    if is_command:
        low = txt.lower()

        # /listar
        if low.startswith("/listar") or low.startswith("/lista"):
            drafts = list_drafts(DB_FILE)
            if not drafts:
                await context.bot.send_message(SOURCE_CHAT_ID, "📂 No hay borradores.")
                return
            out = ["📋 Borradores pendientes:"]
            for i, (did, snip) in enumerate(drafts, start=1):
                s = (snip or "")
                if len(s) > 80:
                    s = s[:80] + "…"
                out.append(f"• #{i} — {s}  (id:{did})")
            await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))
            return

        # /remover | /borrar | /eliminar | /remove
        if (low.startswith("/remover") or low.startswith("/borrar")
            or low.startswith("/eliminar") or low.startswith("/remove")):

            # 1) si es reply, usamos ese id
            rid = msg.reply_to_message.message_id if msg.reply_to_message else None

            # 2) o si pasó un número/id:NUM
            if rid is None:
                rid = _parse_id_arg(txt)

            if rid is None:
                await context.bot.send_message(
                    SOURCE_CHAT_ID,
                    "❌ Usa: responde a un mensaje con /remover, o /remover <id> (también 'id:123')."
                )
                return

            delete_draft(DB_FILE, int(rid))
            remaining = len(list_drafts(DB_FILE))
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                f"🗑️ Eliminado id:{rid}. Quedan {remaining} en la cola."
            )
            return

        # /enviar
        if low.startswith("/enviar") or low.startswith("/enviar_casos_clinicos"):
            total_antes = len(list_drafts(DB_FILE))
            ok, fail, skipped = await _publicar_todo(context)
            msg_out = f"✅ Publicados {ok}."
            msg_out += f"\n📦 Resultado: {ok}/{total_antes} enviados."
            if skipped:
                msg_out += f"\n♻️ Descartados (faltaba original): {skipped}."
            if fail:
                msg_out += f"\n⚠️ Fallidos: {fail}."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            return

        # /programar YYYY-MM-DD HH:MM
        if low.startswith("/programar"):
            parts = txt.split()
            if len(parts) < 3:
                await context.bot.send_message(SOURCE_CHAT_ID, "⏰ Usa: /programar YYYY-MM-DD HH:MM")
                return
            try:
                when_dt = datetime.strptime(parts[1] + " " + parts[2], "%Y-%m-%d %H:%M")
                # segundos desde ahora (si ya pasó, será 0 → ejecuta inmediato)
                seconds = max(0, int((when_dt - datetime.now()).total_seconds()))

                async def job(ctx: ContextTypes.DEFAULT_TYPE):
                    total_prev = len(list_drafts(DB_FILE))
                    ok, fail, skipped = await _publicar_todo(ctx)
                    msg2 = f"⏱️ Programación ejecutada. Publicados {ok}."
                    msg2 += f"\n📦 Resultado: {ok}/{total_prev} enviados."
                    if skipped:
                        msg2 += f"\n♻️ Descartados: {skipped}."
                    if fail:
                        msg2 += f"\n⚠️ Fallidos: {fail}."
                    await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)

                # PTB acepta float de segundos directamente
                context.job_queue.run_once(job, when=seconds)
                await context.bot.send_message(SOURCE_CHAT_ID, f"🗓️ Programado para {when_dt:%Y-%m-%d %H:%M}.")
            except ValueError:
                await context.bot.send_message(SOURCE_CHAT_ID, "❌ Formato inválido. Ej: /programar 2025-08-20 07:00")
            except Exception as e:
                logger.exception("Error al programar", exc_info=e)
                await context.bot.send_message(SOURCE_CHAT_ID, "❌ No pude programar. Revisa el formato e inténtalo de nuevo.")
            return

        # /id
        if low.startswith("/id"):
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                f"BORRADOR: `{SOURCE_CHAT_ID}`\nPRINCIPAL: `{TARGET_CHAT_ID}`",
                parse_mode="Markdown"
            )
            return

        # /ayuda o /start
        if low.startswith("/ayuda") or low.startswith("/start"):
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                "Comandos:\n"
                "• /listar — muestra borradores (con id)\n"
                "• /remover <id> — elimina de la cola (o responde a un mensaje con /remover)\n"
                "• /enviar — publica ahora\n"
                "• /programar YYYY-MM-DD HH:MM — programa el envío\n"
                "• /id — muestra IDs\n"
                f"• Pausa actual: {PAUSE:.2f}s entre mensajes"
            )
            return

        # Cualquier otro comando desconocido: NO guardar, avisar breve
        await context.bot.send_message(SOURCE_CHAT_ID, "🤖 Comando no reconocido. Usa /ayuda.")
        return  # <- importante: NO guardar comandos

    # --------- SI NO ES COMANDO → GUARDAR BORRADOR ----------
    snippet = _snippet_from_msg(msg)
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

    # Registrar error handler
    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
