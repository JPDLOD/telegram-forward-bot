# -*- coding: utf-8 -*-
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID
from publisher import ACTIVE_BACKUP

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
        "🛠️ Comandos:\n"
        "• /listar — muestra borradores pendientes (excluye los programados)\n"
        "• /cancelar <id> — o responde con /cancelar (quita de la cola sin borrar del canal)\n"
        "• /deshacer [id] — revierte un /cancelar (o responde)\n"
        "• /eliminar <id> — o responde (BORRA del canal y de la cola)  [alias: /del, /delete, /remove, /borrar]\n"
        "• /nuke all|todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N(últimos)\n"
        "• /enviar — publica ahora a targets activos (los programados NO se mezclan)\n"
        "• /preview — manda la cola a PREVIEW sin marcar como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa lo que está en /listar (formato 24h, sin AM/PM)\n"
        "• /programados — muestra programaciones pendientes y cuánto falta\n"
        "• /desprogramar <id|all> — cancela por id o todas\n"
        "• /id [id] — info del mensaje o, si respondes, te dice el ID\n"
        "• /canales — IDs + estado de targets (alias: /targets, /where)\n"
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
        "Elige un atajo o usa `/programar YYYY-MM-DD HH:MM` (formato 24h: 00:00–23:59, sin '(24h)' ni AM/PM).\n"
        "⚠️ Si no hay borradores, no se programa nada."
    )
