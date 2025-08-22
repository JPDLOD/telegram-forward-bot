# -*- coding: utf-8 -*-
# BORRADOR (SOURCE_CHAT_ID) -> PRINCIPAL (TARGET_CHAT_ID) (+ BACKUP opcional)
# Guarda todo lo que publiques en BORRADOR y, al usar /enviar o /programar,
# lo publica en PRINCIPAL (y BACKUP si está ON) en el MISMO ORDEN, sin "Forwarded from...".
# Reconstruye encuestas (quiz/regular) y copia el resto de mensajes.
#
# Comandos en el canal BORRADOR:
#   /listar
#   /cancelar <id>  (o responde con /cancelar)  ← quita de la cola sin borrar el mensaje del canal
#   /deshacer [id]  (o responde)                ← revierte un /cancelar
#   /eliminar <id>  (alias: /del, /delete, /remove, /borrar)  ← BORRA del canal y lo quita de la cola
#   /nuke <…>       ← ver ayuda en /comandos (all/todos, 1,3,5, 1-10, N últimos)
#   /enviar
#   /preview        ← envía la cola a PREVIEW sin marcar como enviada
#   /undo_send      ← borra en targets el último lote que envió el bot
#   /programar YYYY-MM-DD HH:MM
#   /id [id]        ← info del mensaje/ID; si respondes a un mensaje, te dice su ID
#   /canales        ← IDs + estado de targets (alias: /targets, /where)
#   /backup on|off  ← alterna SOLO el backup (principal siempre ON)
#   /comandos (alias: /comando, /ayuda, /start)
#
# NOTA: Los mensajes que empiecen por "/" NO se guardan como borradores.

import os
import re
import json
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Tuple, Optional, List, Set, Dict

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

# Fallbacks por si no pones ENV (así no se rompe al actualizar)
BACKUP_FALLBACK = -1002717125281   # reemplaza por tu backup real si quieres
PREVIEW_FALLBACK = -1003042227035  # reemplaza por tu preview real si quieres

BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID", str(BACKUP_FALLBACK)))
PREVIEW_CHAT_ID = int(os.environ.get("PREVIEW_CHAT_ID", str(PREVIEW_FALLBACK)))

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
logger.info(
    f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  PRINCIPAL={TARGET_CHAT_ID}  BACKUP={BACKUP_CHAT_ID}  "
    f"PREVIEW={PREVIEW_CHAT_ID}  TZ={TZNAME}  PAUSE={PAUSE}s"
)

# ========= ESTADO DE TARGETS =========
# Principal SIEMPRE ON (no se apaga para evitar accidentes)
_ACTIVE_BACKUP = True  # por defecto ON cada vez que inicia el proceso

def get_active_targets() -> List[int]:
    targets = [TARGET_CHAT_ID]
    if _ACTIVE_BACKUP and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

# ========= CONTADORES (para distinguir en /enviar) =========
_STATS = {"cancelados": 0, "eliminados": 0}

# ========= HISTÓRICO ÚLTIMO LOTE ENVIADO (para /undo_send) =========
# Guarda los message_id publicados en cada target en el último /enviar o /programar ejecutado
_LAST_BATCH: Dict[int, List[int]] = {}  # {chat_id_destino: [message_ids publicados allá]}


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
    Extrae un payload *base* listo para send_poll a partir de raw['poll'] (sin chat_id).
    Devuelve (kwargs_parciales, is_quiz).
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


async def _send_with_backoff(func_coro_factory, *, base_pause: float):
    """
    Ejecuta la corrutina de envío con control de flood y reintentos.
    `func_coro_factory` debe ser un lambda que, al llamarse, devuelva la corrutina de envío.
    Devuelve (ok: bool, result: Message|None)
    """
    tries = 0
    while True:
        try:
            msg = await func_coro_factory()
            await _safe_sleep(base_pause)
            return True, msg
        except RetryAfter as e:
            wait = getattr(e, "retry_after", None)
            if wait is None:
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
            if "Flood control exceeded" in str(e):
                m = re.search(r"Retry in (\d+)", str(e))
                wait = int(m.group(1)) if m else 5
                logger.warning(f"Flood control: esperando {wait}s …")
                await _safe_sleep(wait + 1.0)
                tries += 1
            else:
                logger.error(f"TelegramError no recuperable: {e}")
                return False, None
        except Exception as e:
            logger.exception(f"Error enviando: {e}")
            return False, None

        if tries >= 5:
            logger.error("Demasiados reintentos; abandono este mensaje.")
            return False, None


# -------------------------------------------------------
# Publicar borradores pendientes en uno o varios destinos
# -------------------------------------------------------
async def _publicar(context: ContextTypes.DEFAULT_TYPE, *, targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    """
    Envía la cola a los `targets` dados.
    mark_as_sent=True → marca en DB como enviados (para PRINCIPAL/operaciones reales).
    Devuelve (publicados, fallidos, posted_by_target) donde posted_by_target es {chat_id: [mids]}
    """
    rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not rows:
        return 0, 0, {t: [] for t in targets}

    publicados, fallidos = 0, 0
    enviados_ids = []
    posted_by_target: Dict[int, List[int]] = {t: [] for t in targets}

    for mid, _t, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        any_success = False

        for dest in targets:
            if "poll" in data:
                base_kwargs, _is_quiz = _poll_payload_from_raw(data)
                kwargs = dict(base_kwargs)
                kwargs["chat_id"] = dest

                coro_factory = lambda k=kwargs: context.bot.send_poll(**k)
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)
            else:
                # copia tal cual
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)

            if ok:
                any_success = True
                publicados += 1  # contamos por mensaje y por target (si quieres contarlo una vez, comenta esta línea)
                if msg and getattr(msg, "message_id", None):
                    posted_by_target[dest].append(msg.message_id)
            else:
                fallidos += 1

        if any_success and mark_as_sent:
            enviados_ids.append(mid)

    if enviados_ids and mark_as_sent:
        mark_sent(DB_FILE, enviados_ids)

    return publicados, fallidos, posted_by_target


# -------------------------------------------------------
# Enviar a targets activos (PRINCIPAL + BACKUP si ON)
# -------------------------------------------------------
async def _publicar_todo_activos(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int]:
    publicados, fallidos, posted = await _publicar(
        context, targets=get_active_targets(), mark_as_sent=True
    )
    # Guardamos último lote para /undo_send
    global _LAST_BATCH
    _LAST_BATCH = posted
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
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ No pude programar. Falta JobQueue. Asegúrate de usar "
            "`python-telegram-bot[job-queue]` en requirements.txt",
            parse_mode="Markdown"
        )
        return

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        ok, fail = await _publicar_todo_activos(ctx)
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
        _STATS["cancelados"] = 0
        _STATS["eliminados"] = 0

    context.job_queue.run_once(lambda ctx: asyncio.create_task(job(ctx)), when=seconds)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🗓️ Programado para {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}).")


async def _cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    """/id [id] — info del mensaje; o si respondes, devuelve su ID."""
    if update.channel_post and update.channel_post.reply_to_message and len((txt or "").split()) == 1:
        rid = update.channel_post.reply_to_message.message_id
        await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
        return

    mid = _extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
        return
    mid = int(mid)

    row = _get_draft_row(DB_FILE, mid)
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


def _parse_nuke_selection(arg: str, drafts: List[Tuple[int, str]]) -> Set[int]:
    """
    Convierte una selección textual basada en posiciones de /listar a IDs de mensajes.
    Soporta:
      - 'all' / 'todos' → todos
      - '1,3,5' → lista de posiciones
      - '2-7' → rango
      - número simple 'N' → interpreta como 'últimos N'
    """
    arg = (arg or "").strip().lower()
    ids_in_order = [did for (did, _snip) in drafts]  # orden /listar (ASC por created_at)
    result: Set[int] = set()

    if not arg:
        return result

    if arg in ("all", "todos"):
        result.update(ids_in_order)
        return result

    if arg.isdigit():
        n = int(arg)
        if n > 0:
            result.update(ids_in_order[-n:])
        return result

    pieces = [p.strip() for p in arg.split(",") if p.strip()]
    for p in pieces:
        if re.fullmatch(r"\d+-\d+", p):
            a, b = p.split("-")
            a, b = int(a), int(b)
            if a <= 0 or b <= 0:
                continue
            lo, hi = min(a, b), max(a, b)
            for pos in range(lo, hi + 1):
                idx = pos - 1
                if 0 <= idx < len(ids_in_order):
                    result.add(ids_in_order[idx])
        elif p.isdigit():
            pos = int(p)
            idx = pos - 1
            if 0 <= idx < len(ids_in_order):
                result.add(ids_in_order[idx])
    return result


async def _cmd_nuke(context: ContextTypes.DEFAULT_TYPE, txt: str):
    """
    /nuke all|todos
    /nuke 1,3,5
    /nuke 1-10
    /nuke N        ← borra los últimos N (comportamiento anterior)
    """
    parts = (txt or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""

    drafts = list_drafts(DB_FILE)  # [(id, text)] en orden /listar
    if not drafts:
        await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.")
        return

    victims: Set[int] = _parse_nuke_selection(arg, drafts)
    if not victims:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "Usa: /nuke all | /nuke todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N"
        )
        return

    borrados = 0
    for mid in sorted(victims, reverse=True):
        try:
            await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
        except TelegramError as e:
            logger.warning(f"No pude borrar en el canal id:{mid} → {e}")
        _hard_delete_draft(DB_FILE, mid)
        borrados += 1

    _STATS["eliminados"] += borrados
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")


# -------------------------------------------------------
# Ayuda / panel con botones
# -------------------------------------------------------
def _targets_status_text() -> str:
    onoff = "ON" if _ACTIVE_BACKUP else "OFF"
    return (
        "📡 **Targets**\n"
        f"• Principal: `{TARGET_CHAT_ID}` **ON** (fijo)\n"
        f"• Backup   : `{BACKUP_CHAT_ID}` **{onoff}**\n"
        f"• Preview  : `{PREVIEW_CHAT_ID}`\n"
    )

async def _send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛠️ Comandos:\n"
        "• /listar — muestra borradores pendientes\n"
        "• /cancelar <id> — o responde con /cancelar (no borra del canal)\n"
        "• /deshacer [id] — revierte un /cancelar (o responde)\n"
        "• /eliminar <id> — o responde (BORRA del canal y de la cola)  [alias: /del]\n"
        "• /nuke all|todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N(últimos)\n"
        "• /enviar — publica ahora (a targets activos)\n"
        "• /preview — manda la cola a PREVIEW sin marcar como enviada\n"
        "• /undo_send — borra el último lote enviado por el bot en los targets\n"
        "• /programar YYYY-MM-DD HH:MM — programa el envío (targets activos)\n"
        "• /id [id] — info del mensaje o, si respondes, te dice el ID\n"
        "• /canales — IDs + estado de targets (alias: /targets, /where)\n"
        "• /backup on|off — alterna el backup\n"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Listar", callback_data="do:listar"),
             InlineKeyboardButton("📦 Enviar", callback_data="do:enviar")],
            [InlineKeyboardButton("🧪 Preview", callback_data="do:preview"),
             InlineKeyboardButton("⏰ Programar", callback_data="do:programar_menu")],
            [InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="do:backup_toggle"),
             InlineKeyboardButton("ℹ️ Canales", callback_data="do:canales")]
        ]
    )
    await context.bot.send_message(SOURCE_CHAT_ID, text, reply_markup=kb)


async def _send_canales_panel(context: ContextTypes.DEFAULT_TYPE):
    text = (
        _targets_status_text() +
        "\nUsa **/backup on|off** o el botón para alternar backup.\n"
        "Botones rápidos abajo 👇"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Listar", callback_data="do:listar"),
             InlineKeyboardButton("📦 Enviar", callback_data="do:enviar")],
            [InlineKeyboardButton("🧪 Preview", callback_data="do:preview"),
             InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="do:backup_toggle")],
            [InlineKeyboardButton("⏰ +15 min", callback_data="do:sched:+15"),
             InlineKeyboardButton("⏰ +1 h", callback_data="do:sched:+60")],
            [InlineKeyboardButton("⏰ Hoy 20:00", callback_data="do:sched:today2000"),
             InlineKeyboardButton("⏰ Mañana 08:00", callback_data="do:sched:tom0800")]
        ]
    )
    await context.bot.send_message(SOURCE_CHAT_ID, text, reply_markup=kb, parse_mode="Markdown")


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
            ok, fail = await _publicar_todo_activos(context)
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

        elif data == "do:preview":
            pubs, fails, _ = await _publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

        elif data == "do:programar_menu":
            await _send_canales_panel(context)

        elif data == "do:backup_toggle":
            global _ACTIVE_BACKUP
            _ACTIVE_BACKUP = not _ACTIVE_BACKUP
            await _send_canales_panel(context)

        elif data.startswith("do:sched:"):
            now = datetime.now(tz=TZ)
            when = None
            if data == "do:sched:+15":
                when = now + timedelta(minutes=15)
            elif data == "do:sched:+60":
                when = now + timedelta(hours=1)
            elif data == "do:sched:today2000":
                when = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if when <= now:
                    when = when + timedelta(days=1)
            elif data == "do:sched:tom0800":
                when = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

            if when:
                await _cmd_programar(context, when.strftime("%Y-%m-%d %H:%M"))
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "No pude calcular la hora.")
        elif data == "do:canales":
            await _send_canales_panel(context)

    except Exception as e:
        logger.exception(f"Error en callback: {e}")


# -------------------------------------------------------
# Comandos adicionales: /preview, /undo_send, /canales, /backup
# -------------------------------------------------------
async def _cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    pubs, fails, _ = await _publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

async def _cmd_undo_send(context: ContextTypes.DEFAULT_TYPE):
    if not _LAST_BATCH:
        await context.bot.send_message(SOURCE_CHAT_ID, "No hay lote previo para deshacer.")
        return
    removed_total = 0
    for dest, mids in _LAST_BATCH.items():
        for mid in reversed(mids):
            try:
                await context.bot.delete_message(chat_id=dest, message_id=mid)
                removed_total += 1
            except TelegramError as e:
                logger.warning(f"No pude borrar en {dest} mid:{mid} → {e}")
    _LAST_BATCH.clear()
    await context.bot.send_message(SOURCE_CHAT_ID, f"↩️ Undo send: {removed_total} mensajes borrados en los targets.")

async def _cmd_canales(context: ContextTypes.DEFAULT_TYPE):
    await _send_canales_panel(context)

async def _cmd_backup(context: ContextTypes.DEFAULT_TYPE, arg: str):
    global _ACTIVE_BACKUP
    v = (arg or "").strip().lower()
    if v in ("on", "1", "true", "si", "sí"):
        _ACTIVE_BACKUP = True
    elif v in ("off", "0", "false", "no"):
        _ACTIVE_BACKUP = False
    else:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
        return
    await _send_canales_panel(context)


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

        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt)
            return

        if low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt)
            return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt)
            return

        if low.startswith("/nuke"):
            await _cmd_nuke(context, txt)
            return
        if low.strip() in ("/all", "/todos"):
            await _cmd_nuke(context, "/nuke all")
            return

        if low.startswith("/enviar"):
            ok, fail = await _publicar_todo_activos(context)
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
            return

        if low.startswith("/preview"):
            await _cmd_preview(context)
            return

        if low.startswith("/undo_send"):
            await _cmd_undo_send(context)
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
            await _cmd_id(update, context, txt)
            return

        if low.startswith(("/canales", "/targets", "/where")):
            await _cmd_canales(context)
            return

        if low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await _cmd_backup(context, arg)
            return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await _send_help_with_buttons(context)
            return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
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
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
