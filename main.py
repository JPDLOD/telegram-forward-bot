# -*- coding: utf-8 -*-
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

def _conn(db_file):
    return sqlite3.connect(db_file)

def init_db(db_file):
    with _conn(db_file) as c:
        c.executescript(SCHEMA)
        c.commit()

def save_draft(db_file, message_id, text, raw_json):
    with _conn(db_file) as c:
        c.execute("""
          INSERT OR REPLACE INTO drafts(message_id, text, raw_json, is_sent)
          VALUES (?, ?, ?, 0)
        """, (message_id, text, raw_json))
        c.commit()

def get_unsent_drafts(db_file):
    """Devuelve lista de (message_id, text, raw_json) en orden."""
    with _conn(db_file) as c:
        cur = c.execute("""
          SELECT message_id, text, raw_json
          FROM drafts
          WHERE is_sent=0
          ORDER BY message_id ASC
        """)
        rows = cur.fetchall()
    return rows

def mark_sent(db_file, ids):
    if not ids:
        return
    with _conn(db_file) as c:
        c.executemany("UPDATE drafts SET is_sent=1 WHERE message_id=?", [(i,) for i in ids])
        c.commit()

def list_drafts(db_file):
    """Para mostrar en /listar: (message_id, text)"""
    with _conn(db_file) as c:
        cur = c.execute("""
          SELECT message_id, COALESCE(text,'')
          FROM drafts
          WHERE is_sent=0
          ORDER BY message_id ASC
        """)
        rows = cur.fetchall()
    return rows

def delete_draft(db_file, message_id):
    with _conn(db_file) as c:
        c.execute("DELETE FROM drafts WHERE message_id=?", (message_id,))
        c.commit()
