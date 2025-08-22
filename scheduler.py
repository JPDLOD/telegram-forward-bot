# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from telegram.ext import ContextTypes

from config import TZ, TZNAME
from database import list_drafts
from core_utils import temp_notice, human_eta, parse_programar_when
from publisher import publicar_ids, get_active_targets, SCHEDULED_LOCK

# Registro de programaciones en memoria
SCHEDULES: Dict[int, Dict] = {}
SCHED_SEQ = 0


async def schedule_ids(context: ContextTypes.DEFAULT_TYPE, when_dt: datetime, ids: List[int]) -> int:
    """Programa el envío de esos IDs exactos. Bloquea esos IDs hasta ejecutar."""
    if not ids:
        await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
        return 0

    # Bloquear
    SCHEDULED_LOCK.update(ids)

    # Registrar
    global SCHED_SEQ
    SCHED_SEQ += 1
    pid = SCHED_SEQ
    rec = {"when": when_dt, "ids": list(ids), "job": None}
    SCHEDULES[pid] = rec

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        # Ejecuta y limpia
        pubs, fails, _posted = await publicar_ids(ctx, ids=ids, targets=get_active_targets(), mark_as_sent=True)
        for i in ids:
            SCHEDULED_LOCK.discard(i)
        SCHEDULES.pop(pid, None)
        msg2 = f"⏱️ Programación ejecutada. Publicados {pubs}."
        if fails:
            msg2 += f" Fallidos: {fails}."
        await ctx.bot.send_message(chat_id=ctx.bot_data.get('source_chat_id'), text=msg2)

    # JobQueue
    now = datetime.now(tz=TZ)
    seconds = max(0, int((when_dt - now).total_seconds()))
    if not context.job_queue:
        # Revertir bloqueo
        for i in ids:
            SCHEDULED_LOCK.discard(i)
        SCHEDULES.pop(pid, None)
        await context.bot.send_message(
            ctx.bot_data.get('source_chat_id'),
            "❌ No pude programar. Falta JobQueue. Asegúrate de usar `python-telegram-bot[job-queue]`.",
            parse_mode="Markdown",
        )
        return 0

    # Guardar source_chat_id en bot_data para usar dentro del job
    context.bot_data['source_chat_id'] = context.bot_data.get('source_chat_id')

    rec["job"] = context.job_queue.run_once(lambda ctx: ctx.application.create_task(job(ctx)), when=seconds)
    eta = human_eta(when_dt)
    await context.bot.send_message(
        context.bot_data['source_chat_id'],
        f"🗓️ Programado para {when_dt.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta}.  (id prog: {pid})"
    )
    return pid


async def cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    norm = parse_programar_when(when_str or "")
    if not norm:
        await context.bot.send_message(
            context.bot_data['source_chat_id'],
            "❌ Formato inválido. Usa: `/programar YYYY-MM-DD HH:MM`  (formato 24 h)",
            parse_mode="Markdown"
        )
        return
    when = datetime.strptime(norm, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    # Capturar IDs ACTUALES (orden /listar)
    ids = [did for (did, _snip) in list_drafts(context.bot_data['db_file'])]
    await schedule_ids(context, when, ids)


async def cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    if not SCHEDULES:
        await context.bot.send_message(context.bot_data['source_chat_id'], "📭 No hay programaciones pendientes.")
        return
    from config import TZNAME
    from core_utils import human_eta
    from datetime import datetime
    now = datetime.now(tz=TZ)
    lines = ["🗒 Programaciones pendientes:"]
    for pid, rec in sorted(SCHEDULES.items()):
        when = rec["when"]
        ids = rec["ids"]
        eta = human_eta(when, now)
        lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(ids)} mensajes")
    await context.bot.send_message(context.bot_data['source_chat_id'], "\n".join(lines))


async def cmd_desprogramar(context: ContextTypes.DEFAULT_TYPE, arg: str):
    v = (arg or "").strip().lower()
    if v in ("all", "todos"):
        count = 0
        for pid, rec in list(SCHEDULES.items()):
            job = rec.get("job")
            if job:
                try:
                    job.schedule_removal()
                except Exception:
                    pass
            for i in rec.get("ids", []):
                SCHEDULED_LOCK.discard(i)
            SCHEDULES.pop(pid, None)
            count += 1
        await context.bot.send_message(context.bot_data['source_chat_id'], f"❌ Canceladas {count} programaciones.")
        return

    if v.isdigit():
        pid = int(v)
        rec = SCHEDULES.get(pid)
        if not rec:
            await context.bot.send_message(context.bot_data['source_chat_id'], f"No existe la programación #{pid}.")
            return
        job = rec.get("job")
        if job:
            try:
                job.schedule_removal()
            except Exception:
                pass
        for i in rec.get("ids", []):
            SCHEDULED_LOCK.discard(i)
        SCHEDULES.pop(pid, None)
        await context.bot.send_message(context.bot_data['source_chat_id'], f"❌ Cancelada la programación #{pid}.")
        return

    await context.bot.send_message(context.bot_data['source_chat_id'], "Usa: /desprogramar <id|all>")
