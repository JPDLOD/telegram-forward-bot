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
#   /enviar         ← envía ahora a targets activos
#   /preview        ← envía la cola a PREVIEW sin marcar como enviada
#   /programar YYYY-MM-DD HH:MM  ← programa LO QUE HAY EN /listar (y no se mezcla con lo nuevo)
#   /programados    ← muestra programaciones pendientes y cuánto falta
#   /desprogramar <id|all>  ← cancela una programación por id o todas
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
BACKUP_FALLBACK = -1002717125281   # tu backup
PREVIEW_FALLBACK = -1003042227035  # tu preview

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
_ACTIVE_BACKUP = True  # por defecto ON cada vez que inicia el proceso

def get_active_targets() -> List[int]:
    targets = [TARGET_CHAT_ID]
    if _ACTIVE_BACKUP and BACKUP_CHAT_ID:
        targets.append(BACKUP_CHAT_ID)
    return targets

# ========= CONTADORES =========
_STATS = {"cancelados": 0, "eliminados": 0}

# ========= BLOQUEOS DE PROGRAMACIÓN (IDs programados) =========
_SCHEDULED_LOCK: Set[int] = set()

# ========= REGISTRO DE PROGRAMACIONES =========
# Guardamos programaciones pendientes: {pid: {"when": datetime, "ids": [...], "job": Job}}
_SCHEDULES: Dict[int, Dict] = {}
_SCHED_SEQ: int = 0


# -------------------------------------------------------
# helpers de BD locales (no tocan database.py)
# -------------------------------------------------------
def _hard_delete_draft(db_file: str, mid: int) -> None:
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


def _get_rows_by_ids(db_file: str, ids: List[int]) -> List[Tuple[int, str, str]]:
    """Devuelve [(message_id, text, raw_json)] solo para esos IDs que siguen pendientes."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = f"SELECT message_id, text, raw_json FROM drafts WHERE sent=0 AND deleted=0 AND message_id IN ({placeholders}) ORDER BY created_at ASC"
    try:
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute(sql, ids)
        return cur.fetchall()
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _get_pending_ids(db_file: str) -> List[int]:
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

    if not is_quiz:
        kwargs["allows_multiple_answers"] = bool(allows_multiple)

    if is_quiz:
        kwargs["type"] = "quiz"
        cid = p.get("correct_option_id")
        try:
            cid = int(cid) if cid is not None else None
        except Exception:
            cid = None
        if cid is None or cid < 0 or cid >= len(options):
            cid = 0
        kwargs["correct_option_id"] = cid

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

    if is_quiz and p.get("explanation"):
        kwargs["explanation"] = str(p["explanation"])

    return kwargs, is_quiz


# -------------------------------------------------------
# utilidades varias
# -------------------------------------------------------
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


async def _temp_notice(context: ContextTypes.DEFAULT_TYPE, text: str, ttl: int = 6):
    """Envía un aviso temporal y lo borra pasado `ttl` segundos."""
    try:
        m = await context.bot.send_message(SOURCE_CHAT_ID, text, disable_notification=True)
    except Exception:
        return
    async def _auto_del():
        await _safe_sleep(ttl)
        try:
            await context.bot.delete_message(SOURCE_CHAT_ID, m.message_id)
        except Exception:
            pass
    asyncio.create_task(_auto_del())


def _human_eta(target_dt: datetime, now: Optional[datetime] = None) -> str:
    """Texto corto tipo 'en 27 min' / 'en 1 h 15 m' / 'en 2 d 3 h'."""
    now = now or datetime.now(tz=TZ)
    sec = max(0, int((target_dt - now).total_seconds()))
    mins = sec // 60
    if mins < 60:
        return f"en {mins} min"
    hours = mins // 60
    mins = mins % 60
    if hours < 24:
        if mins:
            return f"en {hours} h {mins} m"
        return f"en {hours} h"
    days = hours // 24
    hours = hours % 24
    if hours:
        return f"en {days} d {hours} h"
    return f"en {days} d"


def _maybe_delete_command(update: Update):
    """Borra el mensaje del comando (para mantener limpio el canal)."""
    try:
        if update and update.channel_post:
            # Best-effort; el bot necesita permiso para borrar en el canal.
            update.get_bot().delete_message(chat_id=SOURCE_CHAT_ID, message_id=update.channel_post.message_id)
    except Exception:
        pass


# -------------------------------------------------------
# Publicar borradores: seleccionados o todos (excluyendo bloqueados)
# -------------------------------------------------------
async def _publicar_rows(context: ContextTypes.DEFAULT_TYPE, *, rows: List[Tuple[int, str, str]], targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    publicados = 0
    fallidos = 0
    enviados_ids: List[int] = []
    posted_by_target: Dict[int, List[int]] = {t: [] for t in targets}

    for mid, _t, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}

        any_success = False
        for dest in targets:
            if "poll" in data:
                base_kwargs, _ = _poll_payload_from_raw(data)
                kwargs = dict(base_kwargs)
                kwargs["chat_id"] = dest
                coro_factory = lambda k=kwargs: context.bot.send_poll(**k)
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)
            else:
                coro_factory = lambda d=dest, m=mid: context.bot.copy_message(
                    chat_id=d, from_chat_id=SOURCE_CHAT_ID, message_id=m
                )
                ok, msg = await _send_with_backoff(coro_factory, base_pause=PAUSE)

            if ok:
                any_success = True
                if msg and getattr(msg, "message_id", None):
                    posted_by_target[dest].append(msg.message_id)

        if any_success:
            publicados += 1
            if mark_as_sent:
                enviados_ids.append(mid)
        else:
            fallidos += 1

    if enviados_ids and mark_as_sent:
        mark_sent(DB_FILE, enviados_ids)

    return publicados, fallidos, posted_by_target


async def _publicar(context: ContextTypes.DEFAULT_TYPE, *, targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    """Envía la cola completa EXCLUYENDO los bloqueados (_SCHEDULED_LOCK)."""
    all_rows = get_unsent_drafts(DB_FILE)  # [(message_id, text, raw_json)]
    if not all_rows:
        return 0, 0, {t: [] for t in targets}
    rows = [(m, t, r) for (m, t, r) in all_rows if m not in _SCHEDULED_LOCK]
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)


async def _publicar_ids(context: ContextTypes.DEFAULT_TYPE, *, ids: List[int], targets: List[int], mark_as_sent: bool) -> Tuple[int, int, Dict[int, List[int]]]:
    rows = _get_rows_by_ids(DB_FILE, ids)
    if not rows:
        return 0, 0, {t: [] for t in targets}
    return await _publicar_rows(context, rows=rows, targets=targets, mark_as_sent=mark_as_sent)


async def _publicar_todo_activos(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int]:
    pubs, fails, _posted = await _publicar(context, targets=get_active_targets(), mark_as_sent=True)
    return pubs, fails


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
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"


def _parse_when_from_text(s: str) -> Optional[datetime]:
    """
    Extrae 'YYYY-MM-DD HH:MM' de un texto que puede tener cosas extra (p.ej. '(24h)').
    Valida 24h (00-23).
    """
    if not s:
        return None
    m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})', s)
    if not m:
        return None
    yyyy_mm_dd, hh, mm = m.group(1), m.group(2), m.group(3)
    try:
        h = int(hh)
        mnt = int(mm)
        if not (0 <= h <= 23 and 0 <= mnt <= 59):
            return None
        hh = f"{h:02d}"
        when = datetime.strptime(f"{yyyy_mm_dd} {hh}:{mm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        return when
    except Exception:
        return None


async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts = list_drafts(DB_FILE)  # [(id, text)]
    lines = []
    if not drafts:
        lines.append("📋 Borradores pendientes: 0")
    else:
        lines.append("📋 Borradores pendientes:")
        for i, (did, snip) in enumerate(drafts, start=1):
            s = (snip or "").strip()
            if len(s) > 60:
                s = s[:60] + "…"
            lines.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did})")

    # Añadir programaciones
    if not _SCHEDULES:
        lines.append("\n📑 Programaciones pendientes: 0")
    else:
        lines.append("\n📑 Programaciones pendientes:")
        now = datetime.now(tz=TZ)
        for pid, rec in sorted(_SCHEDULES.items()):
            when = rec["when"]
            eta = _human_eta(when, now)
            lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(rec['ids'])} mensajes")

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(lines))


async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        _maybe_delete_command(update)
        return
    mark_deleted(DB_FILE, mid)
    _SCHEDULED_LOCK.discard(mid)
    _STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await _temp_notice(context, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)
    _maybe_delete_command(update)


async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        _maybe_delete_command(update)
        return

    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
    except TelegramError as e:
        ok_del = False
        logger.warning(f"No pude borrar en el canal id:{mid} → {e}")

    _hard_delete_draft(DB_FILE, mid)
    _SCHEDULED_LOCK.discard(mid)
    _STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await _temp_notice(context, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)
    _maybe_delete_command(update)


async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)

    if not mid:
        await _temp_notice(context, "ℹ️ No hay nada para deshacer.", ttl=5)
        _maybe_delete_command(update)
        return

    restore_draft(DB_FILE, mid)
    if _STATS["cancelados"] > 0:
        _STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await _temp_notice(context, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)
    _maybe_delete_command(update)


# ========== PROGRAMAR ==========
async def _schedule_ids(context: ContextTypes.DEFAULT_TYPE, when_dt: datetime, ids: List[int]):
    """Programa el envío de esos IDs exactos. Bloquea esos IDs hasta que se ejecute."""
    if not ids:
        await _temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
        return

    # bloquear
    _SCHEDULED_LOCK.update(ids)

    # registrar
    global _SCHED_SEQ
    _SCHED_SEQ += 1
    pid = _SCHED_SEQ
    rec = {"when": when_dt, "ids": list(ids), "job": None}
    _SCHEDULES[pid] = rec

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            pubs, fails, _posted = await _publicar_ids(ctx, ids=ids, targets=get_active_targets(), mark_as_sent=True)
        finally:
            # desbloquear y limpiar registro
            for i in ids:
                _SCHEDULED_LOCK.discard(i)
            _SCHEDULES.pop(pid, None)

        msg2 = f"⏱️ Programación ejecutada. Publicados {pubs}."
        extra = []
        if _STATS["cancelados"]:
            extra.append(f"Cancelados: {_STATS['cancelados']}")
        if _STATS["eliminados"]:
            extra.append(f"Eliminados: {_STATS['eliminados']}")
        if fails:
            extra.append(f"Fallidos: {fails}")
        if extra:
            msg2 += " " + " · ".join(extra) + "."
        await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)
        _STATS["cancelados"] = 0
        _STATS["eliminados"] = 0

    now = datetime.now(tz=TZ)
    seconds = max(0, int((when_dt - now).total_seconds()))
    if not context.job_queue:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ No pude programar. Falta JobQueue. Asegúrate de usar `python-telegram-bot[job-queue]`.",
            parse_mode="Markdown",
        )
        # revertir bloqueo si no hay job queue
        for i in ids:
            _SCHEDULED_LOCK.discard(i)
        _SCHEDULES.pop(pid, None)
        return

    rec["job"] = context.job_queue.run_once(lambda ctx: asyncio.create_task(job(ctx)), when=seconds)

    eta = _human_eta(when_dt)
    await context.bot.send_message(
        SOURCE_CHAT_ID,
        f"🗓️ Programado para {when_dt.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta}.  (id prog: {pid})"
    )


async def _cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    when = _parse_when_from_text(when_str)
    if when is None:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ Formato inválido. Usa: /programar YYYY-MM-DD HH:MM  (formato 24h)"
        )
        return

    # capturamos los IDs ACTUALES
    ids = [did for (did, _snip) in list_drafts(DB_FILE)]
    await _schedule_ids(context, when, ids)


async def _cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    if not _SCHEDULES:
        await context.bot.send_message(SOURCE_CHAT_ID, "📭 No hay programaciones pendientes.")
        return
    now = datetime.now(tz=TZ)
    lines = ["🗒 Programaciones pendientes:"]
    for pid, rec in sorted(_SCHEDULES.items()):
        when = rec["when"]
        ids = rec["ids"]
        eta = _human_eta(when, now)
        lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(ids)} mensajes")
    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(lines))


async def _cmd_desprogramar(context: ContextTypes.DEFAULT_TYPE, arg: str):
    v = (arg or "").strip().lower()
    if v in ("all", "todos"):
        count = 0
        for pid, rec in list(_SCHEDULES.items()):
            job = rec.get("job")
            if job:
                try:
                    job.schedule_removal()
                except Exception:
                    pass
            for i in rec.get("ids", []):
                _SCHEDULED_LOCK.discard(i)
            _SCHEDULES.pop(pid, None)
            count += 1
        await context.bot.send_message(SOURCE_CHAT_ID, f"❌ Canceladas {count} programaciones.")
        return

    if v.isdigit():
        pid = int(v)
        rec = _SCHEDULES.get(pid)
        if not rec:
            await context.bot.send_message(SOURCE_CHAT_ID, f"No existe la programación #{pid}.")
            return
        job = rec.get("job")
        if job:
            try:
                job.schedule_removal()
            except Exception:
                pass
        for i in rec.get("ids", []):
            _SCHEDULED_LOCK.discard(i)
        _SCHEDULES.pop(pid, None)
        await context.bot.send_message(SOURCE_CHAT_ID, f"❌ Cancelada la programación #{pid}.")
        return

    await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /desprogramar <id|all>")


async def _cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    if update.channel_post and update.channel_post.reply_to_message and len((txt or "").split()) == 1:
        rid = update.channel_post.reply_to_message.message_id
        await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
        _maybe_delete_command(update)
        return

    mid = _extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
        _maybe_delete_command(update)
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
    _maybe_delete_command(update)


# ---------- NUKE ----------
def _parse_nuke_selection(arg: str, drafts: List[Tuple[int, str]]) -> Set[int]:
    arg = (arg or "").strip().lower()
    ids_in_order = [did for (did, _snip) in drafts]
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

    # Soporta "1,2,3" y "1, 2, 3"
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
    parts = (txt or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""

    drafts = list_drafts(DB_FILE)
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
        _SCHEDULED_LOCK.discard(mid)
        borrados += 1

    _STATS["eliminados"] += borrados
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")


# -------------------------------------------------------
# Menús y paneles (inline)
# -------------------------------------------------------
def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Listar", callback_data="m:list"),
             InlineKeyboardButton("📦 Enviar", callback_data="m:send")],
            [InlineKeyboardButton("🧪 Preview", callback_data="m:preview"),
             InlineKeyboardButton("⏰ Programar", callback_data="m:sched")],
            [InlineKeyboardButton("⚙️ Ajustes", callback_data="m:settings")]
        ]
    )

def _text_main() -> str:
    return (
        "🛠️ Comandos:\n"
        "• /listar — muestra borradores pendientes\n"
        "• /cancelar <id> — o responde con /cancelar (no borra del canal)\n"
        "• /deshacer [id] — revierte un /cancelar (o responde)\n"
        "• /eliminar <id> — o responde (BORRA del canal y de la cola)  [alias: /del]\n"
        "• /nuke all|todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N(últimos)\n"
        "• /enviar — publica ahora (a targets activos)\n"
        "• /preview — manda la cola a PREVIEW sin marcar como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa el envío (formato 24h)\n"
        "• /programados — ver pendientes programados · /desprogramar <id|all>\n"
        "• /id [id] — info del mensaje o, si respondes, te dice el ID\n"
        "• /canales — IDs + estado de targets (alias: /targets, /where)\n"
        "• /backup on|off — alterna el backup\n\n"
        "Pulsa un botón o usa /comandos para volver a ver este panel."
    )

def _kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="m:toggle_backup")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
        ]
    )

def _text_settings() -> str:
    onoff = "ON" if _ACTIVE_BACKUP else "OFF"
    return (
        f"📡 **Targets**\n"
        f"• Principal: `{TARGET_CHAT_ID}` **ON** (fijo)\n"
        f"• Backup   : `{BACKUP_CHAT_ID}` **{onoff}**\n"
        f"• Preview  : `{PREVIEW_CHAT_ID}`\n\n"
        "Usa el botón para alternar backup.\n"
        "⬅️ *Volver* regresa al menú principal."
    )

def _kb_schedule() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏳ +5 min", callback_data="s:+5"),
             InlineKeyboardButton("⏳ +15 min", callback_data="s:+15")],
            [InlineKeyboardButton("🕗 Hoy 20:00", callback_data="s:today20"),
             InlineKeyboardButton("🌅 Mañana 07:00", callback_data="s:tom07")],
            [InlineKeyboardButton("🗒 Ver programados", callback_data="s:list"),
             InlineKeyboardButton("❌ Cancelar todos", callback_data="s:clear")],
            [InlineKeyboardButton("✍️ Custom", callback_data="s:custom"),
             InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
        ]
    )

def _text_schedule() -> str:
    return (
        "⏰ Programar envío de **los borradores actuales**.\n"
        "Elige un atajo o usa `/programar YYYY-MM-DD HH:MM` (formato 24h, sin texto extra).\n"
        "⚠️ Si no hay borradores, no se programa nada."
    )


async def _send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, _text_main(), reply_markup=_kb_main())


async def _send_settings_panel(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, _text_settings(), reply_markup=_kb_settings(), parse_mode="Markdown")


# -------------------------------------------------------
# Callbacks (inline)
# -------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    try:
        # Menú principal
        if data == "m:list":
            await _cmd_listar(context)
        elif data == "m:send":
            await _temp_notice(context, "⏳ Procesando envío…", ttl=4)
            ok, fail = await _publicar_todo_activos(context)
            extras = []
            if _STATS["cancelados"]:
                extras.append(f"Cancelados: {_STATS['cancelados']}")
            if _STATS["eliminados"]:
                extras.append(f"Eliminados: {_STATS['eliminados']}")
            msg_out = f"✅ Publicados {ok}."
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            _STATS["cancelados"] = 0
            _STATS["eliminados"] = 0
        elif data == "m:preview":
            await _temp_notice(context, "⏳ Generando preview…", ttl=3)
            pubs, fails, _ = await _publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
        elif data == "m:sched":
            await q.edit_message_text(_text_schedule(), reply_markup=_kb_schedule())
        elif data == "m:settings":
            await q.edit_message_text(_text_settings(), reply_markup=_kb_settings(), parse_mode="Markdown")
        elif data == "m:toggle_backup":
            global _ACTIVE_BACKUP
            _ACTIVE_BACKUP = not _ACTIVE_BACKUP
            await q.edit_message_text(_text_settings(), reply_markup=_kb_settings(), parse_mode="Markdown")
        elif data == "m:back":
            await q.edit_message_text(_text_main(), reply_markup=_kb_main())

        # Programación rápida
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
                await _cmd_programados(context)
            elif data == "s:clear":
                await _cmd_desprogramar(context, "all")
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM`  (formato 24h, sin texto extra)\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown", reply_markup=_kb_schedule()
                )

            if when:
                # IDs actuales
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await _temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
                else:
                    await _schedule_ids(context, when, ids)

    except Exception as e:
        logger.exception(f"Error en callback: {e}")


# -------------------------------------------------------
# Comandos adicionales
# -------------------------------------------------------
async def _cmd_preview(context: ContextTypes.DEFAULT_TYPE):
    await _temp_notice(context, "⏳ Generando preview…", ttl=3)
    pubs, fails, _ = await _publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
    await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")

async def _cmd_canales(context: ContextTypes.DEFAULT_TYPE):
    await _send_settings_panel(context)

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
    await _send_settings_panel(context)


def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))


# -------------------------------------------------------
# Handler único de POSTS en el CANAL BORRADOR
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if msg.chat_id != SOURCE_CHAT_ID:
        return

    txt = (msg.text or "").strip()

    # --------- COMANDOS ----------
    if _is_command_text(txt):
        low = txt.lower()

        if low.startswith("/listar") or low.startswith("/lista"):
            await _cmd_listar(context);  _maybe_delete_command(update);  return

        if low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt);  return

        if low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt);  return

        if low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt);  return

        if low.startswith("/nuke"):
            await _cmd_nuke(context, txt);  _maybe_delete_command(update);  return
        if low.strip() in ("/all", "/todos"):
            await _cmd_nuke(context, "/nuke all");  _maybe_delete_command(update);  return

        if low.startswith("/enviar"):
            await _temp_notice(context, "⏳ Procesando envío…", ttl=4)
            ok, fail = await _publicar_todo_activos(context)
            extras = []
            if _STATS["cancelados"]:
                extras.append(f"Cancelados: {_STATS['cancelados']}")
            if _STATS["eliminados"]:
                extras.append(f"Eliminados: {_STATS['eliminados']}")
            msg_out = f"✅ Publicados {ok}."
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            _STATS["cancelados"] = 0
            _STATS["eliminados"] = 0
            _maybe_delete_command(update)
            return

        if low.startswith("/preview"):
            await _cmd_preview(context);  _maybe_delete_command(update);  return

        if low.startswith("/programar"):
            parts = txt.split(maxsplit=2)
            when_str = parts[1] + " " + parts[2] if len(parts) >= 3 else ""
            await _cmd_programar(context, when_str)
            _maybe_delete_command(update)
            return

        if low.startswith("/programados"):
            await _cmd_programados(context);  _maybe_delete_command(update);  return

        if low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await _cmd_desprogramar(context, arg);  _maybe_delete_command(update);  return

        if low.startswith("/id"):
            await _cmd_id(update, context, txt);  return

        if low.startswith(("/canales", "/targets", "/where")):
            await _cmd_canales(context);  _maybe_delete_command(update);  return

        if low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await _cmd_backup(context, arg);  _maybe_delete_command(update);  return

        if low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await _send_help_with_buttons(context);  _maybe_delete_command(update);  return

        await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")
        _maybe_delete_command(update)
        return

    # --------- BORRADOR ----------
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