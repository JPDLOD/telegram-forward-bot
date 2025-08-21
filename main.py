# -*- coding: utf-8 -*-
# BORRADOR (SOURCE_CHAT_ID) -> PRINCIPAL (TARGET_CHAT_ID)
# Guarda todo lo que publiques en BORRADOR y, al usar /enviar o /programar,
# lo publica en PRINCIPAL en el MISMO ORDEN, sin "Forwarded from...".
# Reconstruye encuestas (quiz/regular) y copia el resto de mensajes.
#
# Comandos en el canal BORRADOR:
#   /listar
#   /eliminar <id>  (alias: /borrar, /remover, /delete) o responder con el comando
#   /deshacer [id]  (alias: /undo, /restaurar) o responder con el comando
#   /enviar
#   /programar YYYY-MM-DD HH:MM
#   /id
#   /comandos  (alias: /ayuda, /start)
#
# NOTA: Los mensajes que empiecen por "/" NO se guardan como borradores.

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Tuple, Optional

from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.error import (
    TelegramError, Forbidden, BadRequest, RetryAfter, TimedOut, NetworkError
)

from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, list_drafts,
    mark_deleted, restore_draft, get_last_deleted
)

# =========================
# CONFIG DESDE ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # obligatorio
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", "-1002859784457"))
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "-1002679848195"))
DB_FILE = "drafts.db"

# pausa base entre envíos (seg) para no rozar el flood control
PAUSE = float(os.environ.get("PAUSE", "0.6"))
TZNAME = os.environ.get("TIMEZONE", "UTC")
TZ = ZoneInfo(TZNAME)

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DB =========
init_db(DB_FILE)
logger.info(f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  PRINCIPAL={TARGET_CHAT_ID}  TZ={TZNAME}  PAUSE={PAUSE}s")


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

    # tipo & opción correcta
    ptype = str(p.get("type", "regular")).lower()
    coi = p.get("correct_option_id", None)
    is_quiz = (ptype == "quiz") and isinstance(coi, int)

    kwargs = dict(
        chat_id=TARGET_CHAT_ID,
        question=question,
        options=options,
        is_anonymous=is_anon,
    )

    # Para quiz: forzar type="quiz" y pasar correct_option_id.
    # NO mandar allows_multiple_answers (lo prohíbe Telegram en quiz).
    if is_quiz:
        kwargs["type"] = "quiz"
        kwargs["correct_option_id"] = int(coi)
        if p.get("explanation"):
            kwargs["explanation"] = str(p["explanation"])
    else:
        # Para encuestas regulares, sí podemos respetar multiple answers.
        allows_multiple = bool(p.get("allows_multiple_answers", False))
        kwargs["allows_multiple_answers"] = allows_multiple

    # Tiempos: para evitar 400 Bad Request, no enviamos ambos.
    # Son opcionales, así que por estabilidad los omitimos (se pueden reactivar si lo necesitas).
    # if p.get("open_period") is not None:
    #     try:
    #         op = int(p["open_period"])
    #         if 5 <= op <= 600:
    #             kwargs["open_period"] = op
    #     except Exception:
    #         pass
    # elif p.get("close_date") is not None:
    #     try:
    #         kwargs["close_date"] = int(p["close_date"])
    #     except Exception:
    #         pass

    return kwargs, is_quiz


async def _safe_sleep(seconds: float):
    try:
        await asyncio.sleep(max(0.0, seconds))
    except Exception:
        pass


async def _send_with_backoff(send_coro_func, *, base_pause: float) -> bool:
    """
    Ejecuta la corrutina de envío con control de flood y reintentos.
    Devuelve True si se envió; False si definitivamente falló.
    """
    tries = 0
    while True:
        try:
            await send_coro_func()
            await _safe_sleep(base_pause)
            return True
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
                # Mensaje de texto tipo "Retry in X seconds"
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 3
            logger.warning(f"RetryAfter: esperando {wait}s …")
            await _safe_sleep(wait + 1.0)
            tries += 1
        except TimedOut:
            logger.warning("TimedOut: esperando 3s …")
            await _safe_sleep(3.0)
            tries += 1
        except NetworkError:
            logger.warning("NetworkError: esperando 3s …")
            await _safe_sleep(3.0)
            tries += 1
        except TelegramError as e:
            # Flood genérico
            if "Flood control exceeded" in str(e):
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 5
                logger.warning(f"Flood control: esperando {wait}s …")
                await _safe_sleep(wait + 1.0)
                tries += 1
            else:
                logger.error(f"TelegramError no recuperable: {e}")
                return False
        except Exception as e:
            logger.exception(f"Error enviando: {e}")
            return False

        if tries >= 5:
            logger.error("Demasiados reintentos; abandono este mensaje.")
            return False


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

        ok = False
        # ---- Encuesta: RECONSTRUIR ----
        if "poll" in data:
            kwargs, _is_quiz = _poll_payload_from_raw(data)

            async def _do():
                await context.bot.send_poll(**kwargs)

            ok = await _send_with_backoff(_do, base_pause=PAUSE)

        # ---- Resto: copiar tal cual ----
        else:
            async def _do():
                await context.bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=mid
                )
            ok = await _send_with_backoff(_do, base_pause=PAUSE)

        if ok:
            publicados += 1
            enviados_ids.append(mid)
        else:
            fallidos += 1

    if enviados_ids:
        mark_sent(DB_FILE, enviados_ids)
    return publicados, fallidos


# -------------------------------------------------------
# Utilidades de comandos
# -------------------------------------------------------
def _extract_id_from_text(txt: str) -> Optional[int]:
    parts = (txt or "").split()
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
        if p.lower().startswith("id:"):
            n = p.split(":", 1)[1]
            if n.isdigit():
                return int(n)
    return None


async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts = list_drafts(DB_FILE)  # [(id, text)]
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


async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    # por id explícito
    mid = _extract_id_from_text(txt)

    # o por reply
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id

    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    mark_deleted(DB_FILE, mid)
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"🗑️ Eliminado id:{mid}. Quedan {restantes} en la cola.")


async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "ℹ️ No hay nada para deshacer.")
        return

    restore_draft(DB_FILE, mid)
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.")


async def _cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    # when_str: "YYYY-MM-DD HH:MM"
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Formato inválido. Ej: /programar 2025-08-20 07:00")
        return

    now = datetime.now(tz=TZ)
    seconds = max(0, int((when - now).total_seconds()))

    if not context.job_queue:
        # Necesitas instalar el extra del job-queue
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ No pude programar. Falta JobQueue. Asegúrate de usar "
            "`python-telegram-bot[job-queue]` en requirements.txt",
            parse_mode="Markdown"
        )
        return

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        ok, fail = await _publicar_todo(ctx)
        msg2 = f"⏱️ Programación ejecutada. Publicados {ok}."
        if fail:
            msg2 += f" Fallidos: {fail}."
        await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)

    context.job_queue.run_once(lambda ctx: asyncio.create_task(job(ctx)), when=seconds)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🗓️ Programado para {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}).")


def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))


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

    # --------- COMANDOS (NO SE GUARDAN COMO BORRADOR) ----------
    if _is_command_text(txt):
        low = txt.lower()

        if low.startswith("/listar") or low.startswith("/lista"):
            await _cmd_listar(context)
            return

        if low.startswith(("/eliminar", "/borrar", "/remover", "/delete", "/remove")):
            await _cmd_eliminar(update, context, txt)
            return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt)
            return

        if low.startswith("/enviar"):
            ok, fail = await _publicar_todo(context)
            msg_out = f"✅ Publicados {ok}."
            msg_out += f"\n📦 Resultado: {ok}/{ok+fail} enviados."
            if fail:
                msg_out += " ⚠️ Revisa permisos y flood control."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            return

        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await _cmd_programar(context, when_str)
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /programar YYYY-MM-DD HH:MM")
            return

        if low.startswith("/id"):
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                f"BORRADOR: `{SOURCE_CHAT_ID}`\nPRINCIPAL: `{TARGET_CHAT_ID}`",
                parse_mode="Markdown"
            )
            return

        if low.startswith(("/comandos", "/ayuda", "/start")):
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                "🛠️ Comandos:\n"
                "• /listar — muestra borradores pendientes\n"
                "• /eliminar <id> — o responde con /eliminar al mensaje\n"
                "• /deshacer [id] — revierte un /eliminar (o responde)\n"
                "• /enviar — publica ahora\n"
                "• /programar YYYY-MM-DD HH:MM — programa el envío\n"
                "• /id — muestra IDs\n"
            )
            return

        # Comando desconocido: simplemente ignora o avisa
        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        return

    # --------- SI NO ES COMANDO → GUARDAR BORRADOR ----------
    # snippet: usa texto o caption; si queda vacío, igual guardamos (imágenes/documentos)
    snippet = msg.text or msg.caption or ""
    raw_json = json.dumps(msg.to_dict(), ensure_ascii=False)
    save_draft(DB_FILE, msg.message_id, snippet, raw_json)
    logger.info(f"Guardado en borrador: {msg.message_id}")


# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)


# ========= MAIN =========
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # En canales se usa MessageHandler con ChatType.CHANNEL
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))

    # Registrar error handler
    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
