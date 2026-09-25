from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path


def terms(text: str) -> list[str]:
    result = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.casefold()):
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word)
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            result.append(word)
    return result


def query_terms(text: str) -> str:
    words = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.casefold()):
        if '\u3400' <= word[0] <= '\u9fff' and len(word) > 1:
            words.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            words.append(word)
    return ' OR '.join('"' + w + '"' for w in dict.fromkeys(words))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Store:
    def __init__(self, directory: str):
        self.path = Path(directory) / "index.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          PRAGMA cache_size=-8192;
          CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL,
            path TEXT NOT NULL, name TEXT NOT NULL, extension TEXT NOT NULL DEFAULT '',
            size INTEGER, mtime_ns INTEGER, version TEXT, status TEXT NOT NULL,
            reason TEXT, indexed_at TEXT, seen TEXT, locator TEXT NOT NULL DEFAULT '{}');
          CREATE INDEX IF NOT EXISTS docs_source ON documents(source_id);
          CREATE INDEX IF NOT EXISTS docs_ext ON documents(extension);
          CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            text TEXT NOT NULL, hash TEXT NOT NULL, locator TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
          CREATE INDEX IF NOT EXISTS chunks_hash ON chunks(hash);
          CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
          CREATE VIRTUAL TABLE IF NOT EXISTS paths_fts USING fts5(path, tokenize='trigram');
          CREATE TABLE IF NOT EXISTS embeddings(hash TEXT PRIMARY KEY, model TEXT NOT NULL, vector BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def setting(self, key: str, default: str = '') -> str:
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]['value'] if rows else default

    def set_setting(self, key: str, value: str):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))

    def clear_chunks(self, doc_id: int):
        self.db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def remove(self, doc_ids: list[int]):
        with self.lock, self.db:
            for doc_id in doc_ids:
                self.clear_chunks(doc_id)
                self.db.execute("DELETE FROM paths_fts WHERE rowid=?", (doc_id,))
                self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def close(self):
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
