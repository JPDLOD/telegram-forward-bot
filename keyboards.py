# -*- coding: utf-8 -*-
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID
from publisher import is_active_backup  # lee el estado en tiempo real

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
        "🛠️ Comandos (formato exacto, sin abreviar):\n"
        "• /listar — muestra borradores pendientes (excluye los programados)\n"
        "• /enviar — publica ahora a targets activos (principal y, si ON, backup)\n"
        "• /preview — manda la cola a PREVIEW sin marcar como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa lo que está en /listar (formato 24h: 00:00–23:59, sin '(24h)' ni AM/PM). Bloquea esos IDs hasta ejecutarse y no se mezclan con nuevos.\n"
        "• /programados — muestra las programaciones pendientes con su cantidad e ETA\n"
        "• /desprogramar <id|all> — cancela una programación por ID o todas\n"
        "• /cancelar <id> — quita de la cola (no borra del canal). También puedes responder a un mensaje con /cancelar\n"
        "• /deshacer [id] — revierte el último /cancelar o el que indiques (no aplica a /eliminar)\n"
        "• /eliminar <id> — borra del canal y de la cola (alias: /del, /delete, /remove, /borrar)\n"
        "• /nuke all|todos — borra todos los pendientes; /nuke 1,3,5 — borra esas posiciones; /nuke 1-10 — borra ese rango; /nuke N — borra los últimos N\n"
        "• /id [id] — info del mensaje (si respondes con /id, te da el ID; si pasas un id, te da el deep‑link)\n"
        "• /canales — muestra los IDs y estado ON/OFF de los targets (alias: /targets, /where)\n"
        "• /backup on|off — alterna SOLO el backup (principal siempre ON)\n\n"
        "Pulsa un botón o usa /comandos para volver a ver este panel."
    )

def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="m:toggle_backup")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
        ]
    )

def text_settings() -> str:
    onoff = "ON" if is_active_backup() else "OFF"
    return (
        f"📡 **Targets**\n"
        f"• Principal: `{TARGET_CHAT_ID}` **ON** (fijo)\n"
        f"• Backup   : `{BACKUP_CHAT_ID}` **{onoff}**\n"
        f"• Preview  : `{PREVIEW_CHAT_ID}`\n\n"
        "Usa el botón para alternar backup.\n"
        "⬅️ *Volver* regresa al menú principal."
    )
