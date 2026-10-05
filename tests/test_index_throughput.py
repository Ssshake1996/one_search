"""Publication, fairness and recovery invariants of the parallel content pipeline."""
from pathlib import Path
import threading
import time
import sqlite3

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import ResourceLimit


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    config = defaults(str(tmp_path / 'data'), [str(root)])
    config['semantic']['enabled'] = False
    config['resource'].update(min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
    config['scheduler'].update(phase_seconds=10, files_per_tick=16)
    engine = Engine(config)
    def parse(request, *args, **kwargs):
        return {'status': 'ready', 'chunks': [{'text': text, 'locator': {'line_start': n+1}}
                for n, text in enumerate(Path(request['path']).read_text().splitlines())]}
    engine.parser.request = parse
    try:
        yield root, engine
    finally:
        engine.close()


def enqueue(engine, paths):
    engine._metadata_batch([(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths])


def test_diff_keeps_chunk_ids_vectors_and_document_order(indexed):
    root, engine = indexed
    path = root / 'diff.txt'
    # Long independent paragraphs create separate bounded chunks.
    first = 'originalmarker ' * 70
    same = 'retainedmarker ' * 70
    path.write_text(first+'\n'+same)
    engine._file(path, 'first')
    original = engine.store.rows('SELECT * FROM chunks ORDER BY ordinal')
    retained = [row for row in original if 'retainedmarker' in row['text']]
    path.write_text('changedmarker '*70+'\n'+same)
    enqueue(engine, [path])
    old = engine.search('retainedmarker', mode='keyword')['results'][0]
    assert old['stale']
    assert engine.fetch(old['document_id'])['document']['stale']
    engine.catalog.parse()
    after = engine.store.rows('SELECT * FROM chunks ORDER BY ordinal')
    assert [(r['id'],r['hash']) for r in after if 'retainedmarker' in r['text']] == [(r['id'],r['hash']) for r in retained]
    fetched = engine.fetch(old['document_id'], limit=20)
    assert 'changedmarker' in fetched['chunks'][0]['text']
    assert not fetched['document']['stale']
    assert not engine.search('originalmarker', mode='keyword')['results']
    counters = engine.status()['performance']['counters']
    assert counters['chunks_reused'] > 0 and counters['chunks_removed'] > 0


def test_failed_publication_rolls_back_chunks_status_and_queue(indexed, monkeypatch):
    root, engine = indexed
    path = root/'atomic.txt'
    path.write_text('beforemarker')
    engine._file(path, 'first')
    before = engine.store.rows('SELECT * FROM chunks')
    generation = engine.store.setting('vector_generation')
    path.write_text('aftermarker')
    enqueue(engine, [path])
    generation = engine.store.setting('vector_generation')
    original = engine._write_chunks
    def fail(*args):
        original(*args)
        raise RuntimeError('injected before final document publication')
    monkeypatch.setattr(engine, '_write_chunks', fail)
    with pytest.raises(RuntimeError, match='injected'):
        engine.catalog.parse()
    assert engine.store.rows('SELECT * FROM chunks') == before
    assert engine.store.rows('SELECT status FROM documents')[0]['status'] == 'pending'
    assert engine.store.rows('SELECT * FROM file_work')
    assert engine.store.setting('vector_generation') == generation
    monkeypatch.setattr(engine, '_write_chunks', original)
    engine.catalog.parse()
    assert engine.search('aftermarker', mode='keyword')['results']
    assert not engine.store.rows('SELECT * FROM file_work')


def test_changed_during_parse_keeps_old_snapshot_and_retries_promptly(indexed):
    root, engine = indexed
    path = root/'editing.txt'
    path.write_text('oldmarker')
    engine._file(path, 'first')
    path.write_text('middlemarker')
    enqueue(engine, [path])
    def changing(*args, **kwargs):
        path.write_text('finalmarker')
        return {'status':'ready','chunks':[{'text':'middlemarker','locator':{}}]}
    engine.parser.request = changing
    engine.catalog.parse()
    assert engine.store.rows('SELECT text FROM chunks')[0]['text'] == 'oldmarker'
    assert engine.store.rows('SELECT reason FROM documents')[0]['reason'] == 'changed_during_read'
    work = engine.store.rows('SELECT * FROM file_work')[0]
    assert work['attempts'] == 0 and work['available_at'] < time.time()+1


def test_resource_failure_after_partial_write_rolls_back_whole_batch(indexed, monkeypatch):
    root, engine = indexed
    paths = [root/'good.txt', root/'failed.txt']
    for path in paths:
        path.write_text('oldmarker '+path.name)
        engine._file(path, 'first')
        path.write_text('newmarker '+path.name)
    enqueue(engine, paths)
    before = engine.store.rows('SELECT * FROM chunks ORDER BY id')
    generation = engine.store.setting('vector_generation')
    rows = engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON d.id=w.doc_id ORDER BY d.id')
    original = engine._write_chunks
    def fail(doc_id, chunks):
        counts = original(doc_id, chunks)
        if doc_id == rows[1]['id']:
            raise ResourceLimit('injected_write_limit')
        return counts
    monkeypatch.setattr(engine, '_write_chunks', fail)
    indexed_before = engine.telemetry.snapshot({})['counters']['files_indexed']
    engine._parse_batch(rows, {'parser_workers': 1})
    assert engine.store.rows('SELECT * FROM chunks ORDER BY id') == before
    assert engine.store.setting('vector_generation') == generation
    assert engine.telemetry.snapshot({})['counters']['files_indexed'] == indexed_before
    pending = engine.store.rows('SELECT d.status,w.attempts FROM documents d JOIN file_work w ON d.id=w.doc_id ORDER BY d.id')
    assert pending == [{'status':'pending','attempts':0}, {'status':'budget','attempts':1}]
    monkeypatch.setattr(engine, '_write_chunks', original)
    with engine.store.db:
        engine.store.db.execute('UPDATE file_work SET available_at=0')
    engine.catalog.parse()
    assert not engine.store.rows('SELECT * FROM file_work')
    assert len(engine.search('newmarker', mode='keyword')['results']) == 2


def test_recent_changes_and_initial_backlog_both_make_progress(indexed):
    root, engine = indexed
    paths=[]
    for n in range(8):
        path=root/f'{n}.txt';path.write_text(f'marker{n}');paths.append(path)
    enqueue(engine, paths)
    with engine.store.db:
        engine.store.db.execute('UPDATE file_work SET priority=1 WHERE doc_id>4')
    engine.config['scheduler']['files_per_tick']=1
    for _ in range(4):
        engine.catalog.parse()
    done=engine.store.rows("SELECT name FROM documents WHERE status='ready'")
    assert any(int(r['name'][0])<4 for r in done)
    assert any(int(r['name'][0])>=4 for r in done)


def test_parallel_parsing_does_not_hold_sqlite_lock(indexed):
    root, engine=indexed
    paths=[]
    for n in range(2):
        path=root/f'{n}.txt';path.write_text('value');paths.append(path)
    enqueue(engine,paths)
    barrier=threading.Barrier(2)
    def parse(*args,**kwargs):
        barrier.wait(timeout=3)
        # Reading from a separate parser thread would deadlock if parent held Store.lock.
        assert len(engine.store.rows('SELECT id FROM documents')) == 2
        return {'status':'ready','chunks':[{'text':'value','locator':{}}]}
    engine.parser.request=parse
    rows=engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON d.id=w.doc_id')
    assert engine._parse_batch(rows, {'parser_workers':2}) == 2
    assert engine.status()['performance']['last_batch']['workers'] == 2


def test_explicit_refresh_failure_keeps_durable_retry(indexed):
    root, engine = indexed
    path = root/'refresh.txt'
    path.write_text('preservedmarker')
    engine._file(path, 'first')
    path.write_text('changedmarker')
    def fail(*args, **kwargs):
        raise ResourceLimit('worker_memory_limit')
    engine.parser.request = fail
    engine.dispatch('refresh_path', {'path':str(path)})
    assert engine.store.rows('SELECT text FROM chunks')[0]['text'] == 'preservedmarker'
    assert engine.store.rows('SELECT * FROM file_work')[0]['attempts'] == 1


def test_batch_deadline_defers_unstarted_parsers_without_backoff(indexed, monkeypatch):
    from types import SimpleNamespace
    from data_search import engine as module
    root, engine = indexed
    paths=[]
    for n in range(4):
        path=root/f'deadline{n}.txt';path.write_text('value');paths.append(path)
    enqueue(engine,paths)
    tick=[100.]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:tick[0],time=time.time,sleep=time.sleep))
    def extract(job):
        tick[0] += 2
        return {'status':'ready','chunks':[{'text':'value','locator':{}}]}
    monkeypatch.setattr(engine,'_extract_file',extract)
    rows=engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON d.id=w.doc_id ORDER BY d.id')
    assert engine._parse_batch(rows, {'parser_workers':1}, deadline=101) == 1
    pending=engine.store.rows('SELECT attempts,available_at FROM file_work')
    assert len(pending)==3 and all(row=={'attempts':0,'available_at':0} for row in pending)


def test_debounce_and_directory_change_are_local(indexed):
    root,engine=indexed
    path=root/'newdir';path.mkdir()
    for _ in range(3):
        engine.catalog.enqueue_events([{'path':str(path),'is_directory':True}],debounce=True)
    assert engine.store.rows('SELECT version FROM file_events')[0]['version']==3
    engine.catalog.process_events()
    assert not engine.catalog.active
    with engine.store.db:
        engine.store.db.execute('UPDATE file_events SET available_at=0')
    engine.catalog.process_events()
    assert engine.store.rows('SELECT path,scan_kind FROM file_scan_roots') == [{'path':str(path),'scan_kind':'targeted'}]
    assert not engine.store.setting('file_reconcile_requested')


def test_v060_schema_migrates_content_revision_and_order_without_reparse(tmp_path):
    root=tmp_path/'files';root.mkdir()
    path=root/'legacy.txt';path.write_text('firstmarker '*80+'\n'+'lastmarker '*80)
    config=defaults(str(tmp_path/'data'),[str(root)])
    config['semantic']['enabled']=False
    engine=Engine(config)
    try:
        engine._file(path,'old')
        before=engine.store.rows('SELECT id,text FROM chunks ORDER BY ordinal,id')
        index=engine.store.path
    finally:
        engine.close()
    with sqlite3.connect(index) as db:
        db.execute('DROP INDEX chunks_document_order')
        db.execute('ALTER TABLE chunks DROP COLUMN ordinal')
        db.execute('ALTER TABLE documents DROP COLUMN content_size')
        db.execute('ALTER TABLE documents DROP COLUMN content_mtime_ns')
    restarted=Engine(config)
    try:
        assert restarted.store.rows('SELECT id,text FROM chunks ORDER BY ordinal,id') == before
        result=restarted.fetch('c:'+str(before[-1]['id']),limit=1)
        assert result['chunks'][0]['text']==before[-1]['text']
        assert not result['document']['stale']
        assert not restarted.store.rows('SELECT * FROM file_work')
    finally:
        restarted.close()
