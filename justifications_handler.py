# -*- coding: utf-8 -*-
"""
Sistema de Justificaciones Protegidas
Maneja deep-links para enviar justificaciones específicas desde un canal de justificaciones
"""

import logging
import asyncio
from typing import Optional, Dict, Set
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from config import TZ

logger = logging.getLogger(__name__)

# ========= CONFIGURACIÓN DE JUSTIFICACIONES =========
JUSTIFICATIONS_CHAT_ID = -1003058530208  # Canal de justificaciones
AUTO_DELETE_MINUTES = 10  # Tiempo antes de borrar la justificación (0 = no borrar)

# Cache para rastrear mensajes enviados y sus timers de eliminación
sent_justifications: Dict[str, Dict] = {}  # {user_id_message_id: {chat_id, message_id, timer_task}}

# ========= FUNCIONES AUXILIARES =========

def generate_justification_deep_link(bot_username: str, message_id: int) -> str:
    """
    Genera el deep-link para una justificación específica.
    Formato: https://t.me/BotUsername?start=just_MESSAGE_ID
    """
    return f"https://t.me/{bot_username}?start=just_{message_id}"

def create_justification_button(bot_username: str, message_id: int) -> InlineKeyboardMarkup:
    """
    Crea el botón inline "Ver justificación 🔒" con deep-link.
    """
    deep_link = generate_justification_deep_link(bot_username, message_id)
    button = InlineKeyboardButton("Ver justificación 🔒", url=deep_link)
    return InlineKeyboardMarkup([[button]])

async def send_protected_justification(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    justification_message_id: int
) -> bool:
    """
    Envía una justificación protegida específica al usuario.
    
    Args:
        context: Contexto del bot
        user_id: ID del usuario que solicita la justificación
        justification_message_id: ID del mensaje en el canal de justificaciones
    
    Returns:
        bool: True si se envió exitosamente, False si falló
    """
    
    try:
        logger.info(f"📋 Enviando justificación {justification_message_id} a usuario {user_id}")
        
        # Copiar el mensaje desde el canal de justificaciones al usuario
        copied_message = await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=JUSTIFICATIONS_CHAT_ID,
            message_id=justification_message_id,
            protect_content=True  # PROTECCIÓN: No se puede copiar/reenviar/capturar
        )
        
        if not copied_message:
            logger.error(f"❌ No se pudo copiar justificación {justification_message_id}")
            return False
        
        logger.info(f"✅ Justificación {justification_message_id} enviada a {user_id} (mensaje {copied_message.message_id})")
        
        # Programar auto-eliminación si está configurada
        if AUTO_DELETE_MINUTES > 0:
            await schedule_message_deletion(
                context, 
                user_id, 
                copied_message.message_id, 
                justification_message_id
            )
        
        return True
        
    except TelegramError as e:
        if "chat not found" in str(e).lower():
            logger.warning(f"⚠️ Usuario {user_id} no ha iniciado chat con el bot")
        elif "message not found" in str(e).lower():
            logger.error(f"❌ Justificación {justification_message_id} no encontrada en canal")
        elif "not enough rights" in str(e).lower():
            logger.error(f"❌ Bot no tiene permisos en canal de justificaciones")
        else:
            logger.error(f"❌ Error enviando justificación: {e}")
        return False
    
    except Exception as e:
        logger.exception(f"❌ Error inesperado enviando justificación: {e}")
        return False

async def schedule_message_deletion(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    message_id: int,
    justification_id: int
):
    """
    Programa la eliminación automática de una justificación después del tiempo configurado.
    """
    
    # Crear una tarea asyncio para la eliminación
    async def delete_justification():
        try:
            # Esperar el tiempo configurado
            await asyncio.sleep(AUTO_DELETE_MINUTES * 60)
            
            # Intentar borrar el mensaje
            await context.bot.delete_message(chat_id=user_id, message_id=message_id)
            logger.info(f"🗑️ Auto-eliminada justificación {justification_id} del usuario {user_id}")
            
            # Notificar al usuario que se eliminó
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🕐 La justificación se ha eliminado automáticamente por seguridad.",
                    disable_notification=True
                )
            except:
                pass  # Si no se puede notificar, no importa
                
        except TelegramError as e:
            if "message not found" not in str(e).lower():
                logger.warning(f"⚠️ No se pudo auto-eliminar justificación: {e}")
        except Exception as e:
            logger.error(f"❌ Error en auto-eliminación: {e}")
        finally:
            # Limpiar del cache
            cache_key = f"{user_id}_{message_id}"
            sent_justifications.pop(cache_key, None)
    
    # Crear y guardar la tarea
    deletion_task = asyncio.create_task(delete_justification())
    cache_key = f"{user_id}_{message_id}"
    
    sent_justifications[cache_key] = {
        "user_id": user_id,
        "message_id": message_id,
        "justification_id": justification_id,
        "sent_at": datetime.now(tz=TZ),
        "deletion_task": deletion_task
    }
    
    logger.info(f"⏰ Programada auto-eliminación de justificación {justification_id} en {AUTO_DELETE_MINUTES} minutos")

async def handle_justification_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Maneja las solicitudes de justificación que llegan vía deep-link /start just_MESSAGE_ID
    
    Returns:
        bool: True si se manejó una solicitud de justificación, False si no era una solicitud válida
    """
    
    if not update.message or not update.message.text:
        return False
    
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    # Verificar si es una solicitud de justificación
    if not text.startswith("/start just_"):
        return False
    
    # Extraer el ID de la justificación
    try:
        justification_id_str = text.replace("/start just_", "")
        justification_id = int(justification_id_str)
    except ValueError:
        logger.warning(f"⚠️ ID de justificación inválido: {text}")
        await update.message.reply_text(
            "❌ Link de justificación inválido. Verifica que el enlace sea correcto."
        )
        return True
    
    logger.info(f"🔍 Solicitud de justificación {justification_id} por usuario {user_id}")
    
    # Enviar mensaje de "procesando"
    processing_msg = await update.message.reply_text(
        "🔄 Obteniendo justificación...",
        disable_notification=True
    )
    
    # Intentar enviar la justificación
    success = await send_protected_justification(context, user_id, justification_id)
    
    # Borrar el mensaje de "procesando"
    try:
        await processing_msg.delete()
    except:
        pass
    
    if success:
        # Mensaje de éxito con información adicional
        success_text = "✅ Justificación enviada con protección anti-copia."
        if AUTO_DELETE_MINUTES > 0:
            success_text += f"\n🕐 Se eliminará automáticamente en {AUTO_DELETE_MINUTES} minutos."
        
        await update.message.reply_text(
            success_text,
            disable_notification=True
        )
    else:
        await update.message.reply_text(
            "❌ No se pudo obtener la justificación. Puede que el enlace sea inválido o haya un problema temporal.",
            disable_notification=True
        )
    
    return True

# ========= COMANDOS ADMINISTRATIVOS =========

async def cmd_test_justification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando para probar el sistema de justificaciones.
    Uso: /test_just <message_id>
    """
    
    if not context.args:
        await update.message.reply_text("Uso: /test_just <message_id>")
        return
    
    try:
        message_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de mensaje inválido")
        return
    
    user_id = update.message.from_user.id
    success = await send_protected_justification(context, user_id, message_id)
    
    if success:
        await update.message.reply_text(f"✅ Justificación {message_id} enviada como prueba")
    else:
        await update.message.reply_text(f"❌ No se pudo enviar justificación {message_id}")

async def cmd_justification_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra estadísticas del sistema de justificaciones.
    """
    
    active_justifications = len(sent_justifications)
    
    stats_text = f"""
📊 **Estadísticas de Justificaciones**

🔒 Justificaciones activas: {active_justifications}
🕐 Auto-eliminación: {'ON' if AUTO_DELETE_MINUTES > 0 else 'OFF'}
📁 Canal justificaciones: `{JUSTIFICATIONS_CHAT_ID}`

⏰ Tiempo de auto-eliminación: {AUTO_DELETE_MINUTES} minutos
"""
    
    if active_justifications > 0:
        stats_text += "\n📋 **Activas actualmente:**\n"
        for cache_key, info in list(sent_justifications.items())[:5]:  # Mostrar solo las primeras 5
            sent_time = info['sent_at'].strftime("%H:%M:%S")
            stats_text += f"• Usuario {info['user_id']} - Justif {info['justification_id']} ({sent_time})\n"
        
        if active_justifications > 5:
            stats_text += f"... y {active_justifications - 5} más\n"
    
    await update.message.reply_text(stats_text, parse_mode="Markdown")

# ========= FUNCIÓN PARA INTEGRAR CON EL BOT PRINCIPAL =========

def add_justification_handlers(application):
    """
    Agrega los handlers de justificaciones al bot principal.
    Llamar esta función desde main.py después de crear la aplicación.
    """
    
    from telegram.ext import CommandHandler, MessageHandler, filters
    
    # Handler para /start just_ID (debe ir ANTES del handler general de /start)
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^/start just_\d+$"), 
        handle_justification_request
    ), group=0)  # Grupo 0 para que tenga prioridad
    
    # Comandos administrativos
    application.add_handler(CommandHandler("test_just", cmd_test_justification))
    application.add_handler(CommandHandler("just_stats", cmd_justification_stats))
    
    logger.info("✅ Handlers de justificaciones agregados al bot")

# ========= FUNCIÓN PARA USAR EN LAS ENCUESTAS =========

async def add_justification_button_to_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    justification_message_id: int
) -> bool:
    """
    Agrega un botón de justificación a una encuesta ya publicada.
    
    Args:
        context: Contexto del bot
        chat_id: ID del chat donde está la encuesta
        message_id: ID del mensaje de la encuesta
        justification_message_id: ID de la justificación en el canal de justificaciones
    
    Returns:
        bool: True si se agregó exitosamente
    """
    try:
        # Obtener info del bot para el deep-link
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        
        # Crear el botón
        keyboard = create_justification_button(bot_username, justification_message_id)
        
        # Actualizar la encuesta con el botón
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard
        )
        
        logger.info(f"✅ Botón de justificación agregado a encuesta {message_id} → justificación {justification_message_id}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error agregando botón de justificación: {e}")
        return False