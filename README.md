# Telegram Forward Bot

Bot que permite trabajar con un **canal BORRADOR** y publicar en un **canal PRINCIPAL**:
- **Sin “Forwarded from …”** (republica/copias, no reenvía).
- **Mantiene el orden** exacto (texto → imagen → encuesta → link).
- **Reconstruye encuestas**, respeta **álbumes** (media groups).
- Soporta **lotes** (acumulas y envías cuando quieras) y **programación** por fecha/hora.
- Persistencia en **SQLite** (si el worker reinicia, no pierdes la cola).

## 🔑 IDs ya configurados

- **BORRADOR (SOURCE_CHAT_ID):** `-1002859784457`  
- **PRINCIPAL (TARGET_CHAT_ID):** `-1002679848195`

> Asegúrate de agregar el bot como **admin** en ambos canales con permiso para publicar.

---

## 🚀 Comandos (se escriben en el canal **BORRADOR**)

- `/nuevo_lote` — empieza un nuevo lote (opcional; por defecto ya hay uno “abierto”).  
- `/listar` — resumen de mensajes en cola (pendientes, encuestas, medios, excluidos).  
- `/remover <msg_id>` — excluye un mensaje de la cola. También puedes **responder** al mensaje con `/remover`.  
- `/enviar` (o `/enviar_casos_clinicos`) — publica ahora todo el lote en el **PRINCIPAL**.  
- `/programar YYYY-MM-DD HH:MM` — programa el envío del lote (hora Bogotá).  
- `/cancelar_programacion` — cancela la programación pendiente.  
- `/id` — devuelve los IDs de BORRADOR y PRINCIPAL.  
- `/ayuda` — muestra ayuda rápida.

> **Editar** mensajes en el BORRADOR antes de enviar actualiza la cola.  
> Si borras un mensaje en el BORRADOR, Telegram no notifica al bot; usa `/remover`.

---

## ⚙️ Requisitos

- Python 3.11+  
- `python-telegram-bot==21.6`  
- `aiosqlite==0.20.0`

Instalación local:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
