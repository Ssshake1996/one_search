from __future__ import annotations

import json
from collections import OrderedDict
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
    """Immutable, bounded ANN segments built outside query/SQLite writer locks.

    Production builds use a monitored disposable process. The previous version
    stays searchable; Engine checks current document IDs and scope on every hit.
    Only a small manifest is atomically replaced after a complete snapshot save.
    """
    SCHEMA_VERSION = 4

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
        self.readers = OrderedDict()
        self.catalog = store.path.with_name("vectors-catalog.sqlite")

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
        if metadata:
            segments = self._segments(metadata)
            if segments:
                return self.store.path.with_name(segments[0]['snapshot'])
            if metadata.get('legacy'):
                return self.store.path.with_name(metadata['legacy']['snapshot'])
        return self.store.path.with_name('vectors.usearch')

    @staticmethod
    def _segments(metadata):
        if metadata.get('schema_version') == 3:
            return [metadata]
        return metadata.get('segments', [])

    def close(self):
        if self.worker:
            self.worker.close()
        with self.lock:
            self.reader = self.reader_path = self.generation = None
            self.readers.clear()

    def cancel(self):
        if self.worker:
            self.worker.cancel()

    def _valid_segment(self, segment):
        name = segment['snapshot']
        if (not isinstance(name, str) or Path(name).name != name or not name.startswith('vectors-')
                or not name.endswith('.usearch') or isinstance(segment.get('count'), bool)
                or not isinstance(segment.get('count'), int) or segment['count'] < 0):
            return False
        stat = self.store.path.with_name(name).stat()
        return segment.get('snapshot_bytes') == stat.st_size and segment.get('snapshot_mtime_ns') == stat.st_mtime_ns

    def _metadata(self):
        if self.dirty.exists():
            return None
        try:
            value = json.loads(self.meta.read_text(encoding='utf-8'))
            if (value.get('schema_version') not in {3, self.SCHEMA_VERSION} or value.get('model') != MODEL_ID
                    or not isinstance(value.get('generation'), str) or isinstance(value.get('count'), bool)
                    or not isinstance(value.get('count'), int) or value['count'] < 0):
                return None
            if value['schema_version'] == 3:
                return value if self._valid_segment(value) else None
            if (not isinstance(value.get('segments'), list) or not isinstance(value.get('complete'), bool)
                    or not self.catalog.is_file()):
                return None
            names = [segment['snapshot'] for segment in value['segments']]
            if len(names) != len(set(names)) or not all(self._valid_segment(segment) for segment in value['segments']):
                return None
            if value.get('legacy') and not self._valid_segment(value['legacy']):
                return None
            with sqlite3.connect(self.catalog.as_uri() + '?mode=ro', uri=True, timeout=5) as catalog:
                for segment in value['segments']:
                    row = catalog.execute('SELECT count FROM segments WHERE name=?', (segment['snapshot'],)).fetchone()
                    if row is None or row[0] != segment['count']:
                        return None
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            return None

    def status(self):
        meta = self._metadata()
        legacy = meta and (meta.get('schema_version') == 3 or bool(meta.get('legacy')))
        return {'building': self.building, 'published_chunks': meta['count'] if meta else 0,
                'pending': meta is None or bool(legacy) or not meta.get('complete', True)
                           or meta.get('pending_compaction', False)
                           or meta['generation'] != self.store.setting('vector_generation', '0'),
                'segments': len(self._segments(meta)) if meta else 0,
                'legacy_migration': bool(legacy), 'last_sync': self.last_sync}

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

    def _catalog_connection(self):
        connection = sqlite3.connect(self.catalog, timeout=5)
        connection.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA cache_size=-2048;
          CREATE TABLE IF NOT EXISTS segments(name TEXT PRIMARY KEY, count INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS members(segment TEXT NOT NULL, id INTEGER NOT NULL, hash TEXT NOT NULL,
            PRIMARY KEY(segment,id)) WITHOUT ROWID;
          CREATE INDEX IF NOT EXISTS members_id ON members(id,hash,segment);
        """)
        return connection

    def _garbage_collect(self, metadata):
        keep = set()
        if metadata:
            keep.update(segment['snapshot'] for segment in self._segments(metadata))
            keep.update(metadata.get('previous_segments', []))
            if metadata.get('previous_snapshot'):
                keep.add(metadata['previous_snapshot'])
            if metadata.get('legacy'):
                keep.add(metadata['legacy']['snapshot'])
        connection = self._catalog_connection()
        try:
            names = [r[0] for r in connection.execute('SELECT name FROM segments')]
            for name in names:
                if name not in keep:
                    with connection:
                        connection.execute('DELETE FROM members WHERE segment=?', (name,))
                        connection.execute('DELETE FROM segments WHERE name=?', (name,))
            for path in self.store.path.parent.glob('vectors-*.usearch'):
                if path.name not in keep:
                    try:
                        path.unlink()
                    except OSError:
                        pass  # Windows may still have an old query reader mapped.
        finally:
            connection.close()

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

    def _read_connection(self, *, catalog=False):
        connection = sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
        connection.execute('PRAGMA temp_store=FILE')
        connection.execute('PRAGMA cache_size=-4096')
        if catalog:
            connection.execute('ATTACH DATABASE ? AS anncat', (str(self.catalog),))
        return connection

    @staticmethod
    def _eligible(connection):
        return ' AND c.semantic=1' if any(r[1] == 'semantic' for r in connection.execute('PRAGMA table_info(chunks)')) else ''

    def _save_segment(self, connection, sql, args, capacity, checkpoint):
        import numpy as np
        from usearch.index import Index
        path = self.store.path.with_name(f'vectors-{uuid.uuid4().hex}.usearch')
        index = Index(ndim=512, metric='cos', dtype='f16', connectivity=16)
        count = 0
        try:
            cursor = connection.execute(sql, args)
            while rows := cursor.fetchmany(min(256, capacity - count)):
                checkpoint()
                values = np.vstack([unpack_vector(row[2]) for row in rows])
                index.add(np.asarray([r[0] for r in rows], dtype=np.uint64), values, threads=1)
                connection.executemany('INSERT INTO anncat.members VALUES(?,?,?)',
                                       ((path.name, row[0], row[1]) for row in rows))
                count += len(rows)
                if count >= capacity:
                    break
            if not count:
                return None
            checkpoint()
            self.budget.check(disk=True, reserve_mb=max(8, count * .002))
            native = self._native_path(path)
            if os.name == 'nt' and not native.isascii():
                path.write_bytes(index.save())  # Bounded to one segment in inline/library use.
            else:
                index.save(native)
            self._flush(path)
            stat = path.stat()
            connection.execute('INSERT INTO anncat.segments VALUES(?,?)', (path.name, count))
            return {'snapshot': path.name, 'count': count, 'snapshot_bytes': stat.st_size,
                    'snapshot_mtime_ns': stat.st_mtime_ns}
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _publish_manifest(self, value, previous):
        keep = [segment['snapshot'] for segment in self._segments(previous)] if previous else []
        if previous and previous.get('legacy'):
            keep.append(previous['legacy']['snapshot'])
        value['previous_segments'] = list(dict.fromkeys(keep))
        atomic_json(self.meta, value)
        self._flush(self.meta)
        self._flush_directory(self.meta.parent)
        self.dirty.unlink(missing_ok=True)

    def _build(self, cancelled=None):
        def checkpoint():
            if cancelled is not None and cancelled():
                raise ResourceLimit('indexing_paused_or_stopping')
            self.budget.check()

        checkpoint()
        metadata = self._metadata()
        self._garbage_collect(metadata)
        options = getattr(self.budget, 'config', {}).get('semantic', {})
        capacity = options.get('segment_size', 20000)
        maximum = options.get('max_segments_per_sync', 4)
        ratio = options.get('compact_deleted_ratio', .3)
        legacy = metadata if metadata and metadata['schema_version'] == 3 else (metadata or {}).get('legacy')
        segments = list(metadata['segments']) if metadata and metadata['schema_version'] == self.SCHEMA_VERSION else []
        created, rebuilt = [], metadata is None or bool(legacy)
        connection = self._read_connection(catalog=True)
        try:
            connection.execute('CREATE TEMP TABLE active(name TEXT PRIMARY KEY)')
            connection.executemany('INSERT INTO active VALUES(?)', ((s['snapshot'],) for s in segments))
            connection.execute('BEGIN')
            row = connection.execute("SELECT value FROM settings WHERE key='vector_generation'").fetchone()
            generation = row[0] if row else '0'
            if (metadata and metadata['schema_version'] == self.SCHEMA_VERSION and metadata['generation'] == generation
                    and metadata['complete'] and not metadata.get('pending_compaction')):
                self.last_sync = {'rebuilt': False, 'added': 0, 'removed': 0, 'count': metadata['count']}
                return
            eligible = self._eligible(connection)
            current = ' FROM chunks c JOIN embeddings e ON c.hash=e.hash AND e.model=?'
            covered = (' FROM anncat.members m JOIN active a ON a.name=m.segment '
                       'JOIN chunks c ON c.id=m.id AND c.hash=m.hash '
                       'JOIN embeddings e ON c.hash=e.hash AND e.model=? WHERE 1=1' + eligible)
            missing = (' WHERE NOT EXISTS(SELECT 1 FROM anncat.members m JOIN active a ON a.name=m.segment '
                       'WHERE m.id=c.id AND m.hash=c.hash)' + eligible)
            # SQL visits IDs without turning the global membership into Python/native arrays.
            live_before = connection.execute('SELECT count(*)' + covered, (MODEL_ID,)).fetchone()[0]
            removed = max(0, metadata['count'] - live_before) if metadata and not legacy else 0
            added = 0
            for _ in range(maximum):
                checkpoint()
                segment = self._save_segment(connection, 'SELECT c.id,c.hash,e.vector' + current + missing + ' ORDER BY c.id LIMIT ?',
                                             (MODEL_ID, capacity), capacity, checkpoint)
                if segment is None:
                    break
                created.append(segment)
                segments.append(segment)
                added += segment['count']
                connection.execute('INSERT INTO active VALUES(?)', (segment['snapshot'],))
            remaining = connection.execute('SELECT 1' + current + missing + ' LIMIT 1', (MODEL_ID,)).fetchone() is not None
            live = dict(connection.execute('SELECT m.segment,count(*)' + covered + ' GROUP BY m.segment', (MODEL_ID,)))
            # Empty segments are cheap to retire. Physical files/catalog rows survive
            # one previous manifest, and open mappings remain valid after unlink.
            empty = [s for s in segments if not live.get(s['snapshot'], 0)]
            for segment in empty:
                connection.execute('DELETE FROM active WHERE name=?', (segment['snapshot'],))
            segments = [s for s in segments if live.get(s['snapshot'], 0)]
            pending_compaction = False
            while len(created) < maximum:
                checkpoint()
                churn = [s for s in segments if 0 < live[s['snapshot']] <= capacity
                         and (s['count'] - live[s['snapshot']]) / s['count'] >= ratio]
                group = churn[:1]
                if not group and len(segments) > 8:
                    # Merge small delta files, never exceeding one segment's workset.
                    total = 0
                    for segment in sorted(segments, key=lambda s: live[s['snapshot']]):
                        if total + live[segment['snapshot']] > capacity:
                            break
                        group.append(segment)
                        total += live[segment['snapshot']]
                    if len(group) < 2:
                        group = []
                if not group:
                    break
                names = [s['snapshot'] for s in group]
                placeholders = ','.join('?' for _ in names)
                sql = ('SELECT c.id,c.hash,e.vector FROM anncat.members m JOIN chunks c ON c.id=m.id AND c.hash=m.hash '
                       'JOIN embeddings e ON c.hash=e.hash AND e.model=? WHERE m.segment IN (' + placeholders + ')' + eligible + ' ORDER BY c.id LIMIT ?')
                replacement = self._save_segment(connection, sql, [MODEL_ID, *names, capacity], capacity, checkpoint)
                if replacement is None:
                    break
                created.append(replacement)
                connection.executemany('DELETE FROM active WHERE name=?', ((name,) for name in names))
                connection.execute('INSERT INTO active VALUES(?)', (replacement['snapshot'],))
                segments = [s for s in segments if s['snapshot'] not in names] + [replacement]
                live[replacement['snapshot']] = replacement['count']
            pending_compaction = any(0 < live[s['snapshot']] <= capacity and
                (s['count'] - live[s['snapshot']]) / s['count'] >= ratio for s in segments)
            small = sorted(live[s['snapshot']] for s in segments)
            pending_compaction = pending_compaction or (len(small) > 8 and sum(small[:2]) <= capacity)
            count = connection.execute('SELECT count(*)' + covered, (MODEL_ID,)).fetchone()[0]
            checkpoint()
            # The catalog may contain orphan rows if publication fails, but queries
            # consult only names in the committed manifest. Never reverse this order.
            connection.commit()
            value = {'schema_version': self.SCHEMA_VERSION, 'model': MODEL_ID, 'generation': generation,
                     'count': count, 'segments': segments, 'complete': not remaining,
                     'pending_compaction': pending_compaction, 'legacy': legacy if remaining else None}
            self._publish_manifest(value, metadata)
            self.last_sync = {'rebuilt': rebuilt, 'added': added, 'removed': removed, 'count': count}
        except Exception:
            connection.rollback()
            published = self._metadata()
            keep = {s['snapshot'] for s in self._segments(published)} if published else set()
            for segment in created:
                if segment['snapshot'] not in keep:
                    self.store.path.with_name(segment['snapshot']).unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def _get_reader(self, segment):
        name = segment['snapshot']
        if name in self.readers:
            reader = self.readers.pop(name)
            self.readers[name] = reader
            return reader
        reader = self._restore(self.store.path.with_name(name), view=True)
        if reader is None or len(reader) != segment['count'] or reader.ndim != 512:
            raise ValueError('invalid ANN segment')
        self.readers[name] = reader
        while len(self.readers) > 8:
            self.readers.popitem(last=False)
        return reader

    def _valid_hits(self, connection, keys, segment, *, catalog, source_id, extension):
        eligible, extra, args = self._eligible(connection), '', []
        if catalog:
            extra += ' AND EXISTS(SELECT 1 FROM anncat.members m WHERE m.segment=? AND m.id=c.id AND m.hash=c.hash)'
            args.append(segment['snapshot'])
        if source_id:
            extra += ' AND d.source_id=?'
            args.append(source_id)
        if extension:
            extra += ' AND d.extension=?'
            args.append(extension.lower())
        result = set()
        for offset in range(0, len(keys), 256):
            batch = keys[offset:offset+256]
            sql = ('SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id '
                   'JOIN embeddings e ON c.hash=e.hash AND e.model=? WHERE c.id IN (' + ','.join('?' for _ in batch) + ')' + eligible + extra)
            result.update(row[0] for row in connection.execute(sql, [MODEL_ID, *batch, *args]))
        return result

    def _filtered_hits(self, connection, vector, segment, limit, source_id, extension):
        import numpy as np
        extra, args = '', [MODEL_ID, segment['snapshot']]
        if source_id:
            extra += ' AND d.source_id=?'
            args.append(source_id)
        if extension:
            extra += ' AND d.extension=?'
            args.append(extension.lower())
        sql = ('SELECT c.id,e.vector FROM anncat.members m JOIN chunks c ON c.id=m.id AND c.hash=m.hash '
               'JOIN documents d ON d.id=c.doc_id JOIN embeddings e ON c.hash=e.hash AND e.model=? '
               'WHERE m.segment=?' + self._eligible(connection) + extra)
        cursor = connection.execute(sql, args)
        best = []
        norm = float(np.linalg.norm(vector))
        while rows := cursor.fetchmany(256):
            values = np.vstack([unpack_vector(row[1]) for row in rows]).astype(np.float32)
            denominators = np.linalg.norm(values, axis=1) * norm
            scores = 1 - np.divide(values @ vector, denominators, out=np.zeros(len(rows), dtype=np.float32), where=denominators != 0)
            best = sorted([*best, *((row[0], float(score)) for row, score in zip(rows, scores))], key=lambda hit: (hit[1], hit[0]))[:limit]
        return best

    def _query_snapshot(self):
        # A reader can be delayed across multiple publications. Pin the catalog
        # WAL snapshot, then verify the manifest did not change in between, so
        # later GC cannot remove membership needed by this in-flight query.
        for _ in range(3):
            metadata = self._metadata()
            if metadata is None:
                raise RuntimeError('semantic_index_pending')
            connection = self._read_connection(catalog=metadata['schema_version'] == self.SCHEMA_VERSION)
            try:
                connection.execute('BEGIN')
                connection.execute('SELECT id FROM chunks LIMIT 1').fetchone()
                if metadata['schema_version'] == self.SCHEMA_VERSION:
                    connection.execute('SELECT name FROM anncat.segments LIMIT 1').fetchone()
                latest = json.loads(self.meta.read_text(encoding='utf-8'))
                if latest == metadata and not self.dirty.exists():
                    return metadata, connection
            except Exception:
                connection.close()
                raise
            connection.close()
        raise RuntimeError('semantic_index_pending: publication changed during query setup')

    def search(self, vector: list[float], limit: int, *, source_id=None, extension=None) -> list[tuple[int, float]]:
        import numpy as np
        limit = max(1, min(int(limit), 8192))
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (512,) or not np.isfinite(query).all():
            raise ValueError('expected finite 512-dimensional query')
        with self.lock:
            meta, connection = self._query_snapshot()
            current_segments = self._segments(meta)
            legacy = meta.get('legacy') if meta['schema_version'] == self.SCHEMA_VERSION else None
            segments = [(s, meta['schema_version'] == self.SCHEMA_VERSION) for s in current_segments]
            if legacy:
                segments.append((legacy, False))
            best = {}
            try:
                for segment, catalog in segments:
                    if not segment['count']:
                        continue
                    if (source_id or extension) and catalog:
                        # SQL narrows each bounded segment first. Exact batch scoring
                        # avoids giant allowed-ID sets and rare-source ANN starvation.
                        hits = self._filtered_hits(connection, query, segment, limit, source_id, extension)
                    else:
                        try:
                            reader = self._get_reader(segment)
                        except Exception as exc:
                            raise RuntimeError('semantic_index_pending: invalid ANN segment') from exc
                        count = min(segment['count'], max(64, limit * 2))
                        cap = segment['count'] if catalog else min(segment['count'], 8192)
                        while True:
                            found = reader.search(query, count=count, threads=1)
                            keys = [int(key) for key in found.keys]
                            valid = self._valid_hits(connection, keys, segment, catalog=catalog, source_id=source_id, extension=extension)
                            hits = [(int(key), float(distance)) for key, distance in zip(found.keys, found.distances) if int(key) in valid]
                            if len(hits) >= limit or count >= cap:
                                break
                            count = min(cap, count * 2)
                        hits = hits[:limit]
                    for key, distance in hits:
                        best[key] = min(best.get(key, float('inf')), distance)
                    best = dict(sorted(best.items(), key=lambda hit: (hit[1], hit[0]))[:limit])
                self.generation = meta['generation']
                return sorted(best.items(), key=lambda hit: (hit[1], hit[0]))
            finally:
                connection.close()


def build_in_worker(config):
    from types import SimpleNamespace
    from .resources import Budget
    path = Path(config['data_dir']).resolve() / 'index.sqlite3'
    os.chdir(path.parent)  # This is the isolated worker, never the daemon.
    cache = Vectors(SimpleNamespace(path=path), Budget(config), local_io=True)
    cache.sync(isolated=False)
    return cache.last_sync
