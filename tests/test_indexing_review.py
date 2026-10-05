"""Independent recovery and ordering checks for incremental content publication."""
from pathlib import Path
import os
import sqlite3

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import ResourceLimit
from data_search.store import Store


@pytest.mark.parametrize('column, failing_sql', [
    ('ordinal', 'UPDATE chunks SET ordinal=id'),
    ('content_size', 'UPDATE documents SET content_size=size WHERE indexed_at IS NOT NULL'),
    ('content_mtime_ns', 'UPDATE documents SET content_mtime_ns=mtime_ns WHERE indexed_at IS NOT NULL'),
])
def test_interrupted_v060_column_migration_retries_complete_backfill(tmp_path, monkeypatch, column, failing_sql):
    store = Store(str(tmp_path))
    with store.db:
        doc = store.db.execute("INSERT INTO documents(key,source_id,path,name,status,size,mtime_ns,indexed_at) "
                               "VALUES('legacy','files','legacy.txt','legacy.txt','ready',42,12345,'2026-01-01')").lastrowid
        for text in ('first paragraph', 'second paragraph'):
            store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)', (doc,text,text,'{}'))
    index = store.path
    store.close()
    with sqlite3.connect(index) as db:
        db.execute('DROP INDEX chunks_document_order')
        db.execute('ALTER TABLE chunks DROP COLUMN ordinal')
        db.execute('ALTER TABLE documents DROP COLUMN content_size')
        db.execute('ALTER TABLE documents DROP COLUMN content_mtime_ns')
    original, connections = sqlite3.connect, []

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == failing_sql:
                raise sqlite3.OperationalError('injected migration interruption')
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        kwargs['factory'] = Interrupted
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(sqlite3.OperationalError, match='injected migration interruption'):
        Store(str(tmp_path))
    for connection in connections:
        connection.close()
    monkeypatch.setattr(sqlite3, 'connect', original)
    restarted = Store(str(tmp_path))
    try:
        chunks = restarted.rows('SELECT id,ordinal FROM chunks ORDER BY id')
        assert [row['ordinal'] for row in chunks] == [row['id'] for row in chunks], column
        assert restarted.rows("SELECT name FROM sqlite_master WHERE name='chunks_document_order'")
        assert restarted.rows('SELECT content_size,content_mtime_ns FROM documents') == [
            {'content_size':42, 'content_mtime_ns':12345}]
    finally:
        restarted.close()


@pytest.fixture
def local(tmp_path):
    root = tmp_path/'files'
    root.mkdir()
    config = defaults(str(tmp_path/'index'), [str(root)])
    config['semantic']['enabled'] = False
    config['resource'].update(min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
    engine = Engine(config)
    engine.parser.request = lambda request, *args, **kwargs: {
        'status':'ready', 'chunks':[{'text':text, 'locator':{'line_start':number+1}}
            for number,text in enumerate(Path(request['path']).read_text().splitlines())]}
    try:
        yield root, engine
    finally:
        engine.close()


def test_reordering_reused_chunks_does_not_fold_different_document_text(local):
    root, engine = local
    changed, unchanged = root/'changed.txt', root/'unchanged.txt'
    changed.write_text('sharedmarker first paragraph\nsharedmarker second paragraph')
    engine._file(changed, 'first')
    before = engine.store.rows('SELECT id,text FROM chunks ORDER BY ordinal,id')
    changed.write_text('sharedmarker second paragraph\nsharedmarker first paragraph')
    engine._file(changed, 'second')
    after = engine.store.rows('SELECT id,text FROM chunks ORDER BY ordinal,id')
    assert after == list(reversed(before))
    unchanged.write_text('sharedmarker first paragraph\nsharedmarker second paragraph')
    engine._file(unchanged, 'first')
    results = engine.search('sharedmarker', 'keyword', fold_duplicates=True)['results']
    assert {row['name'] for row in results} == {'changed.txt', 'unchanged.txt'}
    assert len({row['content_group'] for row in results}) == 2


def test_replaced_file_identity_during_prepare_finishes_new_document_queue(local):
    root, engine = local
    path = root/'replacement.txt'
    path.write_text('oldmarker')
    engine._metadata_batch([(str(path), path.stat().st_size, path.stat().st_mtime_ns)])
    rows = engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON w.doc_id=d.id')
    replacement = root/'replacement.tmp'
    replacement.write_text('newmarker')
    os.replace(replacement, path)
    engine._parse_batch(rows, {'parser_workers':1})
    documents = engine.store.rows('SELECT id,status FROM documents')
    assert len(documents) == 1 and documents[0]['id'] != rows[0]['id']
    assert documents[0]['status'] == 'ready'
    assert not engine.store.rows('SELECT * FROM file_work')
    assert engine.search('newmarker', 'keyword')['results']


def test_replaced_file_budget_failure_marks_and_backs_off_new_document(local, monkeypatch):
    root, engine = local
    path = root/'replacement.txt'
    path.write_text('oldmarker')
    engine._metadata_batch([(str(path), path.stat().st_size, path.stat().st_mtime_ns)])
    rows = engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON w.doc_id=d.id')
    replacement = root/'replacement.tmp'
    replacement.write_text('newmarker')
    os.replace(replacement, path)

    def fail_publication(*args):
        raise ResourceLimit('system_memory_pressure')

    monkeypatch.setattr(engine, '_commit_file', fail_publication)
    engine._parse_batch(rows, {'parser_workers':1})
    document = engine.store.rows('SELECT id,status,reason FROM documents')[0]
    assert document['id'] != rows[0]['id']
    assert document['status'] == 'budget'
    assert document['reason'] == 'system_memory_pressure'
    work = engine.store.rows('SELECT * FROM file_work')[0]
    assert work['doc_id'] == document['id'] and work['attempts'] == 1
    assert work['available_at'] > 0


def test_pause_between_batch_publications_rolls_back_and_resume_replays(local, monkeypatch):
    root, engine = local
    paths = [root/'first.txt', root/'second.txt']
    for number, path in enumerate(paths):
        path.write_text(f'oldmarker{number}')
        engine._file(path, 'before')
    before = engine.store.rows('SELECT * FROM chunks ORDER BY id')
    for number, path in enumerate(paths):
        path.write_text(f'newmarker{number}')
    engine._metadata_batch([(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths])
    rows = engine.store.rows('SELECT d.*,w.attempts FROM documents d JOIN file_work w ON w.doc_id=d.id ORDER BY d.id')
    generation = engine.store.setting('vector_generation')
    original, commits = engine._commit_file, []

    def pause_after_first(job, result):
        value = original(job, result)
        commits.append(job['id'])
        engine.paused = True
        return value

    monkeypatch.setattr(engine, '_commit_file', pause_after_first)
    with pytest.raises(ResourceLimit, match='paused'):
        engine._parse_batch(rows, {'parser_workers':2})
    assert len(commits) == 1
    assert engine.store.rows('SELECT * FROM chunks ORDER BY id') == before
    assert engine.store.setting('vector_generation') == generation
    assert engine.store.rows('SELECT status FROM documents') == [{'status':'pending'}, {'status':'pending'}]
    assert engine.store.rows('SELECT attempts,available_at FROM file_work') == [
        {'attempts':0, 'available_at':0.0}, {'attempts':0, 'available_at':0.0}]
    monkeypatch.setattr(engine, '_commit_file', original)
    engine.paused = False
    engine._parse_batch(rows, {'parser_workers':2})
    assert not engine.store.rows('SELECT * FROM file_work')
    assert engine.search('newmarker0', 'keyword')['results']
    assert engine.search('newmarker1', 'keyword')['results']
