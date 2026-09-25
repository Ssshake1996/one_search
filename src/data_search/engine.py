from __future__ import annotations

import hashlib
import json
import os
import platform
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import contained
from .model import MODEL_ID, model_ready
from .resources import Budget, ResourceLimit
from .scope import FileScope, link_directory
from .store import Store, pack_vector, query_terms, text_hash
from .vectors import Vectors
from .workers import Worker
from .catalog import FileCatalog
from .product import file_identity
from .search_filters import build_filters


def now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, config: dict):
        self.config = config
        from .maintenance import IndexDirectoryLease
        import weakref
        self.index_lease = IndexDirectoryLease(config)
        self.index_lease.__enter__()
        self._release_index = weakref.finalize(self,self.index_lease.__exit__,None,None,None)
        self.budget = Budget(config)
        self.store = Store(config.get('index_dir', config['data_dir']), self.budget)
        self.instance_id = self.store.setting('instance_id') or str(uuid.uuid4())
        self.store.set_setting('instance_id', self.instance_id)
        self.parser, self.model, self.database = [Worker(self.budget) for _ in range(3)]
        self.vectors = Vectors(self.store, self.budget)
        self.vector_lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop_event, self.scan_event = threading.Event(), threading.Event()
        self.paused = self.store.setting('paused') == 'true'
        from .runtime_policy import RuntimePolicy
        self.policy = RuntimePolicy(config)
        if self.paused and not self.policy.path.exists():
            self.policy.pause()
        self.paused = self.policy.status()['user_paused']
        self.scanning = False
        self.last_error = None
        self.last_scan = self.store.setting('last_scan') or None
        self.source_errors = {}
        self.file_scope = FileScope(config)
        self.file_scan_errors = {'count': 0, 'samples': []}
        self.monitoring = 'periodic' if self.file_scope.mode == 'machine' else 'not_started'
        self.dirty = set()
        self.dirty_lock = threading.Lock()
        self.observer = self.thread = None
        self.vector_thread = None
        self.vector_error = None
        self.journals, self.journal_reports = {}, {}
        self._coverage_cache = None
        self._coverage_time = 0
        self.db_configs = {c['id']: c for c in config['databases']}
        fingerprints = {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        for key, value in self.db_configs.items()}
        previous = json.loads(self.store.setting('database_configs', '{}'))
        scope = self._scope_fingerprint()
        scope_changed = self.store.setting('file_scope') != scope
        # Revocation of a configured root/data source also removes its cached content.
        scan_sql = 'SELECT id,path,source_id,locator FROM documents WHERE id>?'
        if not scope_changed:
            scan_sql += " AND source_id<>'files'"
        after = 0
        while docs := self.store.rows(scan_sql + ' ORDER BY id LIMIT 500', (after,)):
            revoked = []
            for doc in docs:
                if doc['source_id'] == 'files':
                    valid = self.allowed(Path(doc['path']))
                else:
                    valid = (previous.get(doc['source_id']) == fingerprints.get(doc['source_id']) and
                             self._database_allowed(doc))
                if not valid:
                    revoked.append(doc['id'])
            if revoked:
                self.store.remove(revoked)
                self._changed()
            after = docs[-1]['id']
        self.store.set_setting('database_configs', json.dumps(fingerprints))
        self.store.set_setting('file_scope', scope)
        self._apply_indexing_scope()
        self.catalog = FileCatalog(self)

    def _metadata_batch(self, records, seen=None, priority=False):
        if not records:
            return
        self.budget.check(disk=True, reserve_mb=max(1,len(records)*.004))
        changed = False
        keys = ['file:'+row[0] for row in records]
        existing = {row['key']:row for row in self.store.rows('SELECT * FROM documents WHERE key IN ('+
                    ','.join('?' for _ in keys)+')',keys)}
        with self.vector_lock, self.store.lock, self.store.db:
            for record in records:
                path, size, mtime_ns = record[:3]
                identity = record[3] if len(record)>3 else file_identity(Path(path).stat())
                key, p = 'file:'+path, Path(path)
                old = existing.get(key)
                if old and (old.get('file_identity') is None or old['file_identity'] != identity):
                    self.store.db.execute('DELETE FROM documents WHERE id=?',(old['id'],))
                    old = None
                    changed = True
                if old:
                    if not old.get('file_identity'):
                        self.store.db.execute('UPDATE documents SET file_identity=? WHERE id=?',(identity,old['id']))
                    if seen is not None:
                        self.store.db.execute('UPDATE documents SET seen=? WHERE id=?',(seen,old['id']))
                    if old['size']==size and old['mtime_ns']==mtime_ns:
                        continue
                    doc_id = old['id']
                    self.store.clear_chunks(doc_id)
                    self.store.db.execute("UPDATE documents SET size=?,mtime_ns=?,status='pending',reason=NULL,indexed_at=NULL WHERE id=?",
                                          (size,mtime_ns,doc_id))
                    changed = True
                else:
                    row = self.store.db.execute('INSERT INTO documents(key,source_id,path,name,extension,size,mtime_ns,status,seen) '
                        "VALUES(?,'files',?,?,?,?,?,'pending',?)",(key,path,p.name,p.suffix.lower(),size,mtime_ns,seen or 'event'))
                    doc_id = row.lastrowid
                    self.store.db.execute('UPDATE documents SET file_identity=? WHERE id=?',(identity,doc_id))
                if not self._tier_allowed(p,'content'):
                    self.store.db.execute("UPDATE documents SET status='metadata',reason='content_scope_excluded' WHERE id=?",(doc_id,))
                    self.store.db.execute('DELETE FROM file_work WHERE doc_id=?',(doc_id,))
                    continue
                self.store.db.execute('INSERT INTO file_work(doc_id,priority) VALUES(?,?) '
                    'ON CONFLICT(doc_id) DO UPDATE SET available_at=0,attempts=0,priority=max(priority,excluded.priority)',(doc_id,int(priority)))
        self.budget.note_write(len(records)*4096)
        self._coverage_cache = None
        if changed:
            self._changed()

    def _tier_allowed(self, path, tier):
        settings = self.config.get('indexing', {})
        mode = settings.get(tier+'_scope', 'all')
        if mode == 'none':
            return False
        if path is None:  # Database text is explicitly selected in its own config.
            return True
        path = Path(path)
        from .product import sensitive_path
        if any(contained(path,Path(root)) for root in settings.get(tier+'_exclude_paths', [])):
            return False
        if settings.get('sensitive_content_excluded',False) and sensitive_path(path):
            return False
        extensions = settings.get(tier+'_extensions', [])
        if extensions and path.suffix.lower() not in extensions:
            return False
        return mode == 'all' or any(contained(path, Path(root)) for root in settings.get(tier+'_roots', []))

    def _apply_indexing_scope(self):
        settings = json.dumps(self.config.get('indexing', {}), sort_keys=True)
        if self.store.setting('indexing_scope') == settings:
            return
        after = 0
        while rows := self.store.rows('SELECT id,path,source_id,status FROM documents WHERE id>? ORDER BY id LIMIT 200', (after,)):
            with self.store.lock, self.store.db:
                for row in rows:
                    path = row['path'] if row['source_id'] == 'files' else None
                    if path and not self._tier_allowed(path, 'content'):
                        self.store.clear_chunks(row['id'])
                        self.store.db.execute("UPDATE documents SET status='metadata',reason='content_scope_excluded' WHERE id=?", (row['id'],))
                    elif row['status'] == 'metadata':
                        self.store.db.execute("UPDATE documents SET status='pending',reason=NULL WHERE id=?", (row['id'],))
                    self.store.db.execute('UPDATE chunks SET semantic=? WHERE doc_id=?', (int(self._tier_allowed(path, 'semantic')), row['id']))
            after = rows[-1]['id']
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM embeddings WHERE hash NOT IN (SELECT hash FROM chunks WHERE semantic=1)')
        self.store.set_setting('indexing_scope', settings)
        self._changed()

    def allowed(self, path: Path) -> bool:
        return self.file_scope.allowed(path)

    def _scope_fingerprint(self):
        return hashlib.sha256(json.dumps(self.file_scope.report(), sort_keys=True).encode()).hexdigest()

    def _refresh_file_scope(self):
        previous = self._scope_fingerprint()
        self.file_scope = FileScope(self.config)
        current = self._scope_fingerprint()
        if previous != current:
            # Drive disappearance and scope narrowing revoke cached content in
            # bounded batches, even when no successful scan can visit the old root.
            after = 0
            while docs := self.store.rows("SELECT id,path FROM documents WHERE source_id='files' AND id>? ORDER BY id LIMIT 500", (after,)):
                revoked = [row['id'] for row in docs if not self.allowed(Path(row['path']))]
                if revoked:
                    self.store.remove(revoked)
                    self._changed()
                after = docs[-1]['id']
            self.store.set_setting('file_scope', current)

    def _file_error(self, path, reason):
        self.file_scan_errors['count'] += 1
        samples = self.file_scan_errors['samples']
        if len(samples) < 20:
            samples.append({'path': str(path), 'reason': reason})

    def _scope_report(self):
        return {**self.file_scope.report(), 'monitoring': self.monitoring,
                'scan_interval_seconds': self.config['scan_interval_seconds'],
                'scan_errors': {'count': self.file_scan_errors['count'],
                                'samples': list(self.file_scan_errors['samples'])}}

    def _database_allowed(self, doc: dict) -> bool:
        conf = self.db_configs.get(doc['source_id'])
        if not conf:
            return False
        loc = json.loads(doc['locator']) if isinstance(doc['locator'], str) else doc['locator']
        table = loc.get('table')
        index = next((i for i in conf.get('index', []) if i['table'] == table), None)
        if not index or table not in conf.get('allowed_tables', []):
            return False
        if loc.get('id_column') != index['id_column'] or not set(loc.get('columns', [])).issubset(index['text_columns']):
            return False
        columns = conf.get('allowed_columns', {}).get(table)
        return columns is None or set(index['text_columns'] + [index['id_column']]).issubset(columns)

    def _changed(self):
        self._coverage_cache = None
        with self.vector_lock:
            value = int(self.store.setting('vector_generation', '0')) + 1
            self.store.set_setting('vector_generation', str(value))

    def _encode(self, texts: list[str], query=False):
        if not self.config['semantic']['enabled']:
            raise RuntimeError('semantic_disabled')
        if self.config.get('indexing', {}).get('semantic_scope') == 'none':
            raise RuntimeError('semantic_disabled_by_scope')
        if not model_ready(self.config['semantic']['model_dir']):
            raise RuntimeError('model_missing: run model-download')
        return self.model.request({'method': 'encode', 'texts': texts, 'query': query,
                                   'model_dir': self.config['semantic']['model_dir'],
                                   'threads': self.config['semantic']['threads']}, timeout=90)

    def _write_chunks(self, doc_id: int, chunks: list[dict]):
        from .chunking import split_chunks
        document = self.store.rows('SELECT path,source_id FROM documents WHERE id=?', (doc_id,))[0]
        semantic = int(self._tier_allowed(document['path'] if document['source_id'] == 'files' else None, 'semantic'))
        with self.vector_lock, self.store.lock, self.store.db:
            self.store.clear_chunks(doc_id)
            for chunk in split_chunks(chunks, max_chars=self.config['extraction']['max_chars']):
                self.store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator,semantic) VALUES(?,?,?,?,?)',
                    (doc_id, chunk['text'], text_hash(chunk['text']), json.dumps(chunk['locator'], ensure_ascii=False), semantic))
            self.store.db.execute('UPDATE documents SET chunking_version=3 WHERE id=?',(doc_id,))
            self._changed()

    def _file(self, path: Path, seen: str, metadata_only=False, *, explicit=False):
        if self.stop_event.is_set() or (self.paused and not explicit):
            raise ResourceLimit('paused')
        if not self.allowed(path) or link_directory(path):
            return
        key = str(path.resolve())
        existing = self.store.rows('SELECT * FROM documents WHERE key=?', ('file:' + key,))
        if not path.exists():
            if existing:
                self.store.remove([existing[0]['id']])
                self._changed()
            return
        if not path.is_file():
            return
        stat = path.stat()
        identity = file_identity(stat)
        if existing and (existing[0].get('file_identity') is None or existing[0]['file_identity'] != identity):
            self.store.remove([existing[0]['id']])
            self._changed()
            existing = []
        self.budget.check(disk=True, reserve_mb=8)
        preserve_old = False
        with self.vector_lock, self.store.lock, self.store.db:
            if existing:
                doc = existing[0]
                self.store.db.execute('UPDATE documents SET seen=? WHERE id=?', (seen, doc['id']))
                if doc['mtime_ns'] == stat.st_mtime_ns and doc['size'] == stat.st_size and doc['status'] not in {'pending','budget','error'}:
                    return
                doc_id = doc['id']
                preserve_old = (doc['chunking_version']<3 and doc['mtime_ns']==stat.st_mtime_ns and doc['size']==stat.st_size)
                if not preserve_old:
                    self.store.clear_chunks(doc_id)
                    self._changed()
                self.store.db.execute('UPDATE documents SET size=?,mtime_ns=?,status=?,reason=NULL,'
                                      'indexed_at=CASE WHEN ? THEN indexed_at ELSE NULL END WHERE id=?',
                                      (stat.st_size, stat.st_mtime_ns, 'pending', preserve_old, doc_id))
            else:
                cursor = self.store.db.execute('INSERT INTO documents(key,source_id,path,name,extension,size,mtime_ns,status,seen) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('file:'+key, 'files', key, path.name, path.suffix.lower(), stat.st_size, stat.st_mtime_ns, 'pending', seen))
                doc_id = cursor.lastrowid
            self.store.db.execute('UPDATE documents SET file_identity=? WHERE id=?',(identity,doc_id))
        if metadata_only:
            return
        if not self._tier_allowed(path, 'content'):
            with self.store.lock, self.store.db:
                self.store.clear_chunks(doc_id)
                self.store.db.execute("UPDATE documents SET status='metadata',reason='content_scope_excluded' WHERE id=?", (doc_id,))
            return
        status, reason, chunks = 'budget', 'file_size_limit', []
        if stat.st_size <= self.config['extraction']['max_file_mb'] * 1048576:
            try:
                result = self.parser.request({'method': 'extract', 'path': key,
                    'max_chars': self.config['extraction']['max_chars']}, self.config['extraction']['timeout_seconds'])
                latest = path.stat()
                if (latest.st_mtime_ns, latest.st_size) != (stat.st_mtime_ns, stat.st_size):
                    result = {'status': 'pending', 'reason': 'changed_during_read', 'chunks': []}
                status, reason, chunks = result['status'], result.get('reason'), result['chunks']
            except ResourceLimit as exc:
                status, reason = 'budget', str(exc)
            except Exception as exc:
                status, reason = 'error', type(exc).__name__
        if chunks:
            self.budget.check(disk=True, reserve_mb=sum(len(c['text'].encode('utf-8')) for c in chunks)*8/1048576 + 4)
            self._write_chunks(doc_id, chunks)
            self.budget.note_write(sum(len(c['text'].encode('utf-8')) for c in chunks)*8 + 4096)
        elif preserve_old and status not in {'pending','budget','error'}:
            with self.vector_lock,self.store.lock,self.store.db:
                self.store.clear_chunks(doc_id)
                self.store.db.execute('UPDATE documents SET chunking_version=3 WHERE id=?',(doc_id,))
                self._changed()
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE documents SET status=?,reason=?,indexed_at=? WHERE id=?',
                                  (status, reason, now(), doc_id))
        time.sleep(self.config['resource'].get('batch_sleep_ms', 50)/1000)

    def _poll_journals(self):
        if self.file_scope.mode != 'machine' or platform.system() != 'Windows':
            return
        from .change_journal import VolumeJournal
        current = {str(root) for root in self.file_scope.roots}
        for root in list(self.journals):
            if root not in current:
                self.journals.pop(root).close()
                self.journal_reports.pop(root,None)
        for root in current:
            if root not in self.journals:
                self.journals[root] = VolumeJournal(root)
            journal = self.journals[root]
            key = 'journal:'+root
            cursor = json.loads(self.store.setting(key,'null'))
            result = journal.poll(cursor,max_events=self.config['scheduler']['journal_events_per_tick'],
                                  allowed=self.allowed)
            reconcile = result.get('reconciliation_required',False)
            previous = self.journal_reports.get(root)
            if not result.get('available') and previous and not previous.get('available'):
                # A persistent permission/filesystem limitation uses the periodic
                # fallback. Re-arming it every tick would loop full scans forever.
                reconcile = False
            events, next_cursor = result.get('events',[]), result.get('next_cursor')
            if events or reconcile or (next_cursor is not None and next_cursor != cursor):
                self.catalog.enqueue_events(events,state_key=key,state=next_cursor,reconcile=reconcile)
            self.journal_reports[root] = {k:v for k,v in result.items() if k not in {'events','next_cursor'}}
        available = sum(bool(row.get('available')) for row in self.journal_reports.values())
        self.monitoring = ('journal_and_reconciliation' if available==len(current) and current else
                           'journal_with_periodic_fallback' if available else 'periodic')

    def _files(self, full=True):
        if full and not self.catalog.active:
            self._refresh_file_scope()
            self.catalog.restrict()
            self.file_scan_errors = {'count':0,'samples':[]}
            for item in self.file_scope.unavailable_roots:
                self.source_errors[item['path']] = 'root_unavailable'
            if self.file_scope.discovery_error:
                self.source_errors['file_scope'] = self.file_scope.discovery_error
            else:
                self.source_errors.pop('file_scope',None)
        self._poll_journals()
        self.catalog.migrate_chunks()
        with self.dirty_lock:
            dirty, self.dirty = self.dirty, set()
        if dirty:
            self.catalog.enqueue_events([{'path':path,'is_directory':Path(path).is_dir()} for path in dirty])
        if full or self.store.setting('file_reconcile_requested')=='true':
            self.catalog.begin()
        self.catalog.discover()
        self.catalog.process_events()
        self.catalog.parse()

    def _database_progress(self):
        result = {}
        for source_id, conf in self.db_configs.items():
            if not conf.get('index'):
                continue
            state = json.loads(self.store.setting('database_sync:' + source_id, '{}'))
            result[source_id] = {'tables': state.get('tables', {}),
                                 'last_error': state.get('last_error')}
        return result

    def _database_apply_document(self, source_id, item, generation):
        key = 'db:' + source_id + ':' + item['key']
        old = self.store.rows('SELECT id,version,chunking_version FROM documents WHERE key=?', (key,))
        with self.store.lock, self.store.db:
            if old:
                doc_id = old[0]['id']
                self.store.db.execute('UPDATE documents SET seen=? WHERE id=?', (generation, doc_id))
                if old[0]['version'] == item['version'] and old[0]['chunking_version']==3:
                    return
            else:
                cursor = self.store.db.execute('INSERT INTO documents(key,source_id,path,name,status,indexed_at,seen,locator) VALUES(?,?,?,?,?,?,?,?)',
                    (key, source_id, item['table'], item['table'], 'pending', now(), generation, json.dumps(item['locator'])))
                doc_id = cursor.lastrowid
        # Publish the version only AFTER chunks are durable. A crash between these
        # writes must replay the page instead of treating a partial row as current.
        self._write_chunks(doc_id, [{'text': item['text'], 'locator': item['locator']}])
        partial = item['locator'].get('truncated', False)
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE documents SET version=?,locator=?,indexed_at=?,status=?,reason=? WHERE id=?',
                (item['version'], json.dumps(item['locator']), now(), 'partial' if partial else 'ready',
                 'record_text_limit' if partial else None, doc_id))

    def _database_scan(self, deadline=None, force=False):
        sources = list(self.db_configs.items())
        offset = int(self.store.setting('database_next_source','0')) % max(1,len(sources))
        ordered = sources[offset:] + sources[:offset]
        for position, (source_id, conf) in enumerate(ordered):
            if deadline is not None and time.monotonic() >= deadline:
                break
            entries = conf.get('index', [])
            if not entries:
                continue
            self.store.set_setting('database_next_source',str((offset+position+1)%len(sources)))
            setting = 'database_sync:' + source_id
            fingerprint = hashlib.sha256(json.dumps(conf, sort_keys=True).encode()).hexdigest()
            state = json.loads(self.store.setting(setting, '{}'))
            if state.get('fingerprint') != fingerprint:
                state = {'fingerprint': fingerprint, 'tables': {}, 'next_table': 0}
            if state.get('chunking_version')!=3:
                # Preserve cached rows while a bounded full pass upgrades old
                # chunks, including unchanged rows behind an incremental watermark.
                state.update(chunking_version=3,next_poll_at=0)
                for table in state['tables'].values():
                    table.update(phase='idle',watermark=None,last_full_at=0)
            if deadline is not None and not force and time.time() < state.get('next_poll_at',0):
                continue
            sync = conf.get('sync', {})
            page_size = sync.get('page_size', 250)
            max_pages = sync.get('max_pages_per_tick', 4)
            # Legacy total-row limits now bound a tick, never the entire table.
            row_budget = conf.get('index_max_rows', page_size * max_pages)
            page_size = min(page_size, row_budget)
            interval = sync.get('reconcile_interval_seconds', 3600)
            completed_this_tick = set()
            table_state = None
            try:
                if len({entry['table'] for entry in entries}) != len(entries):
                    raise ValueError('Only one index specification per table is supported')
                for _ in range(max_pages):
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    if self.paused or self.stop_event.is_set():
                        raise ResourceLimit('paused')
                    # Round-robin tables across ticks so one large table cannot
                    # indefinitely block the other allowlisted tables.
                    available = [i for i in range(len(entries)) if entries[i]['table'] not in completed_this_tick]
                    if not available or row_budget <= 0:
                        break
                    start = state['next_table'] % len(entries)
                    selected = next((i for i in available if i >= start), available[0])
                    entry = entries[selected]
                    table = entry['table']
                    state['next_table'] = (selected + 1) % len(entries)
                    table_state = state['tables'].setdefault(table, {})
                    if table_state.get('phase', 'idle') == 'idle':
                        full = (not entry.get('updated_column') or table_state.get('watermark') is None
                                or time.time() - table_state.get('last_full_at', 0) >= interval)
                        table_state.update(phase='scanning', mode='full' if full else 'incremental',
                            cursor=None, boundary=None, scanned_rows=0, pages=0, deleted_rows=0,
                            cleanup_after=0, started_at=time.time(), last_error=None)
                        if full:
                            table_state['generation'] = str(uuid.uuid4())
                    limit = min(page_size, row_budget)
                    self.budget.check(disk=True, reserve_mb=4)
                    if table_state['phase'] == 'reconciling':
                        # The full key range is durable before any removals. Cleanup
                        # itself is resumable and has the same bounded page budget.
                        removed = [row['id'] for row in self.store.rows(
                            'SELECT id FROM documents WHERE source_id=? AND path=? AND id>? AND (seen IS NULL OR seen<>?) ORDER BY id LIMIT ?',
                            (source_id, table, table_state['cleanup_after'], table_state['generation'], limit))]
                        if removed:
                            with self.vector_lock:
                                self.store.remove(removed)
                                self._changed()
                            table_state['cleanup_after'] = removed[-1]
                        table_state['deleted_rows'] += len(removed)
                        row_budget -= len(removed)
                        if len(removed) < limit:
                            table_state.update(phase='idle', last_full_at=time.time(), completed_at=time.time(),
                                watermark=table_state['boundary']['watermark'])
                            completed_this_tick.add(table)
                    else:
                        page = self.database.request({'method': 'db_index_page', 'config': conf, 'entry': entry,
                            'mode': table_state['mode'], 'after': table_state['cursor'],
                            'boundary': table_state['boundary'], 'watermark': table_state.get('watermark'),
                            'page_size': limit}, timeout=65)
                        self.budget.check(disk=True, reserve_mb=4 + sum(len(d['text'].encode('utf-8')) for d in page['documents']) * 8 / 1048576)
                        for item in page['documents']:
                            if self.paused or self.stop_event.is_set():
                                raise ResourceLimit('paused')
                            self._database_apply_document(source_id, item, table_state['generation'])
                            self.budget.note_write(len(item['text'].encode('utf-8'))*8+4096)
                        # Persist only after the entire page has been applied. A
                        # failed page can safely be repeated without missing rows.
                        table_state['cursor'] = page['next_cursor']
                        table_state['boundary'] = page['boundary']
                        table_state['scanned_rows'] += len(page['documents'])
                        table_state['pages'] += 1
                        row_budget -= len(page['documents'])
                        if page['complete']:
                            if table_state['mode'] == 'full':
                                table_state['phase'] = 'reconciling'
                            else:
                                table_state.update(phase='idle', completed_at=time.time(), watermark=page['boundary']['watermark'])
                                completed_this_tick.add(table)
                    table_state['last_error'] = None
                    state['last_error'] = None
                    state['retry_count'] = 0
                    if len(state['tables'])==len(entries) and all(row.get('phase')=='idle' for row in state['tables'].values()):
                        state['next_poll_at'] = time.time()+self.config['scan_interval_seconds']
                    else:
                        state['next_poll_at'] = 0
                    self.store.set_setting(setting, json.dumps(state))
                    self.source_errors.pop(source_id, None)
                    time.sleep(self.config['resource'].get('batch_sleep_ms', 50) / 1000)
                    if state['next_poll_at']:
                        break
            except Exception as exc:
                error = str(exc)[:200] if isinstance(exc, ResourceLimit) or str(exc).startswith('DatabaseError:') else type(exc).__name__
                state['last_error'] = error
                state['retry_count'] = min(state.get('retry_count',0)+1,6)
                state['next_poll_at'] = time.time()+min(3600,self.config['scan_interval_seconds']*2**(state['retry_count']-1))
                if table_state is not None:
                    table_state['last_error'] = error
                self.store.set_setting(setting, json.dumps(state))
                self.source_errors[source_id] = error
            finally:
                self.database.idle_close(5)

    def _embed_pending(self):
        if not self.config['semantic']['enabled'] or not model_ready(self.config['semantic']['model_dir']):
            return
        batch = self.config['semantic']['batch_size']
        deadline = time.monotonic() + self.config['scheduler']['phase_seconds']
        batches = 0
        while not self.paused and not self.stop_event.is_set() and time.monotonic()<deadline:
            if batches >= self.config['scheduler']['embedding_batches_per_tick']:
                break
            pending = self.store.rows('SELECT q.hash,(SELECT c.text FROM chunks c WHERE c.hash=q.hash '
                'AND c.semantic=1 LIMIT 1) text FROM embedding_queue q ORDER BY q.hash LIMIT ?',(batch,))
            if not pending:
                break
            self.budget.check(disk=True, reserve_mb=8)
            encoded = self._encode([r['text'] for r in pending])
            with self.store.lock, self.store.db:
                for row, vector in zip(pending, encoded):
                    blob = pack_vector(vector)
                    self.store.db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (row['hash'],MODEL_ID,blob))
            self._changed()
            self.budget.note_write(len(pending)*4096)
            batches += 1
            time.sleep(self.config['resource'].get('batch_sleep_ms',50)/1000)
        if not self.paused and not self.stop_event.is_set():
            self._publish_vectors()

    def _publish_vectors(self):
        if self.vector_thread is not None and self.vector_thread.is_alive():
            return
        if not self.vectors.status()['pending']:
            return
        def publish():
            try:
                self.vectors.sync(cancelled=lambda: self.paused or self.stop_event.is_set())
                self.vector_error = None
            except Exception as error:
                self.vector_error = str(error)[:200] if isinstance(error,ResourceLimit) else type(error).__name__
        self.vector_thread = threading.Thread(target=publish,daemon=True)
        self.vector_thread.start()

    def scan_once(self, full=True) -> dict:
        if not self.scan_lock.acquire(blocking=False):
            return {'accepted': False, 'reason': 'already_scanning'}
        self.scanning, self.last_error = True, None
        try:
            if self.paused:
                return {'accepted': False, 'reason': 'paused'}
            errors = []
            # One bad/slow source must not permanently starve all later phases.
            for phase in (lambda:self._files(full),
                          lambda:self._database_scan(time.monotonic()+self.config['scheduler']['phase_seconds'],full),
                          self._embed_pending):
                if self.paused or self.stop_event.is_set():
                    break
                try:
                    phase()
                except Exception as exc:
                    errors.append(str(exc)[:200] if isinstance(exc,ResourceLimit) else type(exc).__name__)
            self.last_error = '; '.join(errors) or None
            with self.store.lock, self.store.db:
                orphaned = self.store.db.execute('SELECT hash FROM orphan_embedding_queue ORDER BY hash LIMIT 256').fetchall()
                for row in orphaned:
                    self.store.db.execute('DELETE FROM embeddings WHERE hash=? AND NOT EXISTS '
                        '(SELECT 1 FROM chunks WHERE hash=? AND semantic=1)',(row['hash'],row['hash']))
                    self.store.db.execute('DELETE FROM orphan_embedding_queue WHERE hash=?',(row['hash'],))
            self.last_scan = now()
            self.store.set_setting('last_scan', self.last_scan)
        except Exception as exc:
            self.last_error = str(exc)[:200] if isinstance(exc,ResourceLimit) else type(exc).__name__
        finally:
            self.parser.idle_close(5)
            self._coverage_cache = None
            self.scanning = False
            self.scan_lock.release()
        return self.status()

    def _has_work(self):
        if not json.loads(self.store.setting('file_chunking_migration'))['done']:
            return True
        if self.catalog.active or self.store.setting('file_reconcile_requested')=='true':
            return True
        if self.store.rows('SELECT 1 FROM file_events LIMIT 1') or self.store.rows(
                'SELECT 1 FROM file_work WHERE available_at<=? LIMIT 1',(time.time(),)):
            return True
        for source_id, conf in self.db_configs.items():
            if not conf.get('index'):
                continue
            state = json.loads(self.store.setting('database_sync:'+source_id,'{}'))
            if not state.get('last_error') and time.time() >= state.get('next_poll_at',0) and any(
                    row.get('phase') in {'scanning','reconciling'} for row in state.get('tables',{}).values()):
                return True
        if self.config['semantic']['enabled'] and model_ready(self.config['semantic']['model_dir']):
            if self.vectors.status()['pending'] or self.store.rows('SELECT 1 FROM embedding_queue LIMIT 1'):
                return True
        return bool(self.store.rows('SELECT 1 FROM orphan_embedding_queue LIMIT 1'))

    def start_background(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        engine = self
        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type not in {'created','modified','deleted','moved'}:
                    return
                if event.is_directory:
                    # A parent's modified event accompanies every file write; treating it
                    # as a rescan request makes our own index trigger endless full scans.
                    if event.event_type != 'modified' and (engine.allowed(Path(event.src_path)) or
                       (getattr(event, 'dest_path', None) and engine.allowed(Path(event.dest_path)))):
                        engine.scan_event.set()
                    return
                names = [p for p in (event.src_path, getattr(event, 'dest_path', None)) if p and engine.allowed(Path(p))]
                if not names:
                    return
                with engine.dirty_lock:
                    if len(engine.dirty) > 10000:
                        engine.scan_event.set()
                        engine.dirty.clear()
                    else:
                        engine.dirty.update(names)
                try:
                    engine.catalog.enqueue_events([{'path':name} for name in names])
                except ResourceLimit:
                    engine.scan_event.set()
        # Whole disks and Linux recursive inotify can require one watch per
        # directory and unbounded setup memory. Use periodic scans for these
        # scopes; selected Windows roots use native recursive directory handles.
        self.monitoring = 'periodic'
        if self.file_scope.mode == 'directories' and platform.system() == 'Windows' and 0 < len(self.file_scope.roots) <= 32:
            observer = Observer()
            try:
                for root in self.file_scope.roots:
                    observer.schedule(Handler(), str(root), recursive=True)
                observer.start()
                self.observer = observer
                self.monitoring = 'watcher_and_periodic_reconciliation'
            except (OSError, RuntimeError):
                observer.stop()
                if observer.is_alive():
                    observer.join(timeout=5)
                self.source_errors['watcher'] = 'unavailable; periodic scanning active'
        self.scan_event.set()
        def loop():
            last_tick = last_full = 0
            while not self.stop_event.wait(.2):
                moment = time.monotonic()
                self.paused = self.policy.status()['user_paused']
                full = self.scan_event.is_set() or moment-last_full >= self.config['reconcile_interval_seconds']
                if self.observer is None and self.monitoring!='journal_and_reconciliation' and moment-last_full >= self.config['scan_interval_seconds']:
                    full = True
                active = self._has_work() or any(row.get('has_more') for row in self.journal_reports.values())
                interval = self.config['scheduler']['tick_seconds'] if active else self.config['scan_interval_seconds']
                if not self.paused and (full or moment-last_tick >= interval):
                    if self.policy.decision()['background_allowed']:
                        self.scan_event.clear()
                        self.scan_once(full=full)
                        last_tick = time.monotonic()
                        if full:
                            last_full = last_tick
                self.model.idle_close(self.config['semantic']['idle_seconds'])
                self.database.idle_close(5)
                self.parser.idle_close(5)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def _result(self, row: dict, match: str, score: float) -> dict | None:
        if row['source_id'] == 'files':
            if row.get('file_identity') is None:
                return None  # Legacy cached text has no provable association with the current file.
            p = Path(row['path'])
            if not self.allowed(p) or not p.is_file() or p.is_symlink():
                return None
            try:
                with p.open('rb'):
                    stat = p.stat()
                if row.get('file_identity') and row['file_identity'] != file_identity(stat):
                    return None
                stale = row['file_identity']=='unavailable' or stat.st_mtime_ns != row['mtime_ns'] or stat.st_size != row['size']
            except OSError:
                return None
        else:
            if not self._database_allowed(row):
                return None
            stale = True  # Cached DB evidence is explicitly a snapshot until live fetch.
        return {'id': ('c:' + str(row['chunk_id'])) if row.get('chunk_id') else ('d:' + str(row['id'])),
                'document_id': 'd:' + str(row['id']),
                'node_id': self.config['node_id'], 'source_id': row['source_id'], 'path': row['path'],
                'name': row['name'], 'snippet': row.get('text',''),
                'locator': json.loads(row.get('chunk_locator') or row['locator']),
                'indexed_at': row['indexed_at'], 'stale': stale, 'status': row['status'],
                'size':row['size'], 'modified_ns':row['mtime_ns'],
                'identity_verification':'unavailable' if row.get('file_identity')=='unavailable' else 'file_id' if row['source_id']=='files' else 'database_key',
                'revision':row.get('version') or str(row['mtime_ns'])+':'+str(row['size']),
                'reason': row['reason'], 'match': match, 'score': score}

    def _candidate_results(self, candidates, doc_filter, args, limit):
        ordered = sorted(candidates, key=lambda key: -candidates[key]['score'])
        results, seen = [], set()
        for offset in range(0, len(ordered), 256):
            ids = ordered[offset:offset+256]
            placeholders = ','.join('?' for _ in ids)
            rows = self.store.rows('SELECT d.*,c.id chunk_id,c.text,c.locator chunk_locator,c.semantic '
                'FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ('+placeholders+')'+doc_filter, ids+args)
            by_id = {row['chunk_id']: row for row in rows}
            for key in ids:
                row, item = by_id.get(key), candidates[key]
                if row is None or row['id'] in seen:
                    continue
                if not row['semantic'] and 'semantic' in item['matches']:
                    if item['matches'] == ['semantic']:
                        continue
                    item = {**item, 'matches':['keyword'], 'score':item['score']-item.get('semantic_score',0)}
                result = self._result(row, '+'.join(item['matches']), item['score'])
                if result:
                    results.append(result)
                    seen.add(row['id'])
                if len(results) >= limit:
                    return results
        return results

    def search(self, query: str, mode='hybrid', limit=20, source_id=None, extension=None,
               extensions=None,directory=None,modified_after=None,modified_before=None,min_size=None,max_size=None,
               category=None,sort='relevance',fold_duplicates=False,**kwargs):
        start = time.perf_counter()
        self.policy.foreground()
        if mode not in {'files','keyword','semantic','hybrid'} or not isinstance(query,str) or not query.strip() or len(query)>1000:
            raise ValueError('invalid mode or query (1..1000 characters)')
        limit = max(1,min(int(limit),100))
        warnings, results = [], []
        doc_filter,args,applied = build_filters(source_id=source_id,extension=extension,extensions=extensions,
            directory=directory,modified_after=modified_after,modified_before=modified_before,min_size=min_size,max_size=max_size,
            category=category,sort=sort)
        candidate_limit, maximum = max(128,limit*5), 8192
        if mode == 'files':
            doc_filter += " AND d.source_id='files'"
            literal = query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            # LIKE ESCAPE disables FTS trigram acceleration: only use it when needed.
            escaped = '%' in query or '_' in query or '\\' in query
            short_chinese = len(query) <= 2 and all('\u3400' <= char <= '\u9fff' for char in query)
            # CROSS JOIN keeps the short-token posting list as the outer loop;
            # the source index must not turn this into a scan of every file.
            source = ('paths_short_fts p CROSS JOIN documents d ON d.id=p.rowid' if short_chinese else
                      'paths_fts p JOIN documents d ON d.id=p.rowid' if len(query)>=3 and not escaped else 'documents d')
            expression = ('p.path' if source.startswith('paths_fts ') else 'd.path') + ' LIKE ?' + (" ESCAPE '\\'" if escaped else '')
            path_args = ['%'+(literal if escaped else query)+'%']
            if short_chinese:
                expression = 'paths_short_fts MATCH ? AND ' + expression
                path_args.insert(0, '"'+query+'"')
            while True:
                ordering = {'relevance':'CASE WHEN d.name=? COLLATE NOCASE THEN 0 ELSE 1 END,d.id',
                            'modified_desc':'d.mtime_ns DESC,d.id', 'modified_asc':'d.mtime_ns,d.id','name':'d.name COLLATE NOCASE,d.id'}[sort]
                rows = self.store.rows('SELECT d.* FROM '+source+' WHERE '+expression+doc_filter+' ORDER BY '+ordering+' LIMIT ?',
                                       path_args+args+([query] if sort=='relevance' else [])+[candidate_limit])
                results = [hit for row in rows if (hit := self._result(row, 'filename', 1.0))][:limit]
                if len(results)>=limit or len(rows)<candidate_limit or candidate_limit>=maximum:
                    break
                candidate_limit = min(maximum,candidate_limit*2)
        else:
            expression = query_terms(query) if mode in {'keyword','hybrid'} else ''
            vector = None
            if mode in {'semantic','hybrid'}:
                try:
                    vector = self._encode([query],query=True)[0]
                except Exception as exc:
                    warnings.append(str(exc)[:200])
            while True:
                candidates, saturated = {}, False
                if expression:
                    keyword_order = {'relevance':'rank','modified_desc':'d.mtime_ns DESC,rank','modified_asc':'d.mtime_ns,rank','name':'d.name COLLATE NOCASE,rank'}[sort]
                    rows = self.store.rows('SELECT c.id chunk_id,bm25(chunks_fts) rank FROM chunks_fts '
                        'JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.id=c.doc_id '
                        'WHERE chunks_fts MATCH ?'+doc_filter+' ORDER BY '+keyword_order+' LIMIT ?', [expression]+args+[candidate_limit])
                    saturated = len(rows) == candidate_limit
                    for rank,row in enumerate(rows):
                        candidates[row['chunk_id']] = {'score':1/(61+rank), 'matches':['keyword']}
                if vector is not None:
                    try:
                        filters = {'source_id':source_id,'extension':extension}
                        if any(v is not None for v in (extensions,directory,modified_after,modified_before,min_size,max_size,category)):
                            filters.update(filter_sql=doc_filter,filter_args=args)
                        hits = self.vectors.search(vector,candidate_limit,**filters)
                        if doc_filter:
                            warnings.append('filtered_semantic_exact: scores current matching vectors in bounded batches; cost grows with the filtered vector count')
                        saturated = saturated or len(hits) == candidate_limit
                        eligible = set()
                        for offset in range(0,len(hits),256):
                            ids = [key for key,_ in hits[offset:offset+256]]
                            if ids:
                                placeholders = ','.join('?' for _ in ids)
                                eligible.update(r['id'] for r in self.store.rows('SELECT id FROM chunks WHERE semantic=1 AND id IN ('+placeholders+')', ids))
                        for rank,(chunk_id,distance) in enumerate(hits):
                            if chunk_id not in eligible:
                                continue
                            item = candidates.setdefault(chunk_id, {'score':0,'matches':[]})
                            item['semantic_score'] = 1/(61+rank)
                            item['score'] += item['semantic_score']
                            item['matches'].append('semantic')
                    except Exception as exc:
                        warnings.append(str(exc)[:200])
                        vector = None
                results = self._candidate_results(candidates, doc_filter, args, limit)
                if len(results)>=limit or not saturated:
                    break
                if candidate_limit>=maximum:
                    warnings.append('candidate_limit_reached: narrow the query or search a specific source with keyword mode')
                    break
                candidate_limit = min(maximum,candidate_limit*2)
            if vector is not None and self.vectors.status()['pending']:
                warnings.append('semantic_index_updating: results use the previous published snapshot')
            if sort!='relevance':
                if mode!='keyword':
                    warnings.append('sort_applies_to_retrieved_candidates: use files or keyword mode for exhaustive matching ordering')
                results.sort(key=(lambda r:(r['name'].casefold(),r['id'])) if sort=='name' else (lambda r:(r.get('modified_ns') or 0,r['id'])), reverse=sort=='modified_desc')
        if mode=='hybrid' and sort=='relevance':
            exact_rows = self.store.rows("SELECT d.* FROM documents d WHERE d.source_id='files' AND d.name=? COLLATE NOCASE"+doc_filter+' ORDER BY d.id LIMIT ?', [query]+args+[limit])
            exact = [hit for row in exact_rows if (hit:=self._result(row,'exact_filename',1.0))]
            ids = {r['document_id'] for r in exact}
            results = (exact+[r for r in results if r['document_id'] not in ids])[:limit]
        from .product import cite, group_evidence
        for result in results:
            result['citation'] = cite(result)
        if fold_duplicates:
            results = group_evidence(self,results,True)
        return {'results':results,'elapsed_ms':round((time.perf_counter()-start)*1000,2),
                'warnings':list(dict.fromkeys(warnings)),'coverage':self.coverage(),
                'applied_filters':applied,'date_semantics':'UTC when timezone absent; after inclusive, before exclusive',
                'candidate_limit':candidate_limit, 'evidence_only':True,
                'note':'Similarity is not proof that a question is answerable; use the quoted evidence.'}

    def fetch(self,id:str,offset=0,limit=10,**kwargs):
        self.policy.foreground()
        prefix, raw = id.split(':',1)
        number = int(raw)
        if prefix == 'c':
            rows = self.store.rows('SELECT d.*,c.id chunk_id,c.text,c.locator chunk_locator FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?',(number,))
        elif prefix == 'd':
            rows = self.store.rows('SELECT * FROM documents WHERE id=?',(number,))
        else:
            raise ValueError('invalid result ID')
        if not rows or not self._result(rows[0],'fetch',0):
            raise ValueError('result unavailable or outside allowed scope')
        row = rows[0]
        if row['source_id'] != 'files':
            locator = json.loads(row['locator'])
            conf = self.db_configs[row['source_id']]
            index = next(i for i in conf['index'] if i['table'] == locator['table'])
            request = {'table':locator['table'],'columns':index['text_columns'],
                       'filters':[{'column':index['id_column'],'op':'eq','value':locator['id']}],'limit':1}
            return self.query_database(row['source_id'],request)
        offset = max(0,int(offset))
        anchor = self.store.rows('SELECT count(*) n FROM chunks WHERE doc_id=? AND id<?',
                                (row['id'], number))[0]['n'] if prefix == 'c' else 0
        chunks = self.store.rows('SELECT id chunk_id,text,locator chunk_locator FROM chunks WHERE doc_id=? ORDER BY id LIMIT ? OFFSET ?',
                                (row['id'],max(1,min(int(limit),20)),anchor+offset))
        return {'document':self._result(row,'fetch',0), 'chunks':[{**c,'locator':json.loads(c['chunk_locator'])} for c in chunks],
                'snapshot':True, 'offset':offset, 'next_offset':offset+len(chunks)}

    def query_database(self,source_id,request):
        self.policy.foreground()
        conf=self.db_configs.get(source_id)
        if conf is None:
            raise ValueError('unknown source')
        return self.database.request({'method':'db_query','config':conf,'request':request},timeout=65)

    def inspect_source(self,source_id=None,**kwargs):
        if source_id and source_id!='files':
            if source_id not in self.db_configs:
                raise ValueError('unknown source')
            result = self.database.request({'method':'db_inspect','config':self.db_configs[source_id]},timeout=65)
            result['database_sync'] = self._database_progress().get(source_id)
            return result
        return {'node_id':self.config['node_id'],'roots':[str(p) for p in self.file_scope.roots],
                'file_scope':self._scope_report(),
                'databases':[{'id':k,'kind':v['kind']} for k,v in self.db_configs.items()],
                'database_sync':self._database_progress(),
                'remote_nodes':'interface_reserved_not_implemented', 'coverage':self.coverage()}

    def coverage(self):
        if self._coverage_cache is None or time.monotonic()-self._coverage_time > 5:
            states=self.store.rows('SELECT status,count(*) count FROM documents GROUP BY status')
            chunks=self.store.rows('SELECT count(*) n FROM chunks')[0]['n']
            embedded=self.store.rows('SELECT count(*) n FROM chunks c JOIN embeddings e ON c.hash=e.hash WHERE e.model=? AND c.semantic=1',(MODEL_ID,))[0]['n']
            eligible=self.store.rows('SELECT count(*) n FROM chunks WHERE semantic=1')[0]['n']
            self._coverage_cache = {'documents':{r['status']:r['count'] for r in states},'chunks':chunks,'embedded_chunks':embedded,'semantic_eligible_chunks':eligible}
            self._coverage_time = time.monotonic()
        return {**self._coverage_cache,
                'source_errors':dict(self.source_errors),'scanning':self.scanning,'last_scan':self.last_scan}

    def status(self):
        from . import __version__
        from .product import capabilities
        from .model_manager import model_status
        return {'schema_version':1,'version':__version__,'instance_id':self.instance_id,
                'node_id':self.config['node_id'],'paused':self.policy.status()['user_paused'],'last_error':self.last_error,
                'runtime_policy':self.policy.status(),'capabilities':capabilities(self),
                'file_scope':self._scope_report(),
                'coverage':self.coverage(),'resources':self.budget.snapshot(),
                'indexing':self.config.get('indexing', {}),
                'vector_index':self.vectors.status(),
                'worker_controls':{name:worker.control_status for name,worker in [('parser',self.parser),('model',self.model),('database',self.database)]},
                'database_sync':self._database_progress(),
                'scheduler':self.catalog.progress(),
                'journal':dict(self.journal_reports), 'vector_error':self.vector_error,
                'semantic':{'enabled':self.config['semantic']['enabled'],'model_ready':model_ready(self.config['semantic']['model_dir']),
                            'model_loaded':self.model.proc is not None,'model_id':MODEL_ID,'lifecycle':model_status(self.config)},
                'remote_nodes':'not_implemented'}

    def dispatch(self,method:str,params:dict):
        params=dict(params)
        node=params.pop('node_id',None)
        if node and node!=self.config['node_id']:
            raise ValueError('remote_node_not_implemented')
        if method=='index_status': return self.status()
        if method=='search': return self.search(**params)
        if method=='fetch': return self.fetch(**params)
        if method=='inspect_source': return self.inspect_source(**params)
        if method=='query_database': return self.query_database(**params)
        if method=='scope_preview':
            from .product import scope_impact
            return scope_impact(self,**params)
        if method in {'diagnose_path','prioritize_path','refresh_path','read_context','open_source'}:
            from .product import diagnose,prioritize,refresh,context,open_source
            return {'diagnose_path':diagnose,'prioritize_path':prioritize,'refresh_path':refresh,'read_context':context,'open_source':open_source}[method](self,**params)
        if method=='scan':
            self.scan_event.set()
            return {'accepted':True,'paused':self.paused}
        if method in {'pause','resume'}:
            state = self.policy.pause(params.get('seconds')) if method=='pause' else self.policy.resume()
            self.paused=state['user_paused']
            self.store.set_setting('paused',str(self.paused).lower())
            if not self.paused: self.scan_event.set()
            return {'paused':self.paused,**state}
        raise ValueError('unknown operation')

    def begin_shutdown(self):
        self.stop_event.set()
        self.paused=True
        self.vectors.cancel()
        for worker in (self.parser,self.model,self.database):
            worker.cancel()

    def close(self):
        self.begin_shutdown()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
        if self.thread:
            self.thread.join()
        if self.vector_thread:
            self.vector_thread.join()
        self.catalog.close()
        for journal in self.journals.values():
            journal.close()
        for worker in (self.parser,self.model,self.database):
            worker.close()
        self.vectors.close()
        self.store.close()
        self._release_index()
