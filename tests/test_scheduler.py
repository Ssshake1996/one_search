import json
from pathlib import Path
import threading
import time

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import Budget, ResourceLimit


def configuration(tmp_path):
    root = tmp_path/'files'
    root.mkdir(exist_ok=True)
    config = defaults(str(tmp_path/'index'),[str(root)])
    config['semantic']['enabled'] = False
    config['resource'].update(batch_sleep_ms=0,min_available_mb=0,min_free_disk_mb=0)
    config['scheduler'].update(metadata_items_per_tick=2,metadata_batch_size=2,
                               files_per_tick=1,phase_seconds=5)
    return root,config


def fake_parser(engine):
    def extract(request,*args,**kwargs):
        return {'status':'ready','chunks':[{'text':Path(request['path']).read_text(encoding='utf-8'),
                                          'locator':{'line_start':1,'line_end':1}}]}
    engine.parser.request = extract


def finish(engine, ticks=100):
    for _ in range(ticks):
        result = engine.scan_once(full=False)
        assert result['last_error'] is None,result
        if not engine._has_work():
            return
    pytest.fail('durable queues failed to drain')


def test_discovery_and_parse_resume_after_restart(tmp_path):
    root,config = configuration(tmp_path)
    for index in range(11):
        (root/f'{index}.txt').write_text(f'resumetoken{index}',encoding='utf-8')
    engine = Engine(config)
    fake_parser(engine)
    engine.scan_once()
    first = engine.store.rows("SELECT id FROM documents WHERE status='ready'")
    assert len(first)==1
    assert engine.catalog.active
    engine.close()
    engine = Engine(config)
    fake_parser(engine)
    try:
        assert engine.catalog.active
        finish(engine)
        assert engine.store.rows('SELECT count(*) n FROM documents')[0]['n']==11
        assert engine.store.rows('SELECT count(*) n FROM chunks')[0]['n']==11
        assert all(engine.search(f'resumetoken{i}','keyword')['results'] for i in range(11))
        assert engine.store.rows('SELECT id FROM documents WHERE id=?',(first[0]['id'],))
    finally:
        engine.close()


def test_partial_discovery_does_not_delete_unvisited_documents(tmp_path):
    root,config = configuration(tmp_path)
    for index in range(6):
        (root/f'{index}.txt').write_text(f'keep{index}')
    engine = Engine(config)
    fake_parser(engine)
    try:
        engine.scan_once()
        finish(engine)
        (root/'5.txt').unlink()
        engine.scan_once()
        assert engine.catalog.active
        assert engine.store.rows('SELECT count(*) n FROM documents')[0]['n']==6
        finish(engine)
        assert engine.store.rows('SELECT count(*) n FROM documents')[0]['n']==5
    finally:
        engine.close()


def test_new_journal_file_during_cleanup_is_preserved(tmp_path):
    root,config = configuration(tmp_path)
    engine = Engine(config)
    fake_parser(engine)
    try:
        engine.catalog.begin()
        # Simulate a directory already scanned, then a journal upsert published
        # before the root's final missing-record cleanup.
        with engine.store.lock,engine.store.db:
            engine.store.db.execute('DELETE FROM file_scan_dirs')
        source=root/'created-after-directory-walk.txt'
        source.write_text('latecreation')
        engine.catalog.enqueue_events([{'path':str(source)}])
        engine.catalog.process_events()
        engine.catalog._cleanup(100)
        engine.catalog.parse()
        assert engine.search('latecreation','keyword')['results']
    finally:
        engine.close()


def test_scheduled_database_poll_waits_but_explicit_scan_refreshes(tmp_path):
    import sqlite3
    root,config=configuration(tmp_path)
    database=tmp_path/'records.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, body TEXT)')
        db.execute("INSERT INTO records VALUES(1,'firstdatabaseword')")
    config['databases']=[{'id':'db','kind':'sqlite','path':str(database),
        'allowed_tables':['records'],'allowed_columns':{'records':['id','body']},
        'index':[{'table':'records','id_column':'id','text_columns':['body']}]}]
    engine=Engine(config)
    try:
        engine.scan_once()
        assert engine.search('firstdatabaseword','keyword')['results']
        with sqlite3.connect(database) as db:
            db.execute("UPDATE records SET body='seconddatabaseword'")
        engine.scan_once(full=False)
        assert not engine.search('seconddatabaseword','keyword')['results']
        engine.scan_once(full=True)
        assert engine.search('seconddatabaseword','keyword')['results']
    finally:
        engine.close()


def test_database_and_embedding_phases_run_despite_file_backlog(tmp_path,monkeypatch):
    root,config = configuration(tmp_path)
    for index in range(10):
        (root/f'{index}.txt').write_text(f'token{index}')
    engine = Engine(config)
    fake_parser(engine)
    calls=[]
    monkeypatch.setattr(engine,'_database_scan',lambda *args: calls.append('database'))
    monkeypatch.setattr(engine,'_embed_pending',lambda: calls.append('embedding'))
    try:
        engine.scan_once()
        assert calls==['database','embedding']
        assert engine.catalog.active and engine.catalog.progress()['queued_files']>0
        calls.clear()
        monkeypatch.setattr(engine.catalog,'discover',lambda: (_ for _ in ()).throw(OSError('synthetic')))
        assert engine.scan_once(full=False)['last_error']=='OSError'
        assert calls==['database','embedding']
    finally:
        engine.close()


def test_journal_cursor_and_events_commit_together_and_survive_restart(tmp_path):
    root,config = configuration(tmp_path)
    source = root/'later.txt'
    source.write_text('journalqueuedtoken')
    engine = Engine(config)
    cursor={'journal_id':'7','next_usn':100}
    engine.catalog.enqueue_events([{'path':str(source)}],state_key='journal:test',state=cursor)
    assert json.loads(engine.store.setting('journal:test'))==cursor
    engine.close()
    engine=Engine(config)
    fake_parser(engine)
    try:
        engine.scan_once(full=False)
        assert engine.search('journalqueuedtoken','keyword')['results']
        assert not engine.store.rows('SELECT * FROM file_events')
        engine.store.db.execute("CREATE TRIGGER fail_events BEFORE INSERT ON file_events BEGIN SELECT RAISE(ABORT,'fault'); END")
        with pytest.raises(Exception,match='fault'):
            engine.catalog.enqueue_events([{'path':str(source)}],state_key='journal:test',state={'next_usn':200})
        assert json.loads(engine.store.setting('journal:test'))==cursor
    finally:
        engine.close()


def test_event_arriving_during_processing_is_not_lost(tmp_path,monkeypatch):
    root,config=configuration(tmp_path)
    source=root/'race.txt'
    source.write_text('firstvalue')
    engine=Engine(config)
    original=engine._metadata_batch
    def raced(records,seen):
        original(records,seen)
        source.write_text('secondlongervalue')
        engine.catalog.enqueue_events([{'path':str(source)}])
    try:
        engine.catalog.enqueue_events([{'path':str(source)}])
        monkeypatch.setattr(engine,'_metadata_batch',raced)
        engine.catalog.process_events()
        assert len(engine.store.rows('SELECT * FROM file_events'))==1
        monkeypatch.setattr(engine,'_metadata_batch',original)
        engine.catalog.process_events()
        assert not engine.store.rows('SELECT * FROM file_events')
        assert engine.store.rows('SELECT size FROM documents')[0]['size']==source.stat().st_size
    finally:
        engine.close()


def test_transient_parser_error_retries_unchanged_file(tmp_path,monkeypatch):
    root,config=configuration(tmp_path)
    source=root/'retry.txt'
    source.write_text('retryworks')
    engine=Engine(config)
    calls=[]
    def parser(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:
            raise OSError('temporary')
        return {'status':'ready','chunks':[{'text':'retryworks','locator':{}}]}
    monkeypatch.setattr(engine.parser,'request',parser)
    try:
        engine.scan_once()
        assert engine.store.rows('SELECT status FROM documents')[0]['status']=='error'
        with engine.store.lock,engine.store.db:
            engine.store.db.execute('UPDATE file_work SET available_at=0')
        engine.catalog.parse()
        assert len(calls)==2
        assert engine.search('retryworks','keyword')['results']
    finally:
        engine.close()


def test_watcher_queue_overflow_requests_durable_reconciliation(tmp_path):
    root,config=configuration(tmp_path)
    engine=Engine(config)
    try:
        # One synthetic batch exceeds the queue cap, without creating real files.
        events=[{'path':str(root/f'{i}.txt')} for i in range(10001)]
        engine.catalog.enqueue_events(events,state_key='journal:overflow',state={'next_usn':20})
        assert not engine.store.rows('SELECT * FROM file_events')
        assert engine.store.setting('file_reconcile_requested')=='true'
        assert json.loads(engine.store.setting('journal:overflow'))['next_usn']==20
    finally:
        engine.close()


def test_small_metadata_batch_uses_one_transaction(tmp_path):
    root,config=configuration(tmp_path)
    engine=Engine(config)
    files=[]
    for index in range(64):
        path=root/f'{index}.txt'
        path.write_text('small')
        stat=path.stat()
        files.append((str(path),stat.st_size,stat.st_mtime_ns))
    try:
        engine._metadata_batch(files,'first')
        statements=[]
        engine.store.db.set_trace_callback(statements.append)
        engine._metadata_batch(files,'next')
        engine.store.db.set_trace_callback(None)
        assert sum(sql=='COMMIT' for sql in statements)==1
        assert engine.store.rows("SELECT count(*) n FROM documents WHERE seen='next'")[0]['n']==64
    finally:
        engine.close()


def test_disk_checks_reuse_calibration_and_account_writes(tmp_path,monkeypatch):
    _,config=configuration(tmp_path)
    budget=Budget(config)
    calls=[]
    original=budget._calibrate_disk
    def record():
        calls.append(1)
        original()
    monkeypatch.setattr(budget,'_calibrate_disk',record)
    initial=budget.check(disk=True)
    budget.note_write(1048576)
    for _ in range(100):
        state=budget.check(disk=True,reserve_mb=1)
    assert len(calls)==1
    assert state['disk_mb']>=initial['disk_mb']+1
    # A near-limit estimate is recalibrated before rejecting available capacity.
    budget.note_write(20000*1048576)
    budget.check(disk=True)
    assert len(calls)==2
    (Path(config['data_dir'])/'oversize').write_bytes(b'0'*1048576)
    config['resource']['max_disk_mb']=.5
    with pytest.raises(ResourceLimit,match='disk_budget_exceeded'):
        budget.check(disk=True,reserve_mb=1)


def test_vector_build_does_not_hold_background_scheduler(tmp_path,monkeypatch):
    _,config=configuration(tmp_path)
    engine=Engine(config)
    entered,release=threading.Event(),threading.Event()
    def sync(**kwargs):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(engine.vectors,'sync',sync)
    try:
        engine._publish_vectors()
        assert entered.wait(2)
        assert engine.vector_thread.is_alive()
        assert engine.scan_once(full=False)['last_error'] is None
    finally:
        release.set()
        engine.close()


def test_failed_daemon_start_terminates_only_spawned_child(tmp_path,monkeypatch):
    from data_search import service
    _,config=configuration(tmp_path)
    config['config_path']=str(tmp_path/'config.json')
    class Process:
        stopped=False
        def poll(self):return 1 if self.stopped else None
        def terminate(self):self.stopped=True
        def wait(self,timeout=None):return 1
    child=Process()
    monkeypatch.setattr(service,'service_status',lambda _: (_ for _ in ()).throw(service.ServiceError('unavailable')))
    monkeypatch.setattr(service.subprocess,'Popen',lambda *args,**kwargs:child)
    with pytest.raises(service.ServiceError,match='did not become ready'):
        service.start_service(config,timeout=.01)
    assert child.stopped


def test_post_parse_disk_limit_persists_backoff_and_resumes(tmp_path,monkeypatch):
    root,config=configuration(tmp_path)
    config['resource']['max_disk_mb']=10
    path=root/'expanded.txt'
    path.write_text('small source with large extracted text')
    engine=Engine(config)
    calls=[]
    def extract(*args,**kwargs):
        calls.append(1)
        return {'status':'ready','chunks':[{'text':'a'*1_000_000,'locator':{}}]}
    monkeypatch.setattr(engine.parser,'request',extract)
    try:
        engine.scan_once()
        state=engine.store.rows('SELECT status,reason FROM documents')[0]
        assert state=={'status':'budget','reason':'disk_budget_exceeded'}
        work=engine.store.rows('SELECT available_at,attempts FROM file_work')[0]
        assert work['available_at']>time.time() and work['attempts']==1
        engine.scan_once(full=False)
        assert len(calls)==1
        config['resource']['max_disk_mb']=100
        monkeypatch.setattr(engine.parser,'request',lambda *a,**k: {'status':'ready','chunks':[{'text':'recoveredbody','locator':{}}]})
        with engine.store.lock,engine.store.db:
            engine.store.db.execute('UPDATE file_work SET available_at=0')
        finish(engine)
        assert engine.search('recoveredbody','keyword')['results']
        assert not engine.store.rows('SELECT 1 FROM file_work')
    finally:
        engine.close()
