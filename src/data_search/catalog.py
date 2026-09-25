"""Durable, time-sliced filesystem discovery and work queues.

Directory iterators are ephemeral. After a restart only the unfinished directory
is enumerated again; committed metadata and completed directories are retained.
Missing-document cleanup runs only after a successful complete root traversal.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .config import contained
from .resources import ResourceLimit
from .scope import link_directory


class FileCatalog:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.iterator = self.current = None
        with self.store.lock:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS file_scan_roots(
                    path TEXT PRIMARY KEY, generation TEXT NOT NULL, phase TEXT NOT NULL,
                    cleanup_after INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                    discovered INTEGER NOT NULL DEFAULT 0, completed_at REAL);
                CREATE TABLE IF NOT EXISTS file_scan_dirs(
                    path TEXT PRIMARY KEY, root TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS file_scan_dirs_root ON file_scan_dirs(root);
                CREATE TABLE IF NOT EXISTS file_work(
                    doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    available_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS file_work_due ON file_work(available_at,doc_id);
                CREATE TABLE IF NOT EXISTS file_events(
                    path TEXT PRIMARY KEY, directory INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1);
            """)
            if not any(row[1]=='version' for row in self.store.db.execute('PRAGMA table_info(file_events)')):
                self.store.db.execute('ALTER TABLE file_events ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
                self.store.db.commit()
            for table, column, declaration in [('file_work','priority','INTEGER NOT NULL DEFAULT 0'),
                    ('file_scan_dirs','priority','INTEGER NOT NULL DEFAULT 0'),
                    ('file_scan_roots','scan_kind',"TEXT NOT NULL DEFAULT 'full'")]:
                if not any(row[1]==column for row in self.store.db.execute('PRAGMA table_info('+table+')')):
                    self.store.db.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+declaration)
            self.store.db.commit()
            # Existing v0.2 unfinished documents enter the durable queue once.
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO file_work(doc_id) SELECT id FROM documents "
                    "WHERE source_id='files' AND (status IN ('pending','error') OR (status='budget' AND reason<>'file_size_limit'))")
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('file_queue_version','1')")
        self.restrict()
        if not self.store.setting('file_chunking_migration'):
            ceiling = self.store.rows('SELECT coalesce(max(id),0) n FROM documents')[0]['n']
            self.store.set_setting('file_chunking_migration',json.dumps({'after':0,'ceiling':ceiling,'done':ceiling==0}))

    def migrate_chunks(self):
        state = json.loads(self.store.setting('file_chunking_migration'))
        if state['done']:
            return
        batch = self.engine.config['scheduler']['metadata_batch_size']
        rows = self.store.rows('SELECT id,source_id,chunking_version,status FROM documents '
                              'WHERE id>? AND id<=? ORDER BY id LIMIT ?', (state['after'],state['ceiling'],batch))
        self.engine.budget.check(disk=True,reserve_mb=max(.016,len(rows)*.004))
        with self.store.lock,self.store.db:
            for row in rows:
                if row['source_id']=='files' and row['chunking_version']<3 and row['status'] in {'ready','partial'}:
                    self.store.db.execute("UPDATE documents SET status='pending' WHERE id=?",(row['id'],))
                    self.store.db.execute('INSERT OR IGNORE INTO file_work(doc_id) VALUES(?)',(row['id'],))
            state.update(after=rows[-1]['id'] if rows else state['after'],done=len(rows)<batch)
            self.store.db.execute("UPDATE settings SET value=? WHERE key='file_chunking_migration'",(json.dumps(state),))
        self.engine.budget.note_write(len(rows)*4096)

    def restrict(self):
        roots = {str(p) for p in self.engine.file_scope.roots}
        with self.store.lock, self.store.db:
            for row in self.store.db.execute('SELECT path FROM file_scan_roots').fetchall():
                if row['path'] not in roots and not self.engine.allowed(Path(row['path'])):
                    self.store.db.execute('DELETE FROM file_scan_dirs WHERE root=?', (row['path'],))
                    self.store.db.execute('DELETE FROM file_scan_roots WHERE path=?', (row['path'],))
                    if self.current and self.current['root'] == row['path']:
                        self.close()

    def begin(self):
        if self.active:
            return False
        self.restrict()
        with self.store.lock, self.store.db:
            for root in self.engine.file_scope.roots:
                path = str(root)
                self.store.db.execute('INSERT OR REPLACE INTO file_scan_roots(path,generation,phase) VALUES(?,?,?)',
                                      (path, str(uuid.uuid4()), 'discover'))
                self.store.db.execute('INSERT OR REPLACE INTO file_scan_dirs(path,root) VALUES(?,?)', (path,path))
            self.store.db.execute("DELETE FROM settings WHERE key='file_reconcile_requested'")
        return True

    def prioritize_directory(self, path):
        path = str(Path(path).resolve())
        with self.store.lock, self.store.db:
            current = self.store.db.execute('SELECT * FROM file_scan_roots WHERE path=?',(path,)).fetchone()
            if not current or current['phase']=='done':
                self.store.db.execute("INSERT OR REPLACE INTO file_scan_roots(path,generation,phase,scan_kind) VALUES(?,?,'discover','targeted')",(path,str(uuid.uuid4())))
            self.store.db.execute('INSERT INTO file_scan_dirs(path,root,priority) VALUES(?,?,1) ON CONFLICT(path) DO UPDATE SET priority=1',(path,path))

    @property
    def active(self):
        return bool(self.store.rows("SELECT 1 FROM file_scan_roots WHERE phase<>'done' LIMIT 1"))

    def enqueue_events(self, events, *, state_key=None, state=None, reconcile=False):
        """A journal cursor is never committed without its durable events."""
        self.engine.budget.check(disk=True,reserve_mb=.016+len(events)*.004)
        with self.store.lock, self.store.db:
            count = self.store.db.execute('SELECT count(*) FROM file_events').fetchone()[0]
            if count+len(events)>10000:
                # A durable reconciliation request is the overflow recovery
                # contract, including when a journal cursor advances here.
                events, reconcile = [], True
            self.store.db.executemany('INSERT INTO file_events(path,directory) VALUES(?,?) '
                'ON CONFLICT(path) DO UPDATE SET directory=max(directory,excluded.directory),version=version+1',
                [(str(event['path']),int(event.get('is_directory',False))) for event in events if event.get('path')])
            if reconcile:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('file_reconcile_requested','true')")
            if state_key and state is not None:
                self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (state_key,json.dumps(state)))
        self.engine.budget.note_write(len(events)*4096 + 4096)

    def _failure(self, path, error, root):
        self.engine._file_error(path, type(error).__name__)
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE file_scan_roots SET errors=errors+1 WHERE path=?',(root,))
        self.engine.source_errors[root] = 'scan_incomplete'

    def _flush(self, records, directories):
        if directories:
            self.engine.budget.check(disk=True,reserve_mb=len(directories)*.004)
        if records:
            self.engine._metadata_batch(records, self.current['generation'], priority=self.current.get('priority',0))
        with self.store.lock, self.store.db:
            if directories:
                self.store.db.executemany('INSERT OR IGNORE INTO file_scan_dirs(path,root) VALUES(?,?)', directories)
                if self.current.get('priority'):
                    self.store.db.executemany('UPDATE file_scan_dirs SET priority=1 WHERE path=?',[(path,) for path,_ in directories])
            self.store.db.execute('UPDATE file_scan_roots SET discovered=discovered+? WHERE path=?',
                                  (len(records), self.current['root']))
        if directories:
            self.engine.budget.note_write(len(directories)*4096)
        records.clear()
        directories.clear()

    def discover(self):
        settings = self.engine.config['scheduler']
        deadline = time.monotonic() + settings['phase_seconds']
        visited = finished = 0
        while visited < settings['metadata_items_per_tick'] and finished < settings['directories_per_tick']:
            if time.monotonic() >= deadline or self.engine.paused or self.engine.stop_event.is_set():
                break
            self.engine.budget.check()
            if self.iterator is None:
                rows = self.store.rows("SELECT d.path,d.root,d.priority,r.generation FROM file_scan_dirs d "
                    "JOIN file_scan_roots r ON r.path=d.root WHERE r.phase='discover' ORDER BY d.priority DESC,d.rowid LIMIT 1")
                if not rows:
                    break
                self.current = rows[0]
                path = Path(self.current['path'])
                try:
                    if not self.engine.allowed(path) or link_directory(path):
                        self._finish_directory()
                        finished += 1
                        continue
                    self.iterator = os.scandir(path)
                except OSError as error:
                    self._failure(path,error,self.current['root'])
                    self._finish_directory()
                    finished += 1
                    continue
            records, directories = [], []
            exhausted = False
            try:
                for _ in range(min(settings['metadata_batch_size'], settings['metadata_items_per_tick']-visited)):
                    if time.monotonic() >= deadline or self.engine.paused or self.engine.stop_event.is_set():
                        break
                    try:
                        entry = next(self.iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    visited += 1
                    path = Path(entry.path)
                    try:
                        if not self.engine.allowed(path) or link_directory(path) or entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories.append((str(path), self.current['root']))
                        elif entry.is_file(follow_symlinks=False):
                            # Python 3.11 DirEntry.stat on Windows can expose zero
                            # st_ino/st_dev. Path.stat obtains the actual file ID.
                            stat = path.stat() if os.name=='nt' else entry.stat(follow_symlinks=False)
                            from .product import file_identity
                            records.append((str(path),stat.st_size,stat.st_mtime_ns,file_identity(stat)))
                    except OSError as error:
                        self._failure(path,error,self.current['root'])
            except OSError as error:
                self._failure(self.current['path'],error,self.current['root'])
                exhausted = True
            try:
                self._flush(records,directories)
            except Exception:
                # Reopen this directory on retry; uncommitted records must not vanish.
                self.close()
                raise
            if exhausted:
                self._finish_directory()
                finished += 1
        self._cleanup(settings['metadata_batch_size'])
        return visited

    def _finish_directory(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM file_scan_dirs WHERE path=?', (self.current['path'],))
        self.close()

    def _cleanup(self, batch):
        from .search_filters import path_predicate
        deadline = time.monotonic()+self.engine.config['scheduler']['phase_seconds']
        roots = self.store.rows("SELECT * FROM file_scan_roots r WHERE phase<>'done' "
            "AND NOT EXISTS(SELECT 1 FROM file_scan_dirs d WHERE d.root=r.path) ORDER BY r.cleanup_after,r.path LIMIT 16")
        for root in roots:
            if self.engine.paused or self.engine.stop_event.is_set() or time.monotonic()>=deadline:
                return
            self.engine.budget.check()
            with self.store.lock, self.store.db:
                if root['errors']:
                    self.store.db.execute("UPDATE file_scan_roots SET phase='done',completed_at=? WHERE path=?",
                                          (time.time(),root['path']))
                    continue
                clause, path_args = path_predicate(root['path'])
                rows = self.store.db.execute("SELECT d.id,d.path FROM documents d WHERE d.source_id='files' "
                    "AND d.seen<>? AND d.id>?"+clause+" ORDER BY d.id LIMIT ?",[root['generation'],root['cleanup_after'],*path_args,batch]).fetchall()
                missing = []
                for row in rows:
                    path = Path(row['path'])
                    if not contained(path,Path(root['path'])):
                        continue
                    try:
                        # A change journal may already have queued a file created
                        # in a directory visited earlier in this baseline scan.
                        if not path.is_file():
                            missing.append(row['id'])
                    except OSError:
                        self.engine._file_error(path,'cleanup_access_failed')
                for doc_id in missing:
                    self.store.clear_chunks(doc_id)
                    self.store.db.execute('DELETE FROM documents WHERE id=?',(doc_id,))
                self.store.db.execute('UPDATE file_scan_roots SET phase=?,cleanup_after=?,completed_at=? WHERE path=?',
                    ('cleanup' if len(rows)==batch else 'done',rows[-1]['id'] if rows else root['cleanup_after'],
                     None if len(rows)==batch else time.time(),root['path']))
            if missing:
                self.engine._changed()
            if len(rows)<batch:
                self.engine.source_errors.pop(root['path'],None)

    def process_events(self):
        settings = self.engine.config['scheduler']
        rows = self.store.rows('SELECT path,directory,version FROM file_events ORDER BY rowid LIMIT ?',
                               (settings['metadata_batch_size'],))
        if not rows:
            return
        records = []
        seen = None
        for row in rows:
            if self.engine.paused or self.engine.stop_event.is_set():
                raise ResourceLimit('paused')
            path = Path(row['path'])
            if not self.engine.allowed(path):
                continue
            if row['directory']:
                self.enqueue_events([],reconcile=True)
                continue
            # The current file wins over queued rename/delete history when a path is reused.
            try:
                if path.is_file() and not path.is_symlink() and not link_directory(path):
                    stat = path.stat()
                    from .product import file_identity
                    records.append((str(path.resolve()),stat.st_size,stat.st_mtime_ns,file_identity(stat)))
                elif not path.exists():
                    self.engine._file(path, '')
            except OSError as error:
                self.engine._file_error(path,type(error).__name__)
                self.enqueue_events([],reconcile=True)
        self.engine._metadata_batch(records,seen)
        with self.store.lock, self.store.db:
            self.store.db.executemany('DELETE FROM file_events WHERE path=? AND version=?',
                                     [(row['path'],row['version']) for row in rows])

    def parse(self):
        settings = self.engine.config['scheduler']
        deadline = time.monotonic() + settings['phase_seconds']
        rows = self.store.rows('SELECT d.*,w.attempts FROM file_work w JOIN documents d ON d.id=w.doc_id '
            'WHERE w.available_at<=? ORDER BY w.priority DESC,w.available_at,w.doc_id LIMIT ?', (time.time(),settings['files_per_tick']))
        processed = 0
        for row in rows:
            if self.engine.paused or self.engine.stop_event.is_set() or time.monotonic() >= deadline:
                break
            try:
                self.engine._file(Path(row['path']),row['seen'])
            except ResourceLimit as error:
                if self.engine.paused or self.engine.stop_event.is_set():
                    raise
                # A post-extraction disk reservation can fail after expensive
                # parser work. Persist backoff instead of repeating it each tick.
                with self.store.lock,self.store.db:
                    self.store.db.execute("UPDATE documents SET status='budget',reason=? WHERE id=?",
                                          (str(error)[:200],row['id']))
            except OSError as error:
                self.engine._file_error(row['path'],type(error).__name__)
                with self.store.lock,self.store.db:
                    self.store.db.execute("UPDATE documents SET status='error',reason=? WHERE id=?",
                                          (type(error).__name__,row['id']))
            current = self.store.rows('SELECT status,reason FROM documents WHERE id=?',(row['id'],))
            retry = bool(current and current[0]['status'] in {'pending','budget','error'} and
                         current[0]['reason'] != 'file_size_limit')
            with self.store.lock,self.store.db:
                if retry:
                    delay = min(3600, max(5,self.engine.config['scan_interval_seconds']) * 2**min(row['attempts'],4))
                    self.store.db.execute('UPDATE file_work SET available_at=?,attempts=attempts+1 WHERE doc_id=?',
                                          (time.time()+delay,row['id']))
                else:
                    self.store.db.execute('DELETE FROM file_work WHERE doc_id=?',(row['id'],))
            processed += 1
        return processed

    def progress(self):
        roots = self.store.rows('SELECT * FROM file_scan_roots ORDER BY path')
        return {'roots':roots,'discovery_active':any(row['phase']!='done' for row in roots),
                'chunking_migration':json.loads(self.store.setting('file_chunking_migration')),
                'queued_directories':self.store.rows('SELECT count(*) n FROM file_scan_dirs')[0]['n'],
                'queued_files':self.store.rows('SELECT count(*) n FROM file_work')[0]['n'],
                'queued_events':self.store.rows('SELECT count(*) n FROM file_events')[0]['n'],
                'resume_granularity':'directory; completed metadata and parse jobs persist'}

    def close(self):
        if self.iterator is not None:
            self.iterator.close()
        self.iterator = self.current = None
