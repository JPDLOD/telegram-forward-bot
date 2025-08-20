import asyncio
import logging
import os
import random
from datetime import datetime
from typing import List, Optional, Tuple

import aiosqlite
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    constants,
    Poll,
)
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# -------------------------
# Config & Logger
# -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])  # Canal BORRADOR
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])  # Canal PUBLICACIÓN
TZ_NAME = os.environ.get("TIMEZONE", "UTC")
LOCAL_TZ = ZoneInfo(TZ_NAME)

# PAUSE: segundos entre mensajes (float aceptado: "0.6", "1", etc.)
_raw = os.environ.get("PAUSE", "1.0")
try:
    PAUSE_SEC = float(_raw)
except Exception:
    PAUSE_SEC = 1.0  # fallback seguro

DB_PATH = "bot.sqlite3"

# -------------------------
# DB helpers (aiosqlite)
# -------------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS drafts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source_msg_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL,                      -- text|media|poll
    text_snippet TEXT,                       -- primeras ~80 chars del texto/caption
    poll_question TEXT,
    poll_options TEXT,                       -- opciones separadas por \n
    deleted INTEGER NOT NULL DEFAULT 0,      -- 0/1 soft delete
    pinned INTEGER NOT NULL DEFAULT 0,       -- mensaje de pin/servicio (NO listar)
    bot_generated INTEGER NOT NULL DEFAULT 0,-- mensajes del bot (NO listar)
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_active
  ON drafts(deleted, pinned, bot_generated, source_msg_id);
"""

SNIPPET_LEN = 80


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()


async def db_insert_draft(
    chat_id: int,
    source_msg_id: int,
    kind: str,
    text_snippet: Optional[str],
    poll_question: Optional[str],
    poll_options: Optional[str],
    pinned: int,
    bot_generated: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO drafts
            (chat_id, source_msg_id, kind, text_snippet, poll_question, poll_options, deleted, pinned, bot_generated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, strftime('%s','now'))
            """,
            (
                chat_id,
                source_msg_id,
                kind,
                text_snippet,
                poll_question,
                poll_options,
                pinned,
                bot_generated,
            ),
        )
        await db.commit()


async def db_list_drafts() -> List[Tuple[int, str, str, str]]:
    """(msg_id, kind, snippet, poll_question) solo activos (no /eliminar, no pinned, no bot)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT source_msg_id, kind, COALESCE(text_snippet,''), COALESCE(poll_question,'')
            FROM drafts
            WHERE deleted=0 AND pinned=0 AND bot_generated=0
            ORDER BY source_msg_id ASC
            """
        )
        return await cur.fetchall()


async def db_counts_for_report() -> Tuple[int, int, int]:
    """Devuelve (activos, eliminados, total_para_reporte = activos + eliminados)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT
              SUM(CASE WHEN deleted=0 AND pinned=0 AND bot_generated=0 THEN 1 ELSE 0 END) AS activos,
              SUM(CASE WHEN deleted=1 AND pinned=0 AND bot_generated=0 THEN 1 ELSE 0 END) AS eliminados
            FROM drafts
            """
        )
        row = await cur.fetchone()
        activos = int(row[0] or 0)
        eliminados = int(row[1] or 0)
        return activos, eliminados, activos + eliminados


async def db_mark_deleted(source_msg_id: int, deleted: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE drafts SET deleted=? WHERE source_msg_id=?",
            (1 if deleted else 0, source_msg_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_get_draft(source_msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT chat_id, source_msg_id, kind, text_snippet, poll_question, poll_options, deleted
            FROM drafts WHERE source_msg_id=?
            """,
            (source_msg_id,),
        )
        return await cur.fetchone()


async def db_all_active_to_publish() -> List[Tuple[int, str, Optional[str], Optional[str]]]:
    """(source_msg_id, kind, poll_question, poll_options) activos para publicar."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT source_msg_id, kind, poll_question, poll_options
            FROM drafts
            WHERE deleted=0 AND pinned=0 AND bot_generated=0
            ORDER BY source_msg_id ASC
            """
        )
        return await cur.fetchall()


# -------------------------
# Utils
# -------------------------
def is_command_message_text(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.strip()
    return t.startswith("/")


def make_snippet(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = " ".join(text.strip().split())
    return t[:SNIPPET_LEN] + ("…" if len(t) > SNIPPET_LEN else "")


def build_channel_link(chat_id: int, message_id: int) -> Optional[str]:
    s = str(chat_id)
    if s.startswith("-100"):
        short = s[4:]
        return f"https://t.me/c/{short}/{message_id}"
    return None


async def gentle_pause():
    # Pausa pseudo-aleatoria alrededor de PAUSE_SEC
    base = max(0.2, PAUSE_SEC)
    extra = random.uniform(-0.2, 0.3)
    await asyncio.sleep(base + extra)


# -------------------------
# Handlers
# -------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda borradores cuando llegan al canal BORRADOR."""
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    # 1) Ignorar mensajes de servicio (pinned, joins, etc.)
    if msg.pinned_message or msg.new_chat_members or msg.left_chat_member:
        return

    # 2) Ignorar comandos / mensajes que empiezan con "/"
    if is_command_message_text(msg.text) or is_command_message_text(getattr(msg, "caption", None)):
        return

    # 3) Ignorar mensajes generados por el bot
    bot_generated = 1 if (msg.from_user and msg.from_user.is_bot) else 0
    if bot_generated:
        return

    # 4) Detectar tipo y extraer info
    kind = "text"
    snippet = None
    poll_q = None
    poll_opts = None

    if msg.poll:
        kind = "poll"
        poll: Poll = msg.poll
        poll_q = poll.question
        poll_opts = "\n".join([o.text for o in poll.options])
        snippet = make_snippet(poll_q)
    elif msg.text:
        kind = "text"
        snippet = make_snippet(msg.text)
    else:
        kind = "media"
        snippet = make_snippet(getattr(msg, "caption", None))

    await db_insert_draft(
        chat_id=msg.chat_id,
        source_msg_id=msg.message_id,
        kind=kind,
        text_snippet=snippet,
        poll_question=poll_q,
        poll_options=poll_opts,
        pinned=0,
        bot_generated=0,
    )
    log.info("Guardado en borrador: %s", msg.message_id)


async def cmd_comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    text = (
        "🛠️ *Comandos:*\n"
        "• */listar* — muestra borradores pendientes\n"
        "• */eliminar* `<id>` — o responde con */eliminar* al mensaje\n"
        "• */deshacer* `<id>` — revierte un */eliminar* (o responde)\n"
        "• */enviar* — publica ahora\n"
        "• */programar* `YYYY-MM-DD HH:MM` — programa el envío\n"
        "• */mensaje* `<id>` — vista previa/enlace del borrador\n"
        "• */id* — responde a un mensaje para ver su id\n"
        "• */comandos* — muestra esta ayuda\n"
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def cmd_listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    drafts = await db_list_drafts()
    if not drafts:
        await update.effective_message.reply_text("📁 No hay borradores.")
        return

    lines = ["🗒️ *Borradores pendientes:*"]
    for i, (mid, kind, snip, pq) in enumerate(drafts, start=1):
        label = snip or pq or "[contenido]"
        if kind == "poll":
            label = f"[encuesta] {pq or ''}".strip()
        lines.append(f"• {i} — {label}  (id:{mid})")

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    ref = update.effective_message.reply_to_message
    if not ref:
        await update.effective_message.reply_text("Responde a un mensaje del canal con /id.")
        return
    await update.effective_message.reply_text(f"id:{ref.message_id}")


def _parse_single_id(update: Update, args: List[str]) -> Optional[int]:
    if args:
        raw = args[0].replace("id:", "").strip()
        if raw.isdigit():
            return int(raw)
    ref = update.effective_message.reply_to_message
    if ref:
        return ref.message_id
    return None


async def cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    mid = _parse_single_id(update, context.args)
    if not mid:
        await update.effective_message.reply_text("Usa /eliminar <id> o responde con /eliminar al mensaje.")
        return
    ok = await db_mark_deleted(mid, True)
    if not ok:
        await update.effective_message.reply_text(f"No encontré el id:{mid}.")
        return
    rest = len(await db_list_drafts())
    await update.effective_message.reply_text(f"🗑️ Eliminado id:{mid}. Quedan {rest} en la cola.")


async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    mid = _parse_single_id(update, context.args)
    if not mid:
        await update.effective_message.reply_text("Usa /deshacer <id> o responde con /deshacer al mensaje.")
        return
    ok = await db_mark_deleted(mid, False)
    if not ok:
        await update.effective_message.reply_text(f"No encontré el id:{mid}.")
        return
    rest = len(await db_list_drafts())
    await update.effective_message.reply_text(f"↩️ Repuesto id:{mid}. Quedan {rest} en la cola.")


async def cmd_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usa: /mensaje <id>")
        return
    try:
        mid = int(context.args[0].replace("id:", "").strip())
    except ValueError:
        await update.effective_message.reply_text("Formato: /mensaje <id>")
        return

    row = await db_get_draft(mid)
    if not row:
        await update.effective_message.reply_text(f"No encontré el id:{mid}.")
        return

    _, source_msg_id, kind, snippet, pq, _opts, _del = row
    label = snippet or pq or "[contenido]"
    link = build_channel_link(SOURCE_CHAT_ID, source_msg_id)
    text = f"*id:{source_msg_id}* [{kind}]\n{label}"
    if link:
        text += f"\n\n[Ver en borrador]({link})"
    await update.effective_message.reply_text(
        text,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def publish_all(context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, int, int, int]:
    """Devuelve (total_reportado, enviados, eliminados_manuales, fallidos)."""
    activos, eliminados, total_report = await db_counts_for_report()
    drafts = await db_all_active_to_publish()

    enviados = 0
    fallidos = 0
    bot = context.bot

    for (source_msg_id, kind, poll_question, poll_options) in drafts:
        try:
            if kind == "poll":
                options = (poll_options or "").split("\n") if poll_options else []
                if not options or not poll_question:
                    raise BadRequest("Poll malformada")
                await bot.send_poll(
                    chat_id=TARGET_CHAT_ID,
                    question=poll_question,
                    options=options,
                    is_anonymous=True,
                    allows_multiple_answers=False,
                )
            else:
                await bot.copy_message(
                    chat_id=TARGET_CHAT_ID,
                    from_chat_id=SOURCE_CHAT_ID,
                    message_id=source_msg_id,
                )

            enviados += 1
            await gentle_pause()

        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 3)) + 1
            log.warning("RetryAfter %ss (flood control). Esperando…", wait)
            await asyncio.sleep(wait)
            try:
                if kind == "poll":
                    options = (poll_options or "").split("\n") if poll_options else []
                    await bot.send_poll(
                        chat_id=TARGET_CHAT_ID,
                        question=poll_question or "",
                        options=options,
                        is_anonymous=True,
                        allows_multiple_answers=False,
                    )
                else:
                    await bot.copy_message(
                        chat_id=TARGET_CHAT_ID,
                        from_chat_id=SOURCE_CHAT_ID,
                        message_id=source_msg_id,
                    )
                enviados += 1
            except Exception as e2:
                log.error("Falló tras RetryAfter id:%s -> %s", source_msg_id, e2)
                fallidos += 1

        except (TimedOut, NetworkError):
            ok = False
            for _ in range(3):
                await asyncio.sleep(3)
                try:
                    if kind == "poll":
                        options = (poll_options or "").split("\n") if poll_options else []
                        await bot.send_poll(
                            chat_id=TARGET_CHAT_ID,
                            question=poll_question or "",
                            options=options,
                            is_anonymous=True,
                            allows_multiple_answers=False,
                        )
                    else:
                        await bot.copy_message(
                            chat_id=TARGET_CHAT_ID,
                            from_chat_id=SOURCE_CHAT_ID,
                            message_id=source_msg_id,
                        )
                    enviados += 1
                    ok = True
                    break
                except Exception:
                    pass
            if not ok:
                log.error("Demasiados reintentos; abandono id:%s", source_msg_id)
                fallidos += 1

        except (BadRequest, Forbidden) as e:
            log.error("No pude publicar id:%s -> %s", source_msg_id, e)
            fallidos += 1

    return (total_report, enviados, eliminados, fallidos)


async def cmd_enviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    total, enviados, eliminados, fallidos = await publish_all(context)
    texto = (
        f"✅ Publicados {enviados}.\n"
        f"📦 Resultado: {enviados}/{total} enviados."
    )
    if eliminados:
        texto += f" 🗑️ Omitidos por /eliminar: {eliminados}."
    if fallidos:
        texto += " ⚠️ Revisa permisos y flood control."
    await update.effective_message.reply_text(texto)


async def cmd_programar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usa: /programar YYYY-MM-DD HH:MM")
        return
    when_str = f"{context.args[0]} {context.args[1]}"
    try:
        dt = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        await update.effective_message.reply_text("❌ Formato inválido. Ej: /programar 2025-08-20 07:00")
        return

    now = datetime.now(tz=LOCAL_TZ)
    diff = (dt - now).total_seconds()
    if diff <= 0:
        await update.effective_message.reply_text("La fecha/hora ya pasó.")
        return

    async def job(_context: ContextTypes.DEFAULT_TYPE):
        await publish_all(_context)

    context.job_queue.run_once(job, when=diff)
    await update.effective_message.reply_text(f"🗓️ Programado para {dt.strftime('%Y-%m-%d %H:%M')} ({TZ_NAME}).")


# -------------------------
# App bootstrap
# -------------------------
def main():
    asyncio.run(db_init())

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter(max_retries=0))
        .build()
    )

    # Comandos
    app.add_handler(CommandHandler("comandos", cmd_comandos, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("listar", cmd_listar, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("id", cmd_id, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("eliminar", cmd_eliminar, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("mensaje", cmd_mensaje, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("enviar", cmd_enviar, filters.Chat(SOURCE_CHAT_ID)))
    app.add_handler(CommandHandler("programar", cmd_programar, filters.Chat(SOURCE_CHAT_ID)))

    # Escuchar publicaciones del canal BORRADOR
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))

    log.info("SQLite listo. BORRADOR=%s  PUBLICACIÓN=%s  TZ=%s  PAUSE=%.3fs", SOURCE_CHAT_ID, TARGET_CHAT_ID, TZ_NAME, PAUSE_SEC)
    app.run_polling(allowed_updates=["channel_post", "message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
