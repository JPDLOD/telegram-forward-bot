# -*- coding: utf-8 -*-
import os
from zoneinfo import ZoneInfo

# =========================
# CONFIG DESDE ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # obligatorio

# Chats (con fallbacks para evitar crasheos en despliegues)
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", "-1002859784457"))
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "-1002679848195"))

BACKUP_FALLBACK = -1002717125281   # tu backup
PREVIEW_FALLBACK = -1003042227035  # tu preview

BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID", str(BACKUP_FALLBACK)))
PREVIEW_CHAT_ID = int(os.environ.get("PREVIEW_CHAT_ID", str(PREVIEW_FALLBACK)))

DB_FILE = os.environ.get("DB_FILE", "drafts.db")

# pausa base entre envíos (seg) para evitar flood
PAUSE = float(os.environ.get("PAUSE", "0.6"))

TZNAME = os.environ.get("TIMEZONE", "America/Bogota")
TZ = ZoneInfo(TZNAME)
