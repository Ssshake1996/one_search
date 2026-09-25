from __future__ import annotations

import array
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import contained
from .model import MODEL_ID, model_ready
from .resources import Budget, ResourceLimit
from .store import Store, query_terms, terms, text_hash
from .vectors import Vectors
from .workers import Worker


def now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, config: dict):
        self.config = config
        self.store = Store(config['data_dir'])
        self.budget = Budget(config)
        self.parser, self.model, self.database = [Worker(self.budget) for _ in range(3)]
        self.vectors = Vectors(self.store, self.budget)
        self.vector_lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop_event, self.scan_event = threading.Event(), threading.Event()
        self.paused = self.store.setting('paused') == 'true'
        self.scanning = False
        self.last_error = None
        self.last_scan = self.store.setting('last_scan') or None
        self.source_errors = {}
        self.dirty = set()
        self.dirty_lock = threading.Lock()
        self.observer = self.thread = None
        self._coverage_cache = None
        self._coverage_time = 0
        self.db_configs = {c['id']: c for c in config['databases']}
        fingerprints = {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        for key, value in self.db_configs.items()}
        previous = json.loads(self.store.setting('database_configs', '{}'))
        scope = hashlib.sha256(json.dumps({key:config[key] for key in ('roots','data_dir','exclude_names')},
                                         sort_keys=True).encode()).hexdigest()
        scope_changed = self.store.setting('file_scope') != scope
        # Revocation of a configured root/data source also removes its cached content.
        revoked = []
        scan_sql = 'SELECT id,path,source_id,locator FROM documents'
        if not scope_changed:
            scan_sql += " WHERE source_id<>'files'"
        for doc in self.store.rows(scan_sql):
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
        self.store.set_setting('database_configs', json.dumps(fingerprints))
        self.store.set_setting('file_scope', scope)

    def allowed(self, path: Path) -> bool:
        if contained(path, Path(self.config['data_dir'])):
            return False
        for root in self.config['roots']:
            try:
                relative = path.resolve().relative_to(Path(root).resolve())
                if not any(p.casefold() in {n.casefold() for n in self.config['exclude_names']} for p in relative.parts):
                    return True
            except (ValueError, OSError):
                continue
        return False

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
            self.vectors.close()
            value = int(self.store.setting('vector_generation', '0')) + 1
            self.store.set_setting('vector_generation', str(value))

    def _encode(self, texts: list[str], query=False):
        if not self.config['semantic']['enabled']:
            raise RuntimeError('semantic_disabled')
        if not model_ready(self.config['semantic']['model_dir']):
            raise RuntimeError('model_missing: run model-download')
        return self.model.request({'method': 'encode', 'texts': texts, 'query': query,
                                   'model_dir': self.config['semantic']['model_dir'],
                                   'threads': self.config['semantic']['threads']}, timeout=90)

    def _write_chunks(self, doc_id: int, chunks: list[dict]):
        grouped = []
        for chunk in chunks:
            if (grouped and grouped[-1]['locator'] == chunk['locator'] and
                len(grouped[-1]['text']) + len(chunk['text']) < 1200):
                grouped[-1]['text'] += '\n' + chunk['text']
            else:
                grouped.append({'text':chunk['text'], 'locator':chunk['locator']})
        with self.vector_lock, self.store.lock, self.store.db:
            self.store.clear_chunks(doc_id)
            for chunk in grouped:
                # BGE max 512 tokens: bounded pieces with overlap preserve later paragraphs.
                text = chunk['text']
                for start in range(0, len(text), 300):
                    part = text[start:start+350]
                    if not part.strip():
                        continue
                    loc = dict(chunk['locator'])
                    loc.update({'char_start': start, 'char_end': start + len(part)})
                    cursor = self.store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)',
                        (doc_id, part, text_hash(part), json.dumps(loc, ensure_ascii=False)))
                    self.store.db.execute('INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)',
                                          (cursor.lastrowid, ' '.join(terms(part))))
            self._changed()

    def _file(self, path: Path, seen: str, metadata_only=False):
        if self.stop_event.is_set() or self.paused:
            raise ResourceLimit('paused')
        if not self.allowed(path) or path.is_symlink():
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
        self.budget.check(disk=True, reserve_mb=8)
        with self.vector_lock, self.store.lock, self.store.db:
            if existing:
                doc = existing[0]
                self.store.db.execute('UPDATE documents SET seen=? WHERE id=?', (seen, doc['id']))
                if doc['mtime_ns'] == stat.st_mtime_ns and doc['size'] == stat.st_size and doc['status'] not in {'pending','budget'}:
                    return
                doc_id = doc['id']
                self.store.clear_chunks(doc_id)
                self.store.db.execute('UPDATE documents SET size=?,mtime_ns=?,status=?,reason=NULL,indexed_at=NULL WHERE id=?',
                                      (stat.st_size, stat.st_mtime_ns, 'pending', doc_id))
                self._changed()
            else:
                cursor = self.store.db.execute('INSERT INTO documents(key,source_id,path,name,extension,size,mtime_ns,status,seen) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('file:'+key, 'files', key, path.name, path.suffix.lower(), stat.st_size, stat.st_mtime_ns, 'pending', seen))
                doc_id = cursor.lastrowid
                self.store.db.execute('INSERT INTO paths_fts(rowid,path) VALUES(?,?)', (doc_id, key))
        if metadata_only:
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
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE documents SET status=?,reason=?,indexed_at=? WHERE id=?',
                                  (status, reason, now(), doc_id))
        time.sleep(self.config['resource'].get('batch_sleep_ms', 50)/1000)

    def _files(self, full=True):
        seen = str(uuid.uuid4())
        if not full:
            with self.dirty_lock:
                dirty, self.dirty = self.dirty, set()
            for name in dirty:
                path = Path(name)
                if path.is_dir():
                    self.scan_event.set()
                else:
                    self._file(path, seen)
            return
        for root in self.config['roots']:
            base = Path(root)
            if not base.is_dir():
                self.source_errors[root] = 'root_unavailable'
                continue
            failed = []
            for parent, dirs, names in os.walk(base, followlinks=False, onerror=lambda exc: failed.append(type(exc).__name__)):
                dirs[:] = [d for d in dirs if d not in self.config['exclude_names'] and
                           not Path(parent,d).is_symlink() and self.allowed(Path(parent,d))]
                for name in names:
                    try:
                        self._file(Path(parent,name), seen, metadata_only=True)
                    except OSError:
                        failed.append('read_error')
            if failed:
                self.source_errors[root] = 'scan_incomplete'
            else:
                self.source_errors.pop(root, None)
                missing = [r['id'] for r in self.store.rows("SELECT id,path FROM documents WHERE source_id='files' AND seen<>?", (seen,)) if contained(Path(r['path']), base)]
                if missing:
                    self.store.remove(missing)
                    self._changed()
        # Publish the whole filename catalog before starting expensive parsers.
        # Page the pending queue so millions of names do not become Python objects.
        after = 0
        while not self.stop_event.is_set() and not self.paused:
            pending = self.store.rows("SELECT id,path FROM documents WHERE source_id='files' AND seen=? AND status IN ('pending','budget') AND id>? ORDER BY id LIMIT 100", (seen, after))
            if not pending:
                break
            for row in pending:
                self._file(Path(row['path']), seen)
                after = row['id']

    def _database_scan(self):
        for source_id, conf in self.db_configs.items():
            if not conf.get('index'):
                continue
            seen = str(uuid.uuid4())
            try:
                snapshot = self.database.request({'method': 'db_documents', 'config': conf,
                    'max_rows': conf.get('index_max_rows', 1000)}, timeout=65)
                complete = False
                for item in snapshot:
                    if item['kind'] == 'snapshot':
                        complete = item['complete']
                        continue
                    if self.paused or self.stop_event.is_set():
                        raise ResourceLimit('paused')
                    self.budget.check(disk=True, reserve_mb=len(item['text'].encode('utf-8'))*8/1048576+4)
                    key = 'db:' + source_id + ':' + item['key']
                    old = self.store.rows('SELECT * FROM documents WHERE key=?', (key,))
                    with self.store.lock, self.store.db:
                        if old:
                            doc_id = old[0]['id']
                            self.store.db.execute('UPDATE documents SET seen=? WHERE id=?', (seen, doc_id))
                            if old[0]['version'] == item['version']:
                                continue
                            self.store.db.execute('UPDATE documents SET version=?,locator=?,indexed_at=? WHERE id=?',
                                (item['version'], json.dumps(item['locator']), now(), doc_id))
                        else:
                            cursor = self.store.db.execute('INSERT INTO documents(key,source_id,path,name,version,status,indexed_at,seen,locator) VALUES(?,?,?,?,?,?,?,?,?)',
                                (key, source_id, item['table'], item['table'], item['version'], 'ready', now(), seen, json.dumps(item['locator'])))
                            doc_id = cursor.lastrowid
                        partial = item['locator'].get('truncated', False)
                        self.store.db.execute('UPDATE documents SET status=?,reason=? WHERE id=?',
                            ('partial' if partial else 'ready', 'record_text_limit' if partial else None, doc_id))
                    self._write_chunks(doc_id, [{'text': item['text'], 'locator': item['locator']}])
                if complete:
                    removed = [r['id'] for r in self.store.rows('SELECT id FROM documents WHERE source_id=? AND seen<>?', (source_id,seen))]
                    if removed:
                        self.store.remove(removed)
                        self._changed()
                    self.source_errors.pop(source_id, None)
                else:
                    self.source_errors[source_id] = 'database_snapshot_incomplete'
            except Exception as exc:
                self.source_errors[source_id] = str(exc) if isinstance(exc,ResourceLimit) else type(exc).__name__
            finally:
                self.database.close()

    def _embed_pending(self):
        if not self.config['semantic']['enabled'] or not model_ready(self.config['semantic']['model_dir']):
            return
        batch = self.config['semantic']['batch_size']
        while not self.paused and not self.stop_event.is_set():
            pending = self.store.rows('SELECT c.hash,min(c.text) text FROM chunks c LEFT JOIN embeddings e ON c.hash=e.hash AND e.model=? WHERE e.hash IS NULL GROUP BY c.hash LIMIT ?', (MODEL_ID,batch))
            if not pending:
                break
            self.budget.check(disk=True, reserve_mb=8)
            encoded = self._encode([r['text'] for r in pending])
            with self.store.lock, self.store.db:
                for row, vector in zip(pending, encoded):
                    blob = array.array('f', vector).tobytes()
                    self.store.db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (row['hash'],MODEL_ID,blob))
            self._changed()
            time.sleep(self.config['resource'].get('batch_sleep_ms',50)/1000)
        if not self.paused and not self.stop_event.is_set():
            with self.vector_lock:
                self.vectors.sync(cancelled=lambda: self.paused or self.stop_event.is_set())

    def scan_once(self, full=True) -> dict:
        if not self.scan_lock.acquire(blocking=False):
            return {'accepted': False, 'reason': 'already_scanning'}
        self.scanning, self.last_error = True, None
        try:
            if self.paused:
                return {'accepted': False, 'reason': 'paused'}
            self._files(full)
            self._database_scan()
            self._embed_pending()
            with self.store.lock, self.store.db:
                self.store.db.execute('DELETE FROM embeddings WHERE hash NOT IN (SELECT hash FROM chunks)')
            self.last_scan = now()
            self.store.set_setting('last_scan', self.last_scan)
        except Exception as exc:
            self.last_error = str(exc)[:200] if isinstance(exc,ResourceLimit) else type(exc).__name__
        finally:
            self.parser.close()
            self._coverage_cache = None
            self.scanning = False
            self.scan_lock.release()
        return self.status()

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
        self.observer = Observer()
        for root in self.config['roots']:
            if Path(root).is_dir():
                self.observer.schedule(Handler(), root, recursive=True)
        try:
            self.observer.start()
        except OSError:
            self.observer = None
            self.source_errors['watcher'] = 'unavailable; periodic reconciliation active'
        self.scan_event.set()
        def loop():
            last_tick = last_full = 0
            while not self.stop_event.wait(.2):
                moment = time.monotonic()
                full = self.scan_event.is_set() or moment-last_full >= self.config['reconcile_interval_seconds']
                if not self.paused and (full or moment-last_tick >= self.config['scan_interval_seconds']):
                    self.scan_event.clear()
                    self.scan_once(full=full)
                    last_tick = moment
                    if full:
                        last_full = moment
                self.model.idle_close(self.config['semantic']['idle_seconds'])
                self.database.idle_close(5)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def _result(self, row: dict, match: str, score: float) -> dict | None:
        if row['source_id'] == 'files':
            p = Path(row['path'])
            if not self.allowed(p) or not p.is_file() or p.is_symlink():
                return None
            try:
                with p.open('rb'):
                    stat = p.stat()
                stale = stat.st_mtime_ns != row['mtime_ns'] or stat.st_size != row['size']
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
                'reason': row['reason'], 'match': match, 'score': score}

    def search(self, query: str, mode='hybrid', limit=20, source_id=None, extension=None, **kwargs):
        start = time.perf_counter()
        if mode not in {'files','keyword','semantic','hybrid'} or not isinstance(query,str) or not query.strip() or len(query)>1000:
            raise ValueError('invalid mode or query (1..1000 characters)')
        limit = max(1,min(int(limit),100))
        candidates, warnings = {}, []
        doc_filter, args = '', []
        if source_id:
            doc_filter += ' AND d.source_id=?'
            args.append(source_id)
        if extension:
            doc_filter += ' AND d.extension=?'
            args.append(extension.lower())
        if mode == 'files':
            # Trigram accelerates substrings >=3; short patterns use a bounded SQLite scan.
            if len(query)>=3:
                sql = 'SELECT d.* FROM paths_fts p JOIN documents d ON d.id=p.rowid WHERE p.path LIKE ?'
            else:
                sql = 'SELECT d.* FROM documents d WHERE d.path LIKE ?'
            escape = ''
            literal = query
            if '%' in query or '_' in query:
                literal = query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                escape = " ESCAPE '\\'"
            rows = self.store.rows(sql + escape + doc_filter + ' LIMIT ?', ['%'+literal+'%']+args+[limit*3])
            results = [r for row in rows if (r:=self._result(row,'filename',1.0))][:limit]
        else:
            if mode in {'keyword','hybrid'} and (expression:=query_terms(query)):
                rows = self.store.rows('SELECT c.id chunk_id,bm25(chunks_fts) rank FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.id=c.doc_id WHERE chunks_fts MATCH ?'+doc_filter+' ORDER BY rank LIMIT ?', [expression]+args+[max(100,limit*5)])
                for rank,row in enumerate(rows):
                    candidates[row['chunk_id']] = {'score': 1/(60+rank+1), 'matches': ['keyword']}
            if mode in {'semantic','hybrid'}:
                if source_id or extension:
                    warnings.append('semantic_filters_apply_to_bounded_candidates; keyword search applies filters before ranking')
                try:
                    vector = self._encode([query],query=True)[0]
                    with self.vector_lock:
                        hits = self.vectors.search(vector,max(100,limit*5))
                    for rank,(chunk_id,distance) in enumerate(hits):
                        item = candidates.setdefault(chunk_id, {'score':0,'matches':[]})
                        item['score'] += 1/(60+rank+1)
                        item['matches'].append('semantic')
                except Exception as exc:
                    warnings.append(str(exc)[:200])
            results, seen_docs = [], set()
            for chunk_id,item in sorted(candidates.items(),key=lambda p:-p[1]['score']):
                rows = self.store.rows('SELECT d.*,c.id chunk_id,c.text,c.locator chunk_locator FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?'+doc_filter,[chunk_id]+args)
                if not rows or rows[0]['id'] in seen_docs:
                    continue
                row=rows[0]
                result = self._result(row,'+'.join(item['matches']),item['score'])
                if result:
                    results.append(result)
                    seen_docs.add(row['id'])
                if len(results)>=limit:
                    break
        return {'results': results, 'elapsed_ms': round((time.perf_counter()-start)*1000,2),
                'warnings': warnings, 'coverage': self.coverage(),
                'evidence_only': True, 'note': 'Similarity is not proof that a question is answerable; use the quoted evidence.'}

    def fetch(self,id:str,offset=0,limit=10,**kwargs):
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
        conf=self.db_configs.get(source_id)
        if conf is None:
            raise ValueError('unknown source')
        return self.database.request({'method':'db_query','config':conf,'request':request},timeout=65)

    def inspect_source(self,source_id=None,**kwargs):
        if source_id and source_id!='files':
            if source_id not in self.db_configs:
                raise ValueError('unknown source')
            return self.database.request({'method':'db_inspect','config':self.db_configs[source_id]},timeout=65)
        return {'node_id':self.config['node_id'],'roots':self.config['roots'],
                'databases':[{'id':k,'kind':v['kind']} for k,v in self.db_configs.items()],
                'remote_nodes':'interface_reserved_not_implemented', 'coverage':self.coverage()}

    def coverage(self):
        if self._coverage_cache is None or time.monotonic()-self._coverage_time > 5:
            states=self.store.rows('SELECT status,count(*) count FROM documents GROUP BY status')
            chunks=self.store.rows('SELECT count(*) n FROM chunks')[0]['n']
            embedded=self.store.rows('SELECT count(*) n FROM chunks c JOIN embeddings e ON c.hash=e.hash WHERE e.model=?',(MODEL_ID,))[0]['n']
            self._coverage_cache = {'documents':{r['status']:r['count'] for r in states},'chunks':chunks,'embedded_chunks':embedded}
            self._coverage_time = time.monotonic()
        return {**self._coverage_cache,
                'source_errors':dict(self.source_errors),'scanning':self.scanning,'last_scan':self.last_scan}

    def status(self):
        return {'node_id':self.config['node_id'],'paused':self.paused,'last_error':self.last_error,
                'coverage':self.coverage(),'resources':self.budget.snapshot(),
                'semantic':{'enabled':self.config['semantic']['enabled'],'model_ready':model_ready(self.config['semantic']['model_dir']),
                            'model_loaded':self.model.proc is not None,'model_id':MODEL_ID},
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
        if method=='scan':
            self.scan_event.set()
            return {'accepted':True,'paused':self.paused}
        if method in {'pause','resume'}:
            self.paused=method=='pause'
            self.store.set_setting('paused',str(self.paused).lower())
            if not self.paused: self.scan_event.set()
            return {'paused':self.paused}
        raise ValueError('unknown operation')

    def begin_shutdown(self):
        self.stop_event.set()
        self.paused=True
        for worker in (self.parser,self.model,self.database):
            worker.cancel()

    def close(self):
        self.begin_shutdown()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
        if self.thread:
            self.thread.join()
        for worker in (self.parser,self.model,self.database):
            worker.close()
        self.vectors.close()
        self.store.close()
