# -*- coding: utf-8 -*-
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import TARGET_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID
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
        "🛠️ Comandos:\n"
        "• /listar — muestra **borradores pendientes** (excluye programados) con su `id` para operar.\n"
        "• /enviar — publica **ahora** los borradores a los targets activos (principal y, si está ON, backup).\n"
        "• /preview — envía toda la cola a **PREVIEW** sin marcar como enviada (útil para revisar antes de publicar).\n"
        "• /programar YYYY-MM-DD HH:MM — programa lo que está en **/listar** (formato 24h, sin AM/PM). Bloquea esos IDs y no se mezclan con nuevos.\n"
        "• /programados — muestra **programaciones pendientes** con hora (TZ) y cantidad de mensajes.\n"
        "• /desprogramar <id|all> — cancela **una** programación por id o **todas** las programaciones pendientes.\n"
        "• /cancelar <id> — (o responde con /cancelar) saca **de la cola** un borrador (no lo borra del canal).\n"
        "• /deshacer [id] — revierte el último **/cancelar** (o el id indicado). No aplica si se usó /eliminar.\n"
        "• /eliminar <id> — (o responde) **borra del canal** y lo quita de la cola definitivamente. [alias: /del, /delete, /remove, /borrar]\n"
        "• /nuke all|todos — elimina **todos** los borradores pendientes.\n"
        "• /nuke 1,3,5 — elimina las posiciones indicadas de **/listar**.\n"
        "• /nuke 2-7 — elimina un **rango** de posiciones de **/listar**.\n"
        "• /nuke N — elimina los **últimos N** pendientes.\n"
        "• /id [id] — si respondes a un mensaje con /id te muestra su ID; con parámetro, te da el enlace directo.\n"
        "• /canales — IDs y **estado de targets** (principal fijo ON, backup ON/OFF, preview).\n"
        "• /backup on|off — activa o desactiva **solo** el backup (el principal siempre ON).\n"
        "• Atajo botón `@@@ TÍTULO | URL` — borra esa línea en BORRADOR y añade un **botón** al último borrador pendiente con ese TÍTULO → URL.\n"
        "\nPulsa un botón o usa /comandos para ver este panel nuevamente."
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
