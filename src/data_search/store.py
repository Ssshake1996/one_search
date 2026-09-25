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


def pack_vector(values) -> bytes:
    import numpy as np
    vector = np.asarray(values, dtype=np.float16)
    if vector.shape != (512,) or not np.isfinite(vector).all():
        raise ValueError('expected finite 512-dimensional embedding')
    return vector.tobytes()


def unpack_vector(blob):
    import numpy as np
    if len(blob) not in (1024, 2048):
        raise ValueError('expected finite 512-dimensional embedding')
    vector = np.frombuffer(blob, dtype=np.float16 if len(blob) == 1024 else np.float32)
    if not np.isfinite(vector).all():
        raise ValueError('expected finite 512-dimensional embedding')
    return vector


class Store:
    def __init__(self, directory: str, budget=None):
        self.path = Path(directory) / "index.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('search_tokens', 1, lambda value: ' '.join(terms(value)), deterministic=True)
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
          CREATE INDEX IF NOT EXISTS docs_source_table_id ON documents(source_id,path,id);
          CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            text TEXT NOT NULL, hash TEXT NOT NULL, locator TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
          CREATE INDEX IF NOT EXISTS chunks_hash ON chunks(hash);
          CREATE TABLE IF NOT EXISTS embeddings(hash TEXT PRIMARY KEY, model TEXT NOT NULL, vector BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()
        if not any(r[1] == 'semantic' for r in self.db.execute('PRAGMA table_info(chunks)')):
            self.db.execute('ALTER TABLE chunks ADD COLUMN semantic INTEGER NOT NULL DEFAULT 1')
            self.db.commit()
        # External-content FTS keeps just the inverted index, not a second copy
        # of Chinese unigram/bigram token text or file paths. Views reconstruct
        # tokens for rebuild/integrity checks from the canonical source rows.
        schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()
        if schema is None or "content='chunk_tokens'" not in schema[0]:
            if schema is not None and budget is not None:
                budget.check(disk=True, reserve_mb=max(16, self.path.stat().st_size/1048576))
            with self.db:
                # sqlite3's implicit transaction starts at DML, not DDL. Start
                # explicitly so an interrupted rebuild restores the old indexes.
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('DROP TABLE IF EXISTS chunks_fts')
                self.db.execute('DROP TABLE IF EXISTS paths_fts')
                self.db.execute('CREATE VIEW IF NOT EXISTS chunk_tokens AS SELECT id,search_tokens(text) tokens FROM chunks')
                self.db.execute("CREATE VIEW IF NOT EXISTS file_paths AS SELECT id,path FROM documents WHERE source_id='files'")
                self.db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens,content='chunk_tokens',content_rowid='id')")
                self.db.execute("CREATE VIRTUAL TABLE paths_fts USING fts5(path,content='file_paths',content_rowid='id',tokenize='trigram')")
                self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                self.db.execute("INSERT INTO paths_fts(paths_fts) VALUES('rebuild')")
        self.db.executescript("""
          CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid,tokens) VALUES(new.id,search_tokens(new.text));
          END;
          CREATE TRIGGER IF NOT EXISTS chunks_delete BEFORE DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts,rowid,tokens) VALUES('delete',old.id,search_tokens(old.text));
          END;
          CREATE TRIGGER IF NOT EXISTS chunks_update AFTER UPDATE OF text ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts,rowid,tokens) VALUES('delete',old.id,search_tokens(old.text));
            INSERT INTO chunks_fts(rowid,tokens) VALUES(new.id,search_tokens(new.text));
          END;
          CREATE TRIGGER IF NOT EXISTS paths_insert AFTER INSERT ON documents WHEN new.source_id='files' BEGIN
            INSERT INTO paths_fts(rowid,path) VALUES(new.id,new.path);
          END;
          CREATE TRIGGER IF NOT EXISTS paths_delete BEFORE DELETE ON documents WHEN old.source_id='files' BEGIN
            INSERT INTO paths_fts(paths_fts,rowid,path) VALUES('delete',old.id,old.path);
          END;
          CREATE TRIGGER IF NOT EXISTS paths_update AFTER UPDATE OF path,source_id ON documents BEGIN
            INSERT INTO paths_fts(paths_fts,rowid,path) SELECT 'delete',old.id,old.path WHERE old.source_id='files';
            INSERT INTO paths_fts(rowid,path) SELECT new.id,new.path WHERE new.source_id='files';
          END;
        """)

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
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def remove(self, doc_ids: list[int]):
        with self.lock, self.db:
            for doc_id in doc_ids:
                self.clear_chunks(doc_id)
                self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def compact(self):
        """Offline maintenance; caller holds the service instance lock."""
        with self.lock:
            before = self.path.stat().st_size
            after = 0
            while True:
                rows = self.db.execute('SELECT rowid,vector FROM embeddings WHERE rowid>? AND length(vector)=2048 ORDER BY rowid LIMIT 256', (after,)).fetchall()
                if not rows:
                    break
                with self.db:
                    for row in rows:
                        self.db.execute('UPDATE embeddings SET vector=? WHERE rowid=?', (pack_vector(unpack_vector(row[1])), row[0]))
                after = rows[-1][0]
            with self.db:
                self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
                self.db.execute("INSERT INTO paths_fts(paths_fts) VALUES('optimize')")
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.db.execute('VACUUM')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            return {'before_bytes': before, 'after_bytes': self.path.stat().st_size}

    def close(self):
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
