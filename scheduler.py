# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram.ext import ContextTypes

from config import TZ, TZNAME
from database import list_drafts
from core_utils import human_eta, temp_notice
from publisher import (
    publicar_ids, get_active_targets, STATS, SCHEDULED_LOCK, LAST_BATCH
)

# ======= registro de programaciones (en memoria) =======
# {pid: {"when": datetime, "ids": [...], "job": Job}}
SCHEDULES: Dict[int, Dict] = {}
SCHED_SEQ: int = 0


def _parse_datetime_str(s: str) -> Optional[datetime]:
    """
    Acepta 'YYYY-MM-DD HH:MM' con HH de 0..23.
    Ignora texto extra al final (p.ej. '(24 h)').
    Acepta '1:27' o '01:27'.
    """
    parts = s.strip().split()
    if len(parts) < 2:
        return None
    date_str = parts[0]
    time_raw = parts[1]
    # normaliza hora 1:27 -> 01:27
    if ":" in time_raw:
        hh, mm = time_raw.split(":", 1)
        if hh.isdigit() and len(hh) == 1:
            time_raw = f"0{hh}:{mm}"
    try:
        dt = datetime.strptime(f"{date_str} {time_raw}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=TZ)
    except Exception:
        return None


async def schedule_ids(context: ContextTypes.DEFAULT_TYPE, when_dt: datetime, ids: List[int]):
    """Programa el envío de esos IDs exactos. Bloquea esos IDs hasta que se ejecute."""
    if not ids:
        await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
        return

    # bloquear
    SCHEDULED_LOCK.update(ids)

    # registrar
    global SCHED_SEQ
    SCHED_SEQ += 1
    pid = SCHED_SEQ
    rec = {"when": when_dt, "ids": list(ids), "job": None}
    SCHEDULES[pid] = rec

    async def job(ctx: ContextTypes.DEFAULT_TYPE):
        pubs, fails, posted = 0, 0, {}
        try:
            pubs, fails, posted = await publicar_ids(ctx, ids=ids, mark_as_sent=True)
        finally:
            # desbloquear y limpiar registro
            for i in ids:
                SCHEDULED_LOCK.discard(i)
            SCHEDULES.pop(pid, None)

        # guarda lote para /undo_send si se decide reactivar en el futuro
        # (dejamos la variable LAST_BATCH intacta si se usa en otro lugar)
        from publisher import LAST_BATCH as _LB
        _LB.update(posted)

        msg2 = f"⏱️ Programación ejecutada. Publicados {pubs}."
        extra = []
        if STATS.get("cancelados"):
            extra.append(f"Cancelados: {STATS['cancelados']}")
        if STATS.get("eliminados"):
            extra.append(f"Eliminados: {STATS['eliminados']}")
        if fails:
            extra.append(f"Fallidos: {fails}")
        if extra:
            msg2 += " " + " · ".join(extra) + "."
        await ctx.bot.send_message(list_drafts.__defaults__[0] if list_drafts.__defaults__ else None, msg2)  # no usamos
        # mejor enviamos directo al canal de borradores:
        from config import SOURCE_CHAT_ID
        await ctx.bot.send_message(SOURCE_CHAT_ID, msg2)
        STATS["cancelados"] = 0
        STATS["eliminados"] = 0

    now = datetime.now(tz=TZ)
    delay = max(0, (when_dt - now).total_seconds())

    # PTB 21.x admite float (segundos) o datetime.
    rec["job"] = context.job_queue.run_once(lambda ctx: asyncio.create_task(job(ctx)), when=delay)

    eta = human_eta(when_dt, now)
    from config import SOURCE_CHAT_ID
    await context.bot.send_message(
        SOURCE_CHAT_ID,
        f"🗓️ Programado para {when_dt.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta}.  (id prog: {pid})"
    )


async def cmd_programar(context: ContextTypes.DEFAULT_TYPE, when_str: str):
    when = _parse_datetime_str(when_str)
    from config import SOURCE_CHAT_ID
    if not when:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "❌ Formato inválido. Usa: /programar YYYY-MM-DD HH:MM  (formato 24 h)"
        )
        return

    ids = [did for (did, _snip) in list_drafts.__wrapped__("drafts.db")]  # fallback por si default no está
    # Mejor: usa la API pública:
    from database import list_drafts as _list
    ids = [did for (did, _snip) in _list("drafts.db")]

    # capturamos los IDs ACTUALES
    from database import list_drafts as list_drafts_real
    ids = [did for (did, _snip) in list_drafts_real("drafts.db")]
    await schedule_ids(context, when, ids)


async def cmd_programados(context: ContextTypes.DEFAULT_TYPE):
    from config import SOURCE_CHAT_ID
    if not SCHEDULES:
        await context.bot.send_message(SOURCE_CHAT_ID, "📭 No hay programaciones pendientes.")
        return
    now = datetime.now(tz=TZ)
    lines = ["🗒 Programaciones pendientes:"]
    for pid, rec in sorted(SCHEDULES.items()):
        when = rec["when"]
        ids = rec["ids"]
        eta = human_eta(when, now)
        lines.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(ids)} mensajes")
    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(lines))


async def cmd_desprogramar(context: ContextTypes.DEFAULT_TYPE, arg: str):
    from config import SOURCE_CHAT_ID
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
