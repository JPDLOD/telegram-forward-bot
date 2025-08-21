# -*- coding: utf-8 -*-
# BORRADOR (SOURCE_CHAT_ID) -> PRINCIPAL (TARGET_CHAT_ID)
# Guarda todo lo que publiques en BORRADOR y, al usar /enviar o /programar,
# lo publica en PRINCIPAL en el MISMO ORDEN, sin "Forwarded from...".
# Reconstruye encuestas (quiz/regular) y copia el resto de mensajes.
#
# Comandos en el canal BORRADOR:
#   /listar
#   /cancelar <id>  (o responde con /cancelar)  ← quita de la cola sin borrar el mensaje del canal
#   /deshacer [id]  (o responde)                ← revierte un /cancelar
#   /eliminar <id>  (alias: /delete, /remove, /borrar)  ← BORRA del canal y lo quita de la cola
#   /nuke <patrón>  ← borra del CANAL los pendientes según patrón (ver ayuda)
#   /enviar
#   /programar YYYY-MM-DD HH:MM
#   /id [id]        ← info del mensaje/ID; si respondes a un mensaje, te dice su ID
#   /canales        ← muestra los IDs del canal borrador y el principal
#   /comandos (alias: /comando, /ayuda, /start)
#
# NOTA: Los mensajes que empiecen por "/" NO se guardan como borradores.

import os
import re
import json
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Tuple, Optional, List

from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, CallbackQueryHandler, filters
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

# ========= CONTADORES (para distinguir en /enviar) =========
_STATS = {"cancelados": 0, "eliminados": 0}


# -------------------------------------------------------
# helpers de BD locales (no tocan database.py)
# -------------------------------------------------------
def _hard_delete_draft(db_file: str, mid: int) -> None:
    """Borra DEFINITIVAMENTE el borrador de la tabla (usado por /eliminar y /nuke)."""
    try:
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("DELETE FROM drafts WHERE message_id = ?", (mid,))
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass


def _get_draft_row(db_file: str, mid: int):
    """Devuelve (message_id, text, raw_json, sent, deleted, created_at) o None."""
    try:
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT message_id, text, raw_json, sent, deleted, created_at FROM drafts WHERE message_id = ?", (mid,))
        row = cur.fetchone()
        return row
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _get_pending_ids(db_file: str) -> List[int]:
    """IDs pendientes (no enviados y no cancelados) en orden de inserción."""
    try:
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT message_id FROM drafts WHERE sent = 0 AND deleted = 0 ORDER BY created_at ASC")
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


# -------------------------------------------------------
# helpers de encuestas
# -------------------------------------------------------
def _poll_payload_from_raw(raw: dict) -> Tuple[dict, bool]:
    """
    Extrae un payload listo para send_poll a partir de raw['poll'].
    Devuelve (kwargs, is_quiz).
    """
    p = raw.get("poll") or {}
    question = p.get("question", "Pregunta")
    options_src = p.get("options", []) or []
    options  = [o.get("text", "") for o in options_src]

    is_anon  = p.get("is_anonymous", True)
    allows_multiple = p.get("allows_multiple_answers", False)
    ptype = (p.get("type") or "regular").lower().strip()
    is_quiz = (ptype == "quiz")

    kwargs = dict(
        chat_id=TARGET_CHAT_ID,
        question=question,
        options=options,
        is_anonymous=is_anon,
    )

    # Solo incluir allows_multiple si NO es quiz (Telegram rechaza quiz + multiple)
    if not is_quiz:
        kwargs["allows_multiple_answers"] = bool(allows_multiple)

    # Quiz: respuesta correcta
    if is_quiz:
        kwargs["type"] = "quiz"
        cid = p.get("correct_option_id")
        try:
            cid = int(cid) if cid is not None else None
        except Exception:
            cid = None

        # Asegurar que esté dentro del rango, si no, forzar 0
        if cid is None or cid < 0 or cid >= len(options):
            cid = 0
        kwargs["correct_option_id"] = cid

    # Tiempos (si existieran) – Telegram no permite open_period y close_date juntos
    if p.get("open_period") is not None and p.get("close_date") is None:
        try:
            kwargs["open_period"] = int(p["open_period"])
        except Exception:
            pass
    elif p.get("close_date") is not None:
        try:
            kwargs["close_date"] = int(p["close_date"])
        except Exception:
            pass

    # Explicación (solo quiz)
    if is_quiz and p.get("explanation"):
        kwargs["explanation"] = str(p["explanation"])

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


def _deep_link_for_channel_message(chat_id: int, mid: int) -> str:
    # Para canales privados: https://t.me/c/<chatid_sin_-100>/<id>
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"


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


async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """Quita de la cola sin borrar el mensaje del canal."""
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    _STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.")


async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """BORRA del canal y lo quita de la cola definitivamente."""
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    # Borrar en Telegram
    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
    except TelegramError as e:
        ok_del = False
        logger.warning(f"No pude borrar en el canal id:{mid} → {e}")

    # Quitar de la base
    _hard_delete_draft(DB_FILE, mid)
    _STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await context.bot.send_message(SOURCE_CHAT_ID, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.")


async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """Revierte /cancelar. (No aplica a /eliminar porque ya no existe el mensaje en Telegram)."""
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "ℹ️ No hay nada para deshacer.")
        return

    restore_draft(DB_FILE, mid)
    # Si estaba contado como cancelado en esta sesión, decrementamos visualmente
    if _STATS["cancelados"] > 0:
        _STATS["cancelados"] -= 1
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
        extra = []
        if _STATS["cancelados"]:
            extra.append(f"Cancelados: {_STATS['cancelados']}")
        if _STATS["eliminados"]:
            extra.append(f"Eliminados: {_STATS['eliminados']}")
        if fail:
            extra.append(f"Fallidos: {fail}")
        if extra:
            msg2 += " " + " · ".join(extra) + "."
        await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)
        # reset contadores
        _STATS["cancelados"] = 0
        _STATS["eliminados"] = 0

    context.job_queue.run_once(lambda ctx: asyncio.create_task(job(ctx)), when=seconds)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🗓️ Programado para {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}).")


async def _cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """/id [id] — info del mensaje; o si respondes, devuelve su ID."""
    # Si viene por reply sin parámetro → devolvemos ID rápido
    if update.channel_post and update.channel_post.reply_to_message and len((txt or "").split()) == 1:
        rid = update.channel_post.reply_to_message.message_id
        await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
        return

    # Con número explícito
    mid = _extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
        return
    mid = int(mid)

    row = _get_draft_row(DB_FILE, mid)
    # Preparar snippet y tipo
    snippet = "[contenido]"
    tipo = "desconocido"
    fecha = ""
    if row:
        try:
            _, text, raw_json, _sent, _deleted, created_at = row
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
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at)
                    fecha = dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    fecha = created_at
        except Exception:
            pass

    link = _deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
    out = f"🆔 {mid}\n• Tipo: {tipo}\n• Snippet: {snippet}\n• Fecha: {fecha}\n• Enlace: {link}"
    await context.bot.send_message(SOURCE_CHAT_ID, out)


async def _cmd_canales(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        SOURCE_CHAT_ID,
        f"BORRADOR: `{SOURCE_CHAT_ID}`\nPRINCIPAL: `{TARGET_CHAT_ID}`",
        parse_mode="Markdown"
    )


def _parse_nuke_pattern(arg: str, ordered_ids: List[int]) -> List[int]:
    """
    Convierte un patrón como:
      - "all"
      - "1,3,5,7"
      - "1-10"
      - combinación: "1-3,7,12-15"
    a una lista de message_ids a borrar (según orden de /listar).
    """
    arg = (arg or "").strip().lower()
    if not arg:
        return []

    if arg == "all":
        return ordered_ids[:]

    result_idx: List[int] = []
    parts = [p.strip() for p in arg.split(",") if p.strip()]
    for tok in parts:
        if "-" in tok:
            a, b = tok.split("-", 1)
            if a.isdigit() and b.isdigit():
                ai, bi = int(a), int(b)
                if ai <= bi:
                    result_idx.extend(list(range(ai, bi + 1)))
                else:
                    result_idx.extend(list(range(bi, ai + 1)))
        elif tok.isdigit():
            result_idx.append(int(tok))

    # Normalizar a límites válidos y únicos, preservando orden
    max_idx = len(ordered_ids)
    seen = set()
    final_ids: List[int] = []
    for i in result_idx:
        if 1 <= i <= max_idx:
            if i not in seen:
                seen.add(i)
                final_ids.append(ordered_ids[i - 1])
    return final_ids


async def _cmd_nuke(context: ContextTypes.DEFAULT_TYPE, txt: str):
    """
    /nuke <patrón>
      all             → borra TODOS los pendientes
      1,3,5,7         → borra esos índices
      1-10            → borra del 1 al 10
      1-3,7,12-15     → combina rangos y elementos
    """
    parts = (txt or "").split(maxsplit=1)
    if len(parts) < 2:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "Usa: /nuke <patrón>\n"
            "Ejemplos: /nuke all · /nuke 1-10 · /nuke 1,3,5,7 · /nuke 1-3,7,12-15"
        )
        return

    # ids pendientes en el mismo orden que /listar
    pending_rows = list_drafts(DB_FILE)  # [(id, text)]
    ordered_ids = [r[0] for r in pending_rows]
    if not ordered_ids:
        await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.")
        return

    victims = _parse_nuke_pattern(parts[1], ordered_ids)
    if not victims:
        await context.bot.send_message(SOURCE_CHAT_ID, "Patrón sin coincidencias.")
        return

    borrados = 0
    for mid in victims:
        try:
            await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
        except TelegramError as e:
            logger.warning(f"No pude borrar en el canal id:{mid} → {e}")
        _hard_delete_draft(DB_FILE, mid)
        borrados += 1

    _STATS["eliminados"] += borrados
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(
        SOURCE_CHAT_ID,
        f"💣 Nuke: {borrados} borrados según patrón. Quedan {restantes} en la cola."
    )


def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))


# -------------------------------------------------------
# Ayuda con botones "clickeables" para canal
# -------------------------------------------------------
async def _send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛠️ Comandos:\n"
        "• /listar — muestra borradores pendientes\n"
        "• /cancelar <id> — o responde con /cancelar (no borra del canal)\n"
        "• /deshacer [id] — revierte un /cancelar (o responde)\n"
        "• /eliminar <id> — o responde (BORRA del canal y de la cola)\n"
        "• /nuke <patrón> — borra del canal pendientes según patrón (all, 1-10, 1,3,5,7)\n"
        "• /enviar — publica ahora\n"
        "• /programar YYYY-MM-DD HH:MM — programa el envío\n"
        "• /id [id] — info del mensaje o, si respondes, te dice el ID\n"
        "• /canales — muestra IDs del BORRADOR y PRINCIPAL\n"
        "• /comandos — muestra este panel de ayuda\n"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Listar", callback_data="do:listar"),
             InlineKeyboardButton("📦 Enviar", callback_data="do:enviar")],
            [InlineKeyboardButton("🗓️ Programar", callback_data="do:programar"),
             InlineKeyboardButton("🛠️ Comandos", callback_data="do:help")]
        ]
    )
    await context.bot.send_message(SOURCE_CHAT_ID, text, reply_markup=kb)


# -------------------------------------------------------
# Handler de callbacks (botones)
# -------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    try:
        if data == "do:listar":
            await _cmd_listar(context)
        elif data == "do:enviar":
            ok, fail = await _publicar_todo(context)
            msg_out = f"✅ Publicados {ok}."
            extras = []
            if _STATS["cancelados"]:
                extras.append(f"Cancelados: {_STATS['cancelados']}")
            if _STATS["eliminados"]:
                extras.append(f"Eliminados: {_STATS['eliminados']}")
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            _STATS["cancelados"] = 0
            _STATS["eliminados"] = 0
        elif data == "do:programar":
            await context.bot.send_message(
                SOURCE_CHAT_ID,
                "Formato: `/programar YYYY-MM-DD HH:MM`\n"
                "Ejemplos:\n"
                "• `/programar 2025-08-20 07:00`\n"
                "• `/programar 2025-08-21 17:30`",
                parse_mode="Markdown"
            )
        elif data == "do:help":
            await _send_help_with_buttons(context)
    except Exception as e:
        logger.exception(f"Error en callback: {e}")


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

        # listar
        if low.startswith("/listar") o r low.startswith("/lista"):
            await _cmd_listar(context)
            return

        # cancelar (antes /eliminar lógico)
        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt)
            return

        # eliminar (borrar del canal + cola)
        if low.startswith(("/eliminar", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt)
            return

        # deshacer
        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt)
            return

        # nuke (ahora por patrón)
        if low.startswith("/nuke"):
            await _cmd_nuke(context, txt)
            return

        # enviar
        if low.startswith("/enviar"):
            ok, fail = await _publicar_todo(context)
            msg_out = f"✅ Publicados {ok}."
            # Añadimos desglose:
            extras = []
            if _STATS["cancelados"]:
                extras.append(f"Cancelados: {_STATS['cancelados']}")
            if _STATS["eliminados"]:
                extras.append(f"Eliminados: {_STATS['eliminados']}")
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            # reset contadores
            _STATS["cancelados"] = 0
            _STATS["eliminados"] = 0
            return

        # programar
        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            if len(parts) >= 3:
                when_str = f"{parts[1]} {parts[2]}"
                await _cmd_programar(context, when_str)
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /programar YYYY-MM-DD HH:MM")
            return

        # id (info de mensaje) – la versión de IDs de canales pasa a /canales
        if low.startswith("/id"):
            await _cmd_id(update, context, txt)
            return

        # canales → IDs de chats
        if low.startswith("/canales"):
            await _cmd_canales(context)
            return

        # comandos/ayuda/start
        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await _send_help_with_buttons(context)
            return

        # Comando desconocido
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
    # Botones de ayuda (callback)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Registrar error handler
    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
