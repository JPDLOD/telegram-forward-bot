# -*- coding: utf-8 -*-
# Base de datos de borradores (SQLite)
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  message_id INTEGER PRIMARY KEY,
  text TEXT,
  raw_json TEXT,
  is_sent INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def _conn(db_file: str):
    # isolation_level=None => autocommit más predecible en Render
    return sqlite3.connect(db_file, isolation_level=None)

def init_db(db_file: str) -> None:
    with _conn(db_file) as c:
        c.executescript(SCHEMA)

def save_draft(db_file: str, message_id: int, text: str, raw_json: str) -> None:
    with _conn(db_file) as c:
        c.execute(
            "INSERT OR REPLACE INTO drafts(message_id, text, raw_json, is_sent) VALUES (?, ?, ?, 0)",
            (message_id, text, raw_json),
        )

def get_unsent_drafts(db_file: str):
    """[(message_id, text, raw_json)] en orden."""
    with _conn(db_file) as c:
        cur = c.execute(
            "SELECT message_id, COALESCE(text,''), COALESCE(raw_json,'') "
            "FROM drafts WHERE is_sent=0 ORDER BY message_id ASC"
        )
        return cur.fetchall()

def mark_sent(db_file: str, ids) -> None:
    if not ids:
        return
    with _conn(db_file) as c:
        c.executemany("UPDATE drafts SET is_sent=1 WHERE message_id=?", [(i,) for i in ids])

def list_drafts(db_file: str):
    """
    Para /listar: [(message_id, text, raw_json)] solo PENDIENTES.
    """
    with _conn(db_file) as c:
        cur = c.execute(
            "SELECT message_id, COALESCE(text,''), COALESCE(raw_json,'') "
            "FROM drafts WHERE is_sent=0 ORDER BY message_id ASC"
        )
        return cur.fetchall()

def delete_draft(db_file: str, message_id: int) -> None:
    with _conn(db_file) as c:
        c.execute("DELETE FROM drafts WHERE message_id=?", (message_id,))
