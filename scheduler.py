# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List

from database import list_drafts
from publisher import publicar_ids
from core_utils import human_eta
from config import TZ, TZNAME, SOURCE_CHAT_ID

# Registro de programaciones en memoria
SCHEDULES: Dict[int, Dict] = {}
SCHED_SEQ: int = 0

async def schedule_ids(context, when_dt: datetime, ids: List[int], *, active_backup: bool):
    """Programa el envío de esos IDs exactos. Bloquea esos IDs hasta que se ejecute."""
    if not ids:
        await context.bot.send_message(context.bot_data.get('source_chat_id', SOURCE_CHAT_ID), "📭 No hay borradores para programar.")
        return

    # Defaults por si aún no está seteado
    context.bot_data.setdefault('source_chat_id', SOURCE_CHAT_ID)
    context.bot_data.setdefault('db_file', 'drafts.db')

    global SCHED_SEQ
    SCHED_SEQ += 1
    pid = SCHED_SEQ

    async def job(ctx):
        pubs, fails = await publicar_ids(ctx, ids=ids, active_backup=active_backup, mark_as_sent=True)
        # limpiar registro
        SCHEDULES.pop(pid, None)
        msg2 = f"⏱️ Programación ejecutada. Publicados {pubs}."
        if fails:
            msg2 += f" Fallidos: {fails}."
        await ctx.bot.send_message(ctx.bot_data.get('source_chat_id', SOURCE_CHAT_ID), msg2)

    now = datetime.now(tz=TZ)
    seconds = max(0, int((when_dt - now).total_seconds()))
    jobh = context.job_queue.run_once(lambda c: asyncio.create_task(job(c)), when=seconds)

    # guardar registro
    SCHEDULES[pid] = {"when": when_dt, "ids": list(ids), "job": jobh}

    eta = human_eta(when_dt, now)
    await context.bot.send_message(
        context.bot_data.get('source_chat_id', SOURCE_CHAT_ID),
        f"🗓️ Programado para {when_dt.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta}.  (id prog: {pid})"
    )

async def list_programados(context):
    if not SCHEDULES:
        await context.bot.send_message(context.bot_data.get('source_chat_id', SOURCE_CHAT_ID), "📭 No hay programaciones pendientes.")
        return
    now = datetime.now(tz=TZ)
    lines = ["🗒 Programaciones pendientes:"]
    for pid, rec in sorted(SCHEDULES.items()):
        when = rec["when"]
        ids = rec["ids"]
        lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {human_eta(when, now)} — {len(ids)} mensajes")
    await context.bot.send_message(context.bot_data.get('source_chat_id', SOURCE_CHAT_ID), "\n".join(lines))

async def desprogramar(context, arg: str):
    v = (arg or "").strip().lower()
    chat_id = context.bot_data.get('source_chat_id', SOURCE_CHAT_ID)

    if v in ("all", "todos"):
        count = 0
        for pid, rec in list(SCHEDULES.items()):
            job = rec.get("job")
            if job:
                try:
                    job.schedule_removal()
                except Exception:
                    pass
            SCHEDULES.pop(pid, None)
            count += 1
        await context.bot.send_message(chat_id, f"❌ Canceladas {count} programaciones.")
        return

    if v.isdigit():
        pid = int(v)
        rec = SCHEDULES.get(pid)
        if not rec:
            await context.bot.send_message(chat_id, f"No existe la programación #{pid}.")
            return
        job = rec.get("job")
        if job:
            try:
                job.schedule_removal()
            except Exception:
                pass
        SCHEDULES.pop(pid, None)
        await context.bot.send_message(chat_id, f"❌ Cancelada la programación #{pid}.")
        return

    await context.bot.send_message(chat_id, "Usa: /desprogramar <id|all>")
