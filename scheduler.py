# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from telegram.ext import ContextTypes
from config import TZ, TZNAME, SOURCE_CHAT_ID
from core_utils import human_eta
from publisher import publicar_ids, get_active_targets, STATS, SCHEDULED_LOCK

logger = logging.getLogger(__name__)

# REGISTRO EN MEMORIA: {pid: {"when": datetime, "ids": [...], "job": Job}}
SCHEDULES: Dict[int, Dict] = {}
SCHED_SEQ: int = 0

async def schedule_ids(context: ContextTypes.DEFAULT_TYPE, when_dt: datetime, ids: List[int]):
    if not ids:
        await context.bot.send_message(SOURCE_CHAT_ID, "📭 No hay borradores para programar.")
        return

    SCHEDULED_LOCK.update(ids)

    global SCHED_SEQ
    SCHED_SEQ += 1
    pid = SCHED_SEQ
    rec = {"when": when_dt, "ids": list(ids), "job": None}
    SCHEDULES[pid] = rec

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            pubs, fails, _posted = await publicar_ids(ctx, ids=ids, targets=get_active_targets(), mark_as_sent=True)
            msg2 = f"⏱️ Programación ejecutada. Publicados {pubs}."
            extra = []
            if STATS["cancelados"]:
                extra.append(f"Cancelados: {STATS['cancelados']}")
            if STATS["eliminados"]:
                extra.append(f"Eliminados: {STATS['eliminados']}")
            if fails:
                extra.append(f"Fallidos: {fails}")
            if extra:
                msg2 += " " + " · ".join(extra) + "."
            await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)
            STATS["cancelados"] = 0
            STATS["eliminados"] = 0
        except Exception:
            await ctx.bot.send_message(SOURCE_CHAT_ID, "❌ Error ejecutando la programación (revisa logs).")
        finally:
            for i in ids:
                SCHEDULED_LOCK.discard(i)
            SCHEDULES.pop(pid, None)

    now = datetime.now(tz=TZ)
    seconds = max(0, int((when_dt - now).total_seconds()))
    if not context.job_queue:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ No pude programar. Falta JobQueue. Asegúrate de usar `python-telegram-bot[job-queue]`.",
            parse_mode="Markdown",
        )
        for i in ids:
            SCHEDULED_LOCK.discard(i)
        SCHEDULES.pop(pid, None)
        return

    rec["job"] = context.job_queue.run_once(job, when=seconds)

    eta = human_eta(when_dt)
    await context.bot.send_message(
        SOURCE_CHAT_ID,
        f"🗓️ Programado para {when_dt.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta}.  (id prog: {pid})"
    )

async def cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    from config import DB_FILE
    from database import list_drafts
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ Formato inválido. Usa: `/programar YYYY-MM-DD HH:MM` (24h: 00:00–23:59, sin '(24h)' ni AM/PM).",
            parse_mode="Markdown",
        )
        return

    ids = [did for (did, _snip) in list_drafts(DB_FILE)]
    if not ids:
        await context.bot.send_message(SOURCE_CHAT_ID, "📭 No hay borradores para programar.")
        return
    await schedule_ids(context, when, ids)

async def cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    from config import TZNAME, TZ
    if not SCHEDULES:
        await context.bot.send_message(SOURCE_CHAT_ID, "📭 No hay programaciones pendientes.")
        return
    from datetime import datetime as _dt
    now = _dt.now(tz=TZ)
    lines = ["🗒 Programaciones pendientes:"]
    for pid, rec in sorted(SCHEDULES.items()):
        when = rec["when"]
        ids = rec["ids"]
        eta = human_eta(when, now)
        lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(ids)} mensajes")
    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(lines))

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
        await context.bot.send_message(SOURCE_CHAT_ID, f"❌ Canceladas {count} programaciones.")
        return

    if v.isdigit():
        pid = int(v)
        rec = SCHEDULES.get(pid)
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
            SCHEDULED_LOCK.discard(i)
        SCHEDULES.pop(pid, None)
        await context.bot.send_message(SOURCE_CHAT_ID, f"❌ Cancelada la programación #{pid}.")
        return

    await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /desprogramar <id|all>")
