from __future__ import annotations

import json
import mmap
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .config import atomic_json
from .model import MODEL_ID
from .resources import ResourceLimit
from .store import unpack_vector


class Vectors:
    """Immutable ANN snapshots built outside the query and SQLite writer locks.

    Production builds use a monitored disposable process. The previous version
    stays searchable; Engine checks current document IDs and scope on every hit.
    Only a small manifest is atomically replaced after a complete snapshot save.
    """
    SCHEMA_VERSION = 3

    def __init__(self, store, budget, *, local_io=False):
        self.store, self.budget = store, budget
        self.meta = store.path.with_name('vectors.json')
        self.dirty = store.path.with_name('vectors.dirty')
        self.reader = self.generation = self.reader_path = None
        self.last_sync = None
        self.lock = threading.RLock()
        self.worker = None
        self.building = False
        self.local_io = local_io

    def _native_path(self, path):
        # Only the disposable, single-request worker may change cwd. USearch's
        # Windows C narrow-path API cannot open non-ASCII parent directories.
        return path.name if self.local_io else str(path)

    def _restore(self, path, *, view):
        from usearch.index import Index
        native = self._native_path(path)
        if os.name != 'nt' or native.isascii():
            return Index.restore(native,view=view)
        # Python opens Windows paths with wide APIs; a file-backed buffer keeps
        # query readers zero-copy even under a non-ASCII user/profile directory.
        with path.open('rb') as stream:
            mapping = mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
        try:
            index = Index.restore(mapping,view=view)
            if view and index is not None:
                index._data_search_mapping = mapping
            else:
                mapping.close()
            return index
        except Exception:
            mapping.close()
            raise

    @property
    def path(self):
        metadata = self._metadata()
        return self.store.path.with_name(metadata['snapshot']) if metadata else self.store.path.with_name('vectors.usearch')

    def close(self):
        if self.worker:
            self.worker.close()
        with self.lock:
            self.reader = self.reader_path = self.generation = None

    def cancel(self):
        if self.worker:
            self.worker.cancel()

    def _metadata(self):
        if self.dirty.exists():
            return None
        try:
            value = json.loads(self.meta.read_text(encoding='utf-8'))
            name = value['snapshot']
            if (not isinstance(name, str) or Path(name).name != name or not name.startswith('vectors-')
                    or not name.endswith('.usearch') or value.get('schema_version') != self.SCHEMA_VERSION
                    or value.get('model') != MODEL_ID or not isinstance(value.get('generation'),str)
                    or isinstance(value.get('count'),bool) or not isinstance(value.get('count'),int)
                    or value['count']<0):
                return None
            stat = self.store.path.with_name(name).stat()
            if value.get('snapshot_bytes') != stat.st_size or value.get('snapshot_mtime_ns') != stat.st_mtime_ns:
                return None
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def status(self):
        meta = self._metadata()
        return {'building': self.building, 'published_chunks': meta['count'] if meta else 0,
                'pending': meta is None or meta['generation'] != self.store.setting('vector_generation', '0'),
                'last_sync': self.last_sync}

    @staticmethod
    def _flush(path):
        with path.open('r+b') as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _flush_directory(path):
        if os.name != 'nt':
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _publish(self, index, generation, added, removed, rebuilt):
        previous = self._metadata()
        path = self.store.path.with_name(f'vectors-{uuid.uuid4().hex}.usearch')
        self.budget.check(disk=True, reserve_mb=max(8, max(len(index), index.capacity) * .002))
        try:
            native = self._native_path(path)
            if os.name == 'nt' and not native.isascii():
                # Inline test/library builds only; production workers always
                # write an ASCII basename without an extra serialized copy.
                path.write_bytes(index.save())
            else:
                index.save(native)
            self._flush(path)
            stat = path.stat()
            atomic_json(self.meta, {'schema_version': self.SCHEMA_VERSION,
                'generation': generation, 'model': MODEL_ID, 'count': len(index),
                'previous_snapshot':previous['snapshot'] if previous else None,
                'snapshot': path.name, 'snapshot_bytes': stat.st_size,
                'snapshot_mtime_ns': stat.st_mtime_ns})
            self._flush(self.meta)
            self._flush_directory(path.parent)
        except Exception:
            current = self._metadata()
            if not current or current.get('snapshot') != path.name:
                path.unlink(missing_ok=True)
            raise
        self.dirty.unlink(missing_ok=True)
        keep = {path.name, previous['snapshot'] if previous else ''}
        for stale in path.parent.glob('vectors-*.usearch'):
            if stale.name not in keep:
                try:
                    stale.unlink()
                except OSError:
                    pass  # A Windows reader may still map the previous file.
        try:
            (path.parent / 'vectors.usearch').unlink(missing_ok=True)
        except OSError:
            pass
        self.last_sync = {'rebuilt': rebuilt, 'added': added, 'removed': removed, 'count': len(index)}

    def sync(self, cancelled=None, *, isolated=None):
        if isolated is None:
            isolated = hasattr(self.budget, 'config')
        self.building = True
        try:
            if isolated:
                from .workers import Worker
                if self.worker is None:
                    self.worker = Worker(self.budget)
                try:
                    self.last_sync = self.worker.request({'method': 'vector_sync', 'config': self.budget.config},
                                                        timeout=3600, cancelled=cancelled)
                finally:
                    self.worker.close()
            else:
                self._build(cancelled)
        finally:
            self.building = False

    def _build(self, cancelled=None):
        import numpy as np
        from usearch.index import Index

        def checkpoint():
            if cancelled is not None and cancelled():
                raise ResourceLimit('indexing_paused_or_stopping')
            self.budget.check()

        checkpoint()
        metadata = self._metadata()
        keep = {metadata['snapshot'],metadata.get('previous_snapshot')} if metadata else set()
        for orphan in self.store.path.parent.glob('vectors-*.usearch'):
            if orphan.name not in keep:
                try:
                    orphan.unlink()
                except OSError:
                    pass
        check_db = sqlite3.connect(self.store.path.as_uri()+'?mode=ro',uri=True)
        try:
            row = check_db.execute("SELECT value FROM settings WHERE key='vector_generation'").fetchone()
            current_generation = row[0] if row else '0'
        finally:
            check_db.close()
        if metadata and metadata['generation'] == current_generation:
            try:
                probe = self._restore(self.path,view=True)
                if probe is not None and len(probe)==metadata['count'] and probe.ndim==512:
                    self.last_sync = {'rebuilt':False,'added':0,'removed':0,'count':len(probe)}
                    return
            except Exception:
                pass
        index = None
        if metadata:
            try:
                index = self._restore(self.path,view=False)
                if index is None or len(index) != metadata['count'] or index.ndim != 512:
                    index = None
            except Exception:
                index = None
        rebuilt = index is None
        if index is None:
            index = Index(ndim=512, metric='cos', dtype='f16', connectivity=16)
        # Separate read snapshot, and an on-disk key table instead of giant sets.
        connection = sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
        try:
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('PRAGMA cache_size=-4096')
            connection.execute('CREATE TEMP TABLE indexed(id INTEGER PRIMARY KEY)')
            keys = np.asarray(index.keys)
            connection.execute('BEGIN')
            for offset in range(0, len(keys), 512):
                checkpoint()
                connection.executemany('INSERT INTO indexed VALUES(?)', ((int(k),) for k in keys[offset:offset+512]))
            row = connection.execute("SELECT value FROM settings WHERE key='vector_generation'").fetchone()
            generation = row[0] if row else '0'
            if metadata and not rebuilt and metadata['generation'] == generation:
                self.last_sync = {'rebuilt': False, 'added': 0, 'removed': 0, 'count': len(index)}
                return
            current_sql = 'SELECT c.id FROM chunks c JOIN embeddings e ON c.hash=e.hash WHERE e.model=?'
            eligible = ' AND c.semantic=1' if any(r[1] == 'semantic' for r in connection.execute('PRAGMA table_info(chunks)')) else ''
            current_sql += eligible
            deleted_sql = f'SELECT id FROM indexed WHERE id NOT IN ({current_sql})'
            removed = connection.execute(f'SELECT count(*) FROM ({deleted_sql})', (MODEL_ID,)).fetchone()[0]
            if not rebuilt and removed > max(1024, len(index)//3):
                index = Index(ndim=512, metric='cos', dtype='f16', connectivity=16)
                rebuilt = True
                connection.execute('DELETE FROM indexed')
            elif removed:
                cursor = connection.execute(deleted_sql, (MODEL_ID,))
                while rows := cursor.fetchmany(256):
                    checkpoint()
                    index.remove(np.asarray([r[0] for r in rows], dtype=np.uint64), threads=1)
            count = connection.execute(f'SELECT count(*) FROM ({current_sql})', (MODEL_ID,)).fetchone()[0]
            self.budget.check(disk=True, reserve_mb=max(8, count*.002))
            after = added = 0
            while True:
                checkpoint()
                rows = connection.execute('SELECT c.id,e.vector FROM chunks c JOIN embeddings e ON c.hash=e.hash '
                    'LEFT JOIN indexed i ON c.id=i.id WHERE i.id IS NULL AND e.model=? AND c.id>?'+eligible+
                    ' ORDER BY c.id LIMIT 256', (MODEL_ID, after)).fetchall()
                if not rows:
                    break
                values = np.vstack([unpack_vector(r[1]) for r in rows])
                index.add(np.asarray([r[0] for r in rows], dtype=np.uint64), values, threads=1)
                after, added = rows[-1][0], added+len(rows)
            checkpoint()
            self._publish(index, generation, added, removed, rebuilt)
        finally:
            connection.close()

    def search(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        import numpy as np
        from usearch.index import Index
        with self.lock:
            meta = self._metadata()
            if meta is None:
                raise RuntimeError('semantic_index_pending')
            if not meta['count']:
                return []
            path = self.store.path.with_name(meta['snapshot'])
            if self.reader is None or self.reader_path != path:
                try:
                    reader = self._restore(path,view=True)
                    if reader is None or len(reader) != meta['count'] or reader.ndim != 512:
                        raise ValueError('invalid ANN snapshot')
                    self.reader, self.reader_path, self.generation = reader, path, meta['generation']
                except Exception as exc:
                    raise RuntimeError('semantic_index_pending: invalid ANN snapshot') from exc
            found = self.reader.search(np.asarray(vector, dtype=np.float32), count=limit, threads=1)
            return [(int(k), float(d)) for k, d in zip(found.keys, found.distances)]


def build_in_worker(config):
    from types import SimpleNamespace
    from .resources import Budget
    path = Path(config['data_dir']).resolve() / 'index.sqlite3'
    os.chdir(path.parent)  # This is the isolated worker, never the daemon.
    cache = Vectors(SimpleNamespace(path=path), Budget(config), local_io=True)
    cache.sync(isolated=False)
    return cache.last_sync
