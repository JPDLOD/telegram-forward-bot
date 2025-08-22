# -*- coding: utf-8 -*-
"""
ui.py
-----
Construcción de textos/teclados y handlers de UI.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List

import publisher  # <<< usamos el módulo, no importamos ACTIVE_BACKUP por valor
from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID


# ===== Keyboards =====
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

def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔀 Backup ON/OFF", callback_data="m:toggle_backup")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m:back")]
        ]
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


# ===== Textos =====
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

def _onoff(val: bool) -> str:
    return "ON" if val else "OFF"

def text_settings() -> str:
    # leemos SIEMPRE del módulo publisher (única fuente de verdad)
    return (
        f"📡 **Targets**\n"
        f"• Principal: `{TARGET_CHAT_ID}` **ON** (fijo)\n"
        f"• Backup   : `{BACKUP_CHAT_ID}` **{_onoff(publisher.is_backup_on())}**\n"
        f"• Preview  : `{PREVIEW_CHAT_ID}`\n\n"
        "Usa el botón para alternar backup.\n"
        "⬅️ *Volver* regresa al menú principal."
    )


# ===== helpers de envío de paneles =====
async def send_help_with_buttons(context):
    from config import SOURCE_CHAT_ID
    await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main())

async def send_settings_panel(context):
    from config import SOURCE_CHAT_ID
    await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")


# ===== handlers de comandos UI relacionados al backup =====
async def handle_backup_command(context, arg: str):
    """
    /backup on|off
    Cambia el estado global en publisher y vuelve a mostrar el panel.
    """
    from config import SOURCE_CHAT_ID
    v = (arg or "").strip().lower()
    if v in ("on", "1", "true", "si", "sí"):
        publisher.set_active_backup(True)
    elif v in ("off", "0", "false", "no"):
        publisher.set_active_backup(False)
    else:
        await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
        return
    # mostrar panel con estado actualizado
    await send_settings_panel(context)
