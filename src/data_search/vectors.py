from __future__ import annotations

import json
import os

from .config import atomic_json
from .model import MODEL_ID
from .resources import ResourceLimit


class Vectors:
    """Recoverable ANN cache; SQLite remains the source of truth.

    Chunk IDs are append-only identities in Store/Engine: changed content is
    deleted and inserted with a new AUTOINCREMENT ID. Embeddings are immutable
    for a (content hash, MODEL_ID). This lets sync diff IDs without loading every
    stored vector or rebuilding every HNSW edge. A snapshot still rewrites the
    ANN file, so its disk reservation includes the replacement copy.
    """

    SCHEMA_VERSION = 2

    def __init__(self, store, budget):
        self.store, self.budget = store, budget
        self.path = store.path.with_name('vectors.usearch')
        self.meta = self.path.with_suffix('.json')
        self.dirty = self.path.with_suffix('.dirty')
        self.reader = None
        self.generation = None
        self.last_sync = None

    def close(self):
        self.reader = None
        self.generation = None

    def _metadata(self):
        if self.dirty.exists() or not self.path.exists():
            return None
        try:
            metadata = json.loads(self.meta.read_text(encoding='utf-8'))
            stat = self.path.stat()
            if (metadata.get('schema_version') != self.SCHEMA_VERSION
                    or metadata.get('model') != MODEL_ID
                    or metadata.get('snapshot_bytes') != stat.st_size
                    or metadata.get('snapshot_mtime_ns') != stat.st_mtime_ns):
                return None
            return metadata
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _flush(path):
        with path.open('r+b') as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _flush_directory(path):
        # POSIX makes the rename/marker durable with directory fsync. Windows
        # does not expose an equivalent directory descriptor through os.open.
        if os.name != 'nt':
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _publish(self, index, generation, added, removed, rebuilt):
        temporary = self.path.with_suffix('.tmp')
        self.budget.check(disk=True, reserve_mb=max(8, max(len(index), index.capacity) * .002))
        # The marker covers the two-file commit. A process interrupted after
        # either replacement leaves it behind and the next sync rebuilds.
        with self.dirty.open('w', encoding='ascii') as stream:
            stream.write('snapshot publication in progress\n')
            stream.flush()
            os.fsync(stream.fileno())
        self._flush_directory(self.path.parent)
        try:
            index.save(str(temporary))
            self._flush(temporary)
            os.replace(temporary, self.path)
            self._flush_directory(self.path.parent)
            stat = self.path.stat()
            atomic_json(self.meta, {
                'schema_version': self.SCHEMA_VERSION,
                'generation': generation, 'model': MODEL_ID, 'count': len(index),
                'snapshot_bytes': stat.st_size, 'snapshot_mtime_ns': stat.st_mtime_ns,
            })
            self._flush(self.meta)
            self._flush_directory(self.path.parent)
            self.dirty.unlink()
            self._flush_directory(self.path.parent)
            self.last_sync = {'rebuilt': rebuilt, 'added': added, 'removed': removed, 'count': len(index)}
        finally:
            if temporary.exists():
                temporary.unlink()

    def sync(self, cancelled=None):
        import numpy as np
        from usearch.index import Index

        def checkpoint():
            if cancelled is not None and cancelled():
                raise ResourceLimit('indexing_paused_or_stopping')

        checkpoint()

        metadata = self._metadata()
        generation = self.store.setting('vector_generation', '0')
        if metadata and metadata.get('generation') == generation:
            # Validate the on-disk container at least once per process rather
            # than trusting metadata when the ANN file is damaged.
            try:
                if self.reader is None or self.generation != generation:
                    self.reader = Index.restore(str(self.path), view=True)
                    if self.reader is None or len(self.reader) != metadata['count'] or self.reader.ndim != 512:
                        raise ValueError('invalid ANN snapshot')
                    self.generation = generation
                self.last_sync = {'rebuilt': False, 'added': 0, 'removed': 0, 'count': metadata['count']}
                return
            except Exception:
                metadata = None

        self.close()
        self.budget.check(disk=True)
        index = None
        if metadata:
            try:
                index = Index.restore(str(self.path), view=False)
                if index is None or len(index) != metadata['count'] or index.ndim != 512:
                    index = None
            except Exception:
                index = None
        rebuilt = index is None
        if index is None:
            index = Index(ndim=512, metric='cos', dtype='f16', connectivity=16)

        with self.store.lock:
            checkpoint()
            generation = self.store.setting('vector_generation', '0')
            current = {int(row[0]) for row in self.store.db.execute(
                'SELECT c.id FROM chunks c JOIN embeddings e ON c.hash=e.hash WHERE e.model=?', (MODEL_ID,))}
            existing = set(map(int, np.asarray(index.keys)))
            removed = existing - current
            # Reclaim an extensively deleted graph; small changes remain
            # incremental. Tombstones must not retain most of an old corpus.
            if not rebuilt and len(removed) > max(1024, len(existing) // 3):
                index = Index(ndim=512, metric='cos', dtype='f16', connectivity=16)
                rebuilt = True
                existing = set()
            elif removed:
                index.remove(np.asarray(sorted(removed), dtype=np.uint64), threads=1)
            pending = sorted(current - existing)
            for offset in range(0, len(pending), 256):
                checkpoint()
                self.budget.check(disk=True, reserve_mb=max(8, len(current) * .002))
                batch = pending[offset:offset + 256]
                placeholders = ','.join('?' for _ in batch)
                rows = self.store.db.execute(
                    'SELECT c.id,e.vector FROM chunks c JOIN embeddings e ON c.hash=e.hash '
                    f'WHERE e.model=? AND c.id IN ({placeholders})', [MODEL_ID, *batch]).fetchall()
                vectors = np.vstack([np.frombuffer(row[1], dtype=np.float32) for row in rows])
                if vectors.shape[1] != 512 or not np.isfinite(vectors).all():
                    raise ValueError('invalid persisted embedding; expected finite 512-dimensional vectors')
                index.add(np.asarray([row[0] for row in rows], dtype=np.uint64), vectors, threads=1)
            # Keep the source generation stable through publication. The caller
            # serializes ANN access; this lock also protects the SQLite snapshot.
            checkpoint()
            self._publish(index, generation, len(pending), len(removed), rebuilt)

    def search(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        import numpy as np
        from usearch.index import Index
        generation = self.store.setting('vector_generation', '0')
        meta = self._metadata()
        if meta is None or meta.get('generation') != generation:
            raise RuntimeError('semantic_index_pending')
        if not meta['count']:
            return []
        if self.reader is None or self.generation != generation:
            self.close()
            try:
                self.reader = Index.restore(str(self.path), view=True)
                if self.reader is None or len(self.reader) != meta['count'] or self.reader.ndim != 512:
                    raise ValueError('invalid ANN snapshot')
            except Exception as exc:
                self.close()
                raise RuntimeError('semantic_index_pending: invalid ANN snapshot') from exc
            self.generation = generation
        found = self.reader.search(np.asarray(vector, dtype=np.float32), count=limit, threads=1)
        return [(int(k), float(d)) for k, d in zip(found.keys, found.distances)]
