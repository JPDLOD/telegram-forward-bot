# -*- coding: utf-8 -*-
import os
from zoneinfo import ZoneInfo

# ============== CONFIG DESDE ENV ==============
BOT_TOKEN = os.environ["BOT_TOKEN"]  # obligatorio

# Canales por defecto (puedes override con ENV)
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", "-1002859784457"))  # BORRADOR
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "-1002679848195"))  # PRINCIPAL

# Fallbacks (si no defines ENV, no se rompe)
BACKUP_FALLBACK = -1002717125281   # cambia si quieres
PREVIEW_FALLBACK = -1003042227035  # cambia si quieres

BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID", str(BACKUP_FALLBACK)))
PREVIEW_CHAT_ID = int(os.environ.get("PREVIEW_CHAT_ID", str(PREVIEW_FALLBACK)))

DB_FILE = os.environ.get("DB_FILE", "drafts.db")

# Pausa base entre envíos (seg) para no rozar el flood control
PAUSE = float(os.environ.get("PAUSE", "0.6"))

# Zona horaria (24h). Recomendado "America/Bogota".
TZNAME = os.environ.get("TIMEZONE", "America/Bogota")
TZ = ZoneInfo(TZNAME)
