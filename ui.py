# -*- coding: utf-8 -*-
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID, TZNAME
from publisher import is_active_backup

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
        "🛠️ Acciones rápidas:\n"
        "• /listar — muestra borradores pendientes\n"
        "• /enviar — publica ahora a targets activos\n"
        "• /preview — manda la cola a PREVIEW sin marcarla como enviada\n"
        "• /programar YYYY-MM-DD HH:MM — programa lo que está en /listar (no mezcla con lo nuevo)\n"
        "• /programados — ver pendientes programados · /desprogramar <id|all>\n"
        "• /nuke …  • /cancelar  • /eliminar  • /id [id]\n"
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
