# -*- coding: utf-8 -*-
import sqlite3
from typing import List, Tuple, Optional

_schema = """
CREATE TABLE IF NOT EXISTS drafts (
  message_id INTEGER PRIMARY KEY,
  snippet    TEXT,
  raw_json   TEXT,
  sent       INTEGER NOT NULL DEFAULT 0,
  deleted    INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_drafts_sent_deleted ON drafts(sent, deleted);
"""

_conn_cache = {}

def _conn(path: str) -> sqlite3.Connection:
    conn = _conn_cache.get(path)
    if conn:
        return conn
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    _conn_cache[path] = conn
    return conn

def init_db(path: str):
    c = _conn(path)
    c.executescript(_schema)
    c.commit()

def save_draft(path: str, message_id: int, snippet: str, raw_json: str):
    c = _conn(path)
    c.execute(
        "INSERT OR IGNORE INTO drafts(message_id, snippet, raw_json) VALUES (?,?,?)",
        (message_id, snippet or "", raw_json or "")
    )
    c.commit()

def get_unsent_drafts(path: str) -> List[Tuple[int, str, str]]:
    c = _conn(path)
    cur = c.execute(
        "SELECT message_id, snippet, raw_json FROM drafts "
        "WHERE sent=0 AND deleted=0 "
        "ORDER BY created_at ASC"
    )
    return list(cur.fetchall())

def mark_sent(path: str, ids: List[int]):
    if not ids:
        return
    c = _conn(path)
    q = "UPDATE drafts SET sent=1 WHERE message_id IN (%s)" % ",".join("?" * len(ids))
    c.execute(q, ids)
    c.commit()

def list_drafts(path: str) -> List[Tuple[int, str]]:
    c = _conn(path)
    cur = c.execute(
        "SELECT message_id, COALESCE(snippet,'') FROM drafts "
        "WHERE sent=0 AND deleted=0 "
        "ORDER BY created_at ASC"
    )
    return list(cur.fetchall())

def mark_deleted(path: str, message_id: int):
    c = _conn(path)
    c.execute("UPDATE drafts SET deleted=1 WHERE message_id=?", (message_id,))
    c.commit()

def restore_draft(path: str, message_id: int):
    c = _conn(path)
    c.execute("UPDATE drafts SET deleted=0 WHERE message_id=?", (message_id,))
    c.commit()

def get_last_deleted(path: str) -> Optional[int]:
    c = _conn(path)
    cur = c.execute(
        "SELECT message_id FROM drafts "
        "WHERE sent=0 AND deleted=1 "
        "ORDER BY message_id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return int(row[0]) if row else None

def count_deleted_unsent(path: str) -> int:
    c = _conn(path)
    cur = c.execute("SELECT COUNT(*) FROM drafts WHERE sent=0 AND deleted=1")
    row = cur.fetchone()
    return int(row[0] or 0)

def get_draft_snippet(path: str, message_id: int) -> Optional[str]:
    c = _conn(path)
    cur = c.execute("SELECT snippet FROM drafts WHERE message_id=?", (message_id,))
    row = cur.fetchone()
    return row[0] if row else None
