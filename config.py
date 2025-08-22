# -*- coding: utf-8 -*-
import os
from zoneinfo import ZoneInfo

# =========================
# CONFIG DESDE ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]  # obligatorio

# Canal de borradores (donde escribes comandos y pegas contenido)
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", "-1002859784457"))

# Destinos
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "-1002679848195"))
BACKUP_CHAT_ID = int(os.environ.get("BACKUP_CHAT_ID", "-1002717125281"))
PREVIEW_CHAT_ID = int(os.environ.get("PREVIEW_CHAT_ID", "-1003042227035"))

# Base de datos
DB_FILE = os.environ.get("DB_FILE", "drafts.db")

# Pausa base entre copias para evitar flood control
PAUSE = float(os.environ.get("PAUSE", "0.6"))

# Zona horaria
TZNAME = os.environ.get("TIMEZONE", "America/Bogota")
TZ = ZoneInfo(TZNAME)
