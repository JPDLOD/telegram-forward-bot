# -*- coding: utf-8 -*-
# Base de datos de borradores (SQLite)
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  message_id INTEGER PRIMARY KEY,
  text TEXT,
  raw_json TEXT,
  is_sent INTEGER DEFAULT 0,
  is_deleted INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def _conn(db_file: str):
    # check_same_thread=False por si en el futuro usas hilos del JobQueue
    return sqlite3.connect(db_file, check_same_thread=False)

def init_db(db_file: str) -> None:
    with _conn(db_file) as c:
        c.executescript(SCHEMA)
        # Migración suave si ya existía la tabla sin is_deleted
        try:
            c.execute("ALTER TABLE drafts ADD COLUMN is_deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        c.commit()

def save_draft(db_file: str, message_id: int, text: str, raw_json: str) -> None:
    with _conn(db_file) as c:
        c.execute(
            "INSERT OR REPLACE INTO drafts(message_id, text, raw_json, is_sent, is_deleted) "
            "VALUES (?, ?, ?, COALESCE((SELECT is_sent FROM drafts WHERE message_id=?),0), 0)",
            (message_id, text, raw_json, message_id),
        )
        c.commit()

def get_unsent_drafts(db_file: str):
    """[(message_id, text, raw_json)] en orden (sólo pendientes y no borrados)."""
    with _conn(db_file) as c:
        cur = c.execute(
            "SELECT message_id, COALESCE(text,''), COALESCE(raw_json,'') "
            "FROM drafts WHERE is_sent=0 AND is_deleted=0 ORDER BY message_id ASC"
        )
        return cur.fetchall()

def mark_sent(db_file: str, ids) -> None:
    if not ids:
        return
    with _conn(db_file) as c:
        c.executemany("UPDATE drafts SET is_sent=1 WHERE message_id=?", [(i,) for i in ids])
        c.commit()

def list_drafts(db_file: str):
    """Para /listar: [(message_id, text)] (sólo pendientes y no borrados)"""
    with _conn(db_file) as c:
        cur = c.execute(
            "SELECT message_id, COALESCE(text,'') "
            "FROM drafts WHERE is_sent=0 AND is_deleted=0 ORDER BY message_id ASC"
        )
        return cur.fetchall()

def mark_deleted(db_file: str, message_id: int) -> None:
    with _conn(db_file) as c:
        c.execute("UPDATE drafts SET is_deleted=1 WHERE message_id=?", (message_id,))
        c.commit()

def restore_draft(db_file: str, message_id: int) -> None:
    with _conn(db_file) as c:
        c.execute("UPDATE drafts SET is_deleted=0 WHERE message_id=?", (message_id,))
        c.commit()

def get_last_deleted(db_file: str):
    """Devuelve el último message_id borrado (pero no enviado) o None."""
    with _conn(db_file) as c:
        cur = c.execute(
            "SELECT message_id FROM drafts "
            "WHERE is_sent=0 AND is_deleted=1 ORDER BY message_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row else None
