# -*- coding: utf-8 -*-
# BORRADOR (SOURCE_CHAT_ID) -> PRINCIPAL (TARGET_CHAT_ID) (+ BACKUP opcional)
# Mantiene todas las funciones anteriores, ahora modularizadas.

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Set

from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, CallbackQueryHandler, filters
from telegram.error import TelegramError

from config import BOT_TOKEN, SOURCE_CHAT_ID, BACKUP_CHAT_ID, PREVIEW_CHAT_ID, TZ, TZNAME, DB_FILE
from database import (
    init_db, save_draft, get_unsent_drafts, mark_sent, list_drafts,
    mark_deleted, restore_draft, get_last_deleted
)

from core_utils import (
    temp_notice, safe_sleep, parse_programar_when, delete_later,
    deep_link_for_channel_message, get_draft_row, hard_delete_draft
)
from publisher import (
    publicar, publicar_ids, get_active_targets, ACTIVE_BACKUP, STATS, LAST_BATCH, SCHEDULED_LOCK
)
from scheduler import (
    schedule_ids, cmd_programar, cmd_programados, cmd_desprogramar, SCHEDULES
)
from ui import kb_main, kb_settings, kb_schedule, text_main, text_settings, text_schedule

# ========= LOGGING =========
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DB =========
init_db(DB_FILE)
logger.info(
    f"SQLite listo. BORRADOR={SOURCE_CHAT_ID}  BACKUP={BACKUP_CHAT_ID}  PREVIEW={PREVIEW_CHAT_ID}  "
    f"TZ={TZNAME}"
)

# -------------------------------------------------------
# Comandos de utilidad
# -------------------------------------------------------
def _is_command_text(txt: Optional[str]) -> bool:
    return bool(txt and txt.strip().startswith("/"))

def _extract_id_from_text(txt: str) -> Optional[int]:
    parts = (txt or "").split()
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
        if p.lower().startswith("id:"):
            n = p.split(":", 1)[1]
            if n.isdigit():
                return int(n)
    return None

async def _cmd_listar(context: ContextTypes.DEFAULT_TYPE):
    drafts = list_drafts(DB_FILE)  # [(id, text)]
    out = []
    if drafts:
        out.append("📋 Borradores pendientes:")
        for i, (did, snip) in enumerate(drafts, start=1):
            s = (snip or "").strip()
            if len(s) > 60:
                s = s[:60] + "…"
            out.append(f"• {i:>2} — {s or '[contenido]'}  (id:{did})")
    else:
        out.append("📋 Borradores pendientes: 0")

    # Programaciones
    if not SCHEDULES:
        out.append("\n🗒 Programaciones pendientes: 0")
    else:
        out.append("\n🗒 Programaciones pendientes:")
        from core_utils import human_eta
        now = datetime.now(tz=TZ)
        for pid, rec in sorted(SCHEDULES.items()):
            when = rec["when"]
            ids = rec["ids"]
            eta = human_eta(when, now)
            out.append(f"• #{pid} — {when.astimezone(TZ):%Y-%m-%d %H:%M} ({TZNAME}) — {eta} — {len(ids)} mensajes")

    await context.bot.send_message(SOURCE_CHAT_ID, "\n".join(out))

async def _cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /cancelar <id> o responde al mensaje a cancelar.")
        return
    mark_deleted(DB_FILE, mid)
    SCHEDULED_LOCK.discard(mid)
    STATS["cancelados"] += 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"🚫 Cancelado id:{mid}. Quedan {restantes} en la cola.", ttl=6)

async def _cmd_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        await context.bot.send_message(SOURCE_CHAT_ID, "❌ Usa: /eliminar <id> o responde al mensaje a eliminar.")
        return

    ok_del = True
    try:
        await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
    except TelegramError as e:
        ok_del = False
        logger.warning(f"No pude borrar en el canal id:{mid} → {e}")

    hard_delete_draft(mid)
    SCHEDULED_LOCK.discard(mid)
    STATS["eliminados"] += 1
    restantes = len(list_drafts(DB_FILE))
    txt_ok = "🗑️ Eliminado del canal y de la cola." if ok_del else "🗑️ Quitado de la cola (no pude borrar en el canal)."
    await temp_notice(context, f"{txt_ok} id:{mid}. Quedan {restantes} en la cola.", ttl=7)

async def _cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str):
    mid = _extract_id_from_text(txt)
    if not mid and update.channel_post and update.channel_post.reply_to_message:
        mid = update.channel_post.reply_to_message.message_id
    if not mid:
        mid = get_last_deleted(DB_FILE)
    if not mid:
        await temp_notice(context, "ℹ️ No hay nada para deshacer.", ttl=5)
        return
    restore_draft(DB_FILE, mid)
    if STATS["cancelados"] > 0:
        STATS["cancelados"] -= 1
    restantes = len(list_drafts(DB_FILE))
    await temp_notice(context, f"↩️ Restaurado id:{mid}. Ahora hay {restantes} en la cola.", ttl=6)

# ---------- NUKE ----------
import re
def _parse_nuke_selection(arg: str, drafts: List[Tuple[int, str]]) -> Set[int]:
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
    # Soportar "1,2,3" y "1, 2, 3"
    pieces = [p.strip() for p in arg.split(",") if p.strip()]
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

async def _cmd_nuke(context: ContextTypes.DEFAULT_TYPE, txt: str):
    parts = (txt or "").split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""
    drafts = list_drafts(DB_FILE)
    if not drafts:
        await context.bot.send_message(SOURCE_CHAT_ID, "No hay pendientes.")
        return
    victims: Set[int] = _parse_nuke_selection(arg, drafts)
    if not victims:
        await context.bot.send_message(
            SOURCE_CHAT_ID,
            "Usa: /nuke all | /nuke todos | /nuke 1,3,5 | /nuke 1-10 | /nuke N"
        )
        return
    borrados = 0
    for mid in sorted(victims, reverse=True):
        try:
            await context.bot.delete_message(chat_id=SOURCE_CHAT_ID, message_id=mid)
        except TelegramError as e:
            logger.warning(f"No pude borrar en el canal id:{mid} → {e}")
        hard_delete_draft(mid)
        SCHEDULED_LOCK.discard(mid)
        borrados += 1
    STATS["eliminados"] += borrados
    restantes = len(list_drafts(DB_FILE))
    await context.bot.send_message(SOURCE_CHAT_ID, f"💣 Nuke: {borrados} borrados. Quedan {restantes} en la cola.")

# -------------------------------------------------------
# Menús y callbacks
# -------------------------------------------------------
async def _send_help_with_buttons(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, text_main(), reply_markup=kb_main())

async def _send_settings_panel(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(SOURCE_CHAT_ID, text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    try:
        if data == "m:list":
            await _cmd_listar(context)
        elif data == "m:send":
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
            ok, fail, _posted = await publicar(context, targets=get_active_targets(), mark_as_sent=True)
            msg_out = f"✅ Publicados {ok}."
            extras = []
            if STATS["cancelados"]:
                extras.append(f"Cancelados: {STATS['cancelados']}")
            if STATS["eliminados"]:
                extras.append(f"Eliminados: {STATS['eliminados']}")
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            STATS["cancelados"] = 0
            STATS["eliminados"] = 0
        elif data == "m:preview":
            await temp_notice(context, "⏳ Generando preview…", ttl=3)
            pubs, fails, _ = await publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
        elif data == "m:sched":
            await q.edit_message_text(text_schedule(), reply_markup=kb_schedule())
        elif data == "m:settings":
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        elif data == "m:toggle_backup":
            from publisher import ACTIVE_BACKUP as AB
            # flip
            import publisher
            publisher.ACTIVE_BACKUP = not AB
            await q.edit_message_text(text_settings(), reply_markup=kb_settings(), parse_mode="Markdown")
        elif data == "m:back":
            await q.edit_message_text(text_main(), reply_markup=kb_main())

        elif data.startswith("s:"):
            now = datetime.now(tz=TZ)
            when = None
            if data == "s:+5":
                when = now + timedelta(minutes=5)
            elif data == "s:+15":
                when = now + timedelta(minutes=15)
            elif data == "s:today20":
                when = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if when <= now:
                    when = when + timedelta(days=1)
            elif data == "s:tom07":
                when = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            elif data == "s:list":
                await cmd_programados(context)
            elif data == "s:clear":
                await cmd_desprogramar(context, "all")
            elif data == "s:custom":
                await q.edit_message_text(
                    "✍️ Formato manual:\n`/programar YYYY-MM-DD HH:MM` (formato 24 h)\n\n⬅️ Usa *Volver* para regresar.",
                    parse_mode="Markdown", reply_markup=kb_schedule()
                )

            if when:
                ids = [did for (did, _snip) in list_drafts(DB_FILE)]
                if not ids:
                    await temp_notice(context, "📭 No hay borradores para programar.", ttl=6)
                else:
                    # guardar metadatos para job
                    context.bot_data['source_chat_id'] = SOURCE_CHAT_ID
                    context.bot_data['db_file'] = DB_FILE
                    await schedule_ids(context, when, ids)

    except Exception as e:
        logger.exception(f"Error en callback: {e}")

# -------------------------------------------------------
# Handler de POSTS en el CANAL BORRADOR
# -------------------------------------------------------
async def handle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or msg.chat_id != SOURCE_CHAT_ID:
        return

    # Guarda referencias para jobs
    context.bot_data['source_chat_id'] = SOURCE_CHAT_ID
    context.bot_data['db_file'] = DB_FILE

    txt = (msg.text or "").strip()

    if _is_command_text(txt):
        low = txt.lower()

        # marcar si borramos el comando “ruidoso”
        delete_cmd = False

        if low.startswith("/listar") or low.startswith("/lista"):
            await _cmd_listar(context)
            delete_cmd = True

        elif low.startswith(("/cancelar", "/cancel", "/skip")):
            await _cmd_cancelar(update, context, txt)
            delete_cmd = True

        elif low.startswith(("/eliminar", "/del", "/delete", "/remove", "/borrar")):
            await _cmd_eliminar(update, context, txt)
            delete_cmd = True

        elif low.startswith(("/deshacer", "/undo", "/restaurar")):
            await _cmd_deshacer(update, context, txt)
            delete_cmd = True

        elif low.startswith("/nuke") or low.strip() in ("/all", "/todos"):
            use_txt = txt if low.startswith("/nuke") else "/nuke all"
            await _cmd_nuke(context, use_txt)
            delete_cmd = True

        elif low.startswith("/enviar"):
            await temp_notice(context, "⏳ Procesando envío…", ttl=4)
            ok, fail, _ = await publicar(context, targets=get_active_targets(), mark_as_sent=True)
            extras = []
            if STATS["cancelados"]:
                extras.append(f"Cancelados: {STATS['cancelados']}")
            if STATS["eliminados"]:
                extras.append(f"Eliminados: {STATS['eliminados']}")
            msg_out = f"✅ Publicados {ok}."
            if fail:
                extras.append(f"Fallidos: {fail}")
            if extras:
                msg_out += "\n📦 " + " · ".join(extras) + "."
            await context.bot.send_message(SOURCE_CHAT_ID, msg_out)
            STATS["cancelados"] = 0
            STATS["eliminados"] = 0
            delete_cmd = True

        elif low.startswith("/preview"):
            pubs, fails, _ = await publicar(context, targets=[PREVIEW_CHAT_ID], mark_as_sent=False)
            await context.bot.send_message(SOURCE_CHAT_ID, f"🧪 Preview: enviados {pubs}, fallidos {fails}.")
            delete_cmd = True

        elif low.startswith("/programar"):
            # Aceptar H y HH
            parts = txt.split(maxsplit=1)
            norm = parse_programar_when(parts[1] if len(parts) > 1 else "")
            if not norm:
                await context.bot.send_message(
                    SOURCE_CHAT_ID,
                    "❌ Formato inválido. Usa: `/programar YYYY-MM-DD HH:MM`  (formato 24 h)",
                    parse_mode="Markdown"
                )
            else:
                context.bot_data['source_chat_id'] = SOURCE_CHAT_ID
                context.bot_data['db_file'] = DB_FILE
                await cmd_programar(context, norm)
            delete_cmd = True

        elif low.startswith("/programados"):
            await cmd_programados(context)
            delete_cmd = True

        elif low.startswith("/desprogramar"):
            parts = txt.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await cmd_desprogramar(context, arg)
            delete_cmd = True

        elif low.startswith("/id"):
            if msg.reply_to_message and len((txt or "").split()) == 1:
                rid = msg.reply_to_message.message_id
                await context.bot.send_message(SOURCE_CHAT_ID, f"🆔 ID del mensaje: {rid}")
            else:
                mid = _extract_id_from_text(txt) or (txt.split()[1] if len(txt.split()) > 1 and txt.split()[1].isdigit() else None)
                if not mid:
                    await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /id <id> o responde a un mensaje con /id.")
                else:
                    mid = int(mid)
                    row = get_draft_row(mid)
                    snippet = "[contenido]"
                    tipo = "desconocido"
                    fecha = ""
                    if row:
                        try:
                            _, text, raw_json, _sent, _deleted, created_at = row
                            if text:
                                snippet = (text.strip()[:180] + "…") if len(text.strip()) > 180 else text.strip()
                            if raw_json:
                                d = json.loads(raw_json)
                                if "poll" in d:
                                    tipo = "quiz" if (d.get("poll", {}).get("type") == "quiz") else "encuesta"
                                elif "photo" in d:
                                    tipo = "foto"
                                elif "document" in d:
                                    tipo = "documento"
                                elif "video" in d:
                                    tipo = "video"
                                else:
                                    tipo = "texto"
                            if created_at:
                                from datetime import datetime as _dt
                                try:
                                    dt = _dt.fromisoformat(created_at)
                                    fecha = dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
                                except Exception:
                                    fecha = created_at
                        except Exception:
                            pass
                    link = deep_link_for_channel_message(SOURCE_CHAT_ID, mid)
                    out = f"🆔 {mid}\n• Tipo: {tipo}\n• Snippet: {snippet}\n• Fecha: {fecha}\n• Enlace: {link}"
                    await context.bot.send_message(SOURCE_CHAT_ID, out)
            delete_cmd = True

        elif low.startswith(("/canales", "/targets", "/where")):
            await _send_settings_panel(context)

        elif low.startswith("/backup"):
            parts = txt.split(maxsplit=1)
            v = (parts[1] if len(parts) > 1 else "").strip().lower()
            import publisher
            if v in ("on", "1", "true", "si", "sí"):
                publisher.ACTIVE_BACKUP = True
            elif v in ("off", "0", "false", "no"):
                publisher.ACTIVE_BACKUP = False
            else:
                await context.bot.send_message(SOURCE_CHAT_ID, "Usa: /backup on|off")
                return
            await _send_settings_panel(context)
            delete_cmd = True

        elif low.startswith(("/comandos", "/comando", "/ayuda", "/start")):
            await _send_help_with_buttons(context)

        else:
            await context.bot.send_message(SOURCE_CHAT_ID, "Comando no reconocido. Usa /comandos.")

        # Limpia el mensaje de comando si corresponde
        if delete_cmd:
            await delete_later(context, SOURCE_CHAT_ID, msg.message_id, delay=3)
        return

    # --------- NO ES COMANDO → GUARDAR BORRADOR ----------
    snippet = msg.text or msg.caption or ""
    raw_json = msg.to_dict()
    import json as _json
    save_draft(DB_FILE, msg.message_id, snippet, _json.dumps(raw_json, ensure_ascii=False))
    logger.info(f"Guardado en borrador: {msg.message_id}")

# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no capturada", exc_info=context.error)

# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_error_handler(on_error)

    logger.info("Bot iniciado 🚀 Escuchando channel_post en el BORRADOR.")
    app.run_polling(allowed_updates=["channel_post", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
