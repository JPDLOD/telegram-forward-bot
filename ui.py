# -*- coding: utf-8 -*-
from typing import List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID, TZNAME, TZ, SOURCE_CHAT_ID
from database import list_drafts
from core_utils import temp_notice
from publisher import ACTIVE_BACKUP, STATS, get_active_targets, publicar_todo_activos, SCHEDULED_LOCK
from scheduler import SCHEDULES, cmd_programados, cmd_desprogramar, schedule_ids

# ------------------ Textos y teclados ------------------

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Listar", callback_data="m:list"),
             InlineKeyboardButton("📦 Enviar", callback_data="m:send")],
            [InlineKeyboardButton("🧪 Preview", callback_data="m:preview"),
             InlineKeyboardButton("⏰ Programar", callback_data="m:sched")],
            [InlineKeyboardButton("⚙️ Ajustes", callback_data="m:settings")]
        ]
    )

def text_main() -> str:
    return (
        "🛠️ Acciones rápidas:\n"
        "• /listar — muestra borradores pendientes\n"
        "• /enviar — publica ahora a targets activos\n"
        "• /preview — manda la cola a PREVIEW sin marcarla como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa lo que está en /listar (formato 24 h)\n"
        "• /programados — ver pendientes programados · /desprogramar <id|all>\n"
        "• /nuke …  • /cancelar  • /eliminar  • /id [id]\n"
        "Pulsa un botón o usa /comandos para ver todos los comandos."
    )

def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="m:toggle_backup")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
        ]
    )

def text_settings() -> str:
    onoff = "ON" if ACTIVE_BACKUP else "OFF"
    return (
        f"📡 **Targets**\n"
        f"• Principal: `{TARGET_CHAT_ID}` **ON** (fijo)\n"
        f"• Backup   : `{BACKUP_CHAT_ID}` **{onoff}**\n"
        f"• Preview  : `{PREVIEW_CHAT_ID}`\n\n"
        "Usa el botón para alternar backup.\n"
        "⬅️ *Volver* regresa al menú principal."
    )

def kb_schedule() -> InlineKeyboardMarkup:
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

def text_schedule() -> str:
    return (
        "⏰ Programar envío de **los borradores actuales**.\n"
        "Elige un atajo o usa `/programar YYYY-MM-DD HH:MM` (formato 24 h).\n"
        "⚠️ Si no hay borradores, no se programa nada."
    )

# ------------------ Salidas ------------------

def _format_drafts_list() -> List[str]:
    drafts = list_drafts("drafts.db")
    out = ["📋 Borradores pendientes:"]
    if not drafts:
        out.append("• 0")
    else:
        for i, (did, snip) in enumerate(drafts, start=1):
            s = (snip or "").strip()
            if len(s) > 60:
                s = s[:60] + "…"
            # Si está bloqueado por schedule, marca
            suffix = " (programado)" if did in SCHEDULED_LOCK else ""
            out.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did}){suffix}")

    # Programaciones
    if not SCHEDULES:
        out.append("\n🗓️ Programaciones pendientes: 0")
    else:
        out.append("\n🗓️ Programaciones pendientes:")
        from core_utils import human_eta
        from datetime import datetime
        now = datetime.now(tz=TZ)
        for pid, rec in sorted(SCHEDULES.items()):
            when = rec["when"]
            eta = human_eta(when, now)
            out.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(rec['ids'])} mensajes")
    return out


async def cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    lines = _format_drafts_list()
    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(lines))


# ------------------ Callbacks ------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    from datetime import datetime, timedelta

    if data == "m:list":
        await cmd_listar(context)
        return

    if data == "m:send":
        await temp_notice(context, "⏳ Procesando envío…", ttl=4)
        ok, fail = await publicar_todo_activos(context)
        extras = []
        if STATS.get("cancelados"):
            extras.append(f"Cancelados: {STATS['cancelados']}")
        if STATS.get("eliminados"):
            extras.append(f"Eliminados: {STATS['eliminados']}")
        msg_out = f"✅ Publicados {ok}."
        if fail:
            extras.append(f"Fallidos: {fail}")
        if extras:
            msg_out += "\n📦 " + " · ".join(extras) + "."
        await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
        STATS["cancelados"] = 0
        STATS["eliminados"] = 0
        return

    if data == "m:preview":
        await temp_notice(context, "⏳ Generando preview…", ttl=3)
        # Preview a canal de preview (no marca como enviados)
        from publisher import _publicar_rows  # interno
        rows = list_drafts("drafts.db")
        from database import get_unsent_drafts
        rows_full = get_unsent_drafts("drafts.db")
        pubs, fails, _ = await _publicar_rows(
            context, rows=rows_full, targets=[PREVIEW_CHAT_ID], mark_as_sent=False
        )
        await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
        return

    if data == "m:sched":
        await q.edit_message_text(text_schedule(), reply_markup=kb_schedule())
        return

    if data == "m:settings":
        await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        return

    if data == "m:toggle_backup":
        from publisher import ACTIVE_BACKUP as AB
        from publisher import ACTIVE_BACKUP as _AB_ALIAS  # para que mypy no fastidie
        import publisher as _pub
        _pub.ACTIVE_BACKUP = not _pub.ACTIVE_BACKUP
        await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        return

    if data == "m:back":
        await q.edit_message_text(text_main(), reply_markup=kb_main())
        return

    # Atajos de programación
    if data.startswith("s:"):
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
            await cmd_programados(context);  return
        elif data == "s:clear":
            await cmd_desprogramar(context, "all");  return
        elif data == "s:custom":
            await q.edit_message_text(
                "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM`  (formato 24 h)\n\n⬅️ Usa *Volver* para regresar.",
                parse_mode="Markdown", reply_markup=kb_schedule()
            )
            return

        if when:
            ids = [did for (did, _snip) in list_drafts("drafts.db")]
            if not ids:
                await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
            else:
                await schedule_ids(context, when, ids)
        return
