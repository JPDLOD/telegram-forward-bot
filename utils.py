# -*- coding: utf-8 -*-
import asyncio
import re
from datetime import datetime
from typing import Optional, List, Tuple, Set

from config import TZ, SOURCE_CHAT_ID

# --------- helpers genéricos ---------
async def safe_sleep(seconds: float):
    try:
        await asyncio.sleep(max(0.0, seconds))
    except Exception:
        pass

async def temp_notice(bot, text: str, ttl: int = 6):
    """Envía un aviso temporal y lo borra pasado `ttl` segundos."""
    try:
        m = await bot.send_message(SOURCE_CHAT_ID, text, disable_notification=True)
    except Exception:
        return
    async def _auto_del():
        await safe_sleep(ttl)
        try:
            await bot.delete_message(SOURCE_CHAT_ID, m.message_id)
        except Exception:
            pass
    asyncio.create_task(_auto_del())

def human_eta(target_dt: datetime, now: Optional[datetime] = None) -> str:
    """Texto corto tipo 'en 27 min' / 'en 1 h 15 m' / 'en 2 d 3 h'."""
    from config import TZ
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

def extract_id_from_text(txt: str) -> Optional[int]:
    parts = (txt or "").split()
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
        if p.lower().startswith("id:"):
            n = p.split(":", 1)[1]
            if n.isdigit():
                return int(n)
    return None

def deep_link_for_channel_message(chat_id: int, mid: int) -> str:
    # Para canales privados: https://t.me/c/<chatid_sin_-100>/<id>
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{mid}"

def parse_nuke_selection(arg: str, drafts: List[Tuple[int, str]]) -> Set[int]:
    """
    Convierte una selección textual basada en posiciones de /listar a IDs de mensajes.
    Soporta:
      - 'all' / 'todos' → todos
      - '1,3,5' o '1, 3, 5' → lista de posiciones
      - '2-7' → rango
      - número simple 'N' → 'últimos N'
    """
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

    # Acepta "1,2,3" o "1, 2, 3"
    arg = arg.replace(" ", "")
    pieces = [p for p in arg.split(",") if p]

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
