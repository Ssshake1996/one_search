"""Pause real worker processes using only temporary synthetic sources."""
import json
import sqlite3
import sys
import threading
import time

import pytest

from data_search import engine as engine_module, service, workers
from data_search.config import atomic_json, defaults, load_config
from data_search.engine import Engine
from data_search.resources import ResourceLimit


def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail('Condition did not become true before the bounded deadline')


@pytest.mark.parametrize('phase', ['parser', 'database', 'model'])
def test_pause_stops_inflight_worker_without_losing_pending_work(tmp_path, monkeypatch, phase):
    root = tmp_path / 'documents'
    root.mkdir()
    (root / 'cached.txt').write_text('cachedsearchneedle', encoding='utf-8')
    config = defaults(str(tmp_path / 'index'), [str(root)])
    config['semantic']['enabled'] = False
    config['runtime_policy'] = {'enabled': False}
    config['resource'].update(batch_sleep_ms=0, min_available_mb=0, min_free_disk_mb=0)
    if phase == 'database':
        source = tmp_path / 'source.db'
        with sqlite3.connect(source) as connection:
            connection.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, text TEXT)')
            connection.execute("INSERT INTO records VALUES(1,'cacheddbneedle')")
        config['databases'] = [{'id': 'test-db', 'kind': 'sqlite', 'path': str(source),
            'allowed_tables': ['records'], 'allowed_columns': {'records': ['id', 'text']},
            'index': [{'table': 'records', 'id_column': 'id', 'text_columns': ['text']}]}]
    engine = Engine(config)
    thread = None
    try:
        engine.scan_once()
        assert engine.search('cachedsearchneedle', 'keyword')['results']
        if phase == 'parser':
            (root / 'new.txt').write_text('resumedfileneedle', encoding='utf-8')
        elif phase == 'database':
            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO records VALUES(2,'resumeddbneedle')")
        else:
            config['semantic']['enabled'] = True
            monkeypatch.setattr(engine_module, 'model_ready', lambda _: True)

        worker = getattr(engine, phase)
        worker.close()
        started = tmp_path / 'worker-started'
        # A real CPU-bound child reproduces an extraction, database query or
        # embedding operation that has not yet returned a response.
        code = ("import pathlib,sys,time; sys.stdin.readline(); "
                f"pathlib.Path({str(started)!r}).write_text('started'); "
                "deadline=time.monotonic()+30\nwhile time.monotonic()<deadline: pass")
        command = workers.process_command
        monkeypatch.setattr(workers, 'process_command', lambda *args: [sys.executable, '-u', '-c', code])
        results = []
        thread = threading.Thread(target=lambda: results.append(engine.scan_once()))
        thread.start()
        wait_until(started.exists)
        process = worker.proc
        assert process is not None and process.poll() is None
        paused = engine.dispatch('pause', {})
        assert engine.search('cachedsearchneedle', 'keyword')['results']
        thread.join(timeout=3)
        assert not thread.is_alive(), 'Pause must interrupt the active CPU worker promptly'
        assert process.poll() is not None
        assert paused['pause_state'] in {'pausing', 'paused'}
        assert results[0]['last_error'] is None
        status = engine.status()
        assert status['pause_state'] == 'paused'
        assert not status['coverage']['source_errors']
        if phase == 'parser':
            pending = engine.store.rows("SELECT d.status,w.attempts,w.available_at FROM documents d "
                "JOIN file_work w ON d.id=w.doc_id WHERE d.name='new.txt'")[0]
            assert pending == {'status': 'pending', 'attempts': 0, 'available_at': 0.0}
        elif phase == 'database':
            state = json.loads(engine.store.setting('database_sync:test-db'))
            assert state['tables']['records']['cursor'] is None
            assert state['last_error'] is None
            assert state['next_poll_at'] == 0
        else:
            assert engine.store.rows('SELECT count(*) n FROM embeddings')[0]['n'] == 0
            assert engine.store.rows('SELECT 1 FROM embedding_queue LIMIT 1')
            with pytest.raises(ResourceLimit, match='indexing_paused_or_stopping'):
                engine._encode(['background work'])
            assert worker.proc is None
            # Foreground semantic query encoding still has access to its worker.
            reply = "import json,sys\nfor line in sys.stdin: print(json.dumps({'ok':True,'result':[[1.0,0.0]]}),flush=True)"
            monkeypatch.setattr(workers, 'process_command', lambda *args: [sys.executable, '-u', '-c', reply])
            assert engine._encode(['query'], query=True) == [[1.0, 0.0]]
            worker.close()

        monkeypatch.setattr(workers, 'process_command', command)
        assert engine.dispatch('resume', {})['pause_state'] == 'running'
        if phase != 'model':
            assert engine.scan_once()['last_error'] is None
            query = 'resumedfileneedle' if phase == 'parser' else 'resumeddbneedle'
            assert engine.search(query, 'keyword')['results']
    finally:
        engine.begin_shutdown()
        if thread is not None:
            thread.join(timeout=5)
        engine.close()


def test_real_daemon_pause_preserves_search_and_survives_restart(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    (root / 'cached.txt').write_text('cachedlocalneedle', encoding='utf-8')
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, text TEXT)')
        connection.execute("INSERT INTO records VALUES(1,'cacheddatabaseneedle')")
    path = tmp_path / 'isolated-data' / 'config.json'
    config = defaults(str(path.parent), [str(root)])
    config['semantic']['enabled'] = False
    config['runtime_policy'] = {'enabled': False}
    config['resource'].update(batch_sleep_ms=0, min_available_mb=0, min_free_disk_mb=0)
    config['databases'] = [{'id': 'test-db', 'kind': 'sqlite', 'path': str(source),
        'allowed_tables': ['records'], 'allowed_columns': {'records': ['id', 'text']},
        'index': [{'table': 'records', 'id_column': 'id', 'text_columns': ['text']}]}]
    atomic_json(path, config)
    config = load_config(path)

    def search(text):
        return service.rpc(config, 'search', {'query': text, 'mode': 'keyword'})['results']

    def status():
        return service.rpc(config, 'index_status')

    try:
        service.start_service(config)
        wait_until(lambda: search('cachedlocalneedle') and search('cacheddatabaseneedle'), seconds=15)
        paused = service.rpc(config, 'pause', {'seconds': 1800})
        wait_until(lambda: status()['pause_state'] == 'paused')
        (root / 'new.txt').write_text('resumedlocalneedle', encoding='utf-8')
        with sqlite3.connect(source) as connection:
            connection.execute("INSERT INTO records VALUES(2,'resumeddatabaseneedle')")
        service.rpc(config, 'scan')
        time.sleep(.6)
        assert search('cachedlocalneedle') and search('cacheddatabaseneedle')
        assert not search('resumedlocalneedle') and not search('resumeddatabaseneedle')
        queried = service.rpc(config, 'query_database', {'source_id': 'test-db',
            'request': {'table': 'records', 'columns': ['id', 'text']}})
        assert len(queried['rows']) == 2
        previous = service.service_status(config)['service_id']
        service.stop_service(config)
        service.start_service(config)
        assert service.service_status(config)['service_id'] != previous
        restarted = status()
        assert restarted['pause_state'] == 'paused'
        assert restarted['runtime_policy']['pause_until'] == paused['pause_until']
        assert search('cachedlocalneedle') and search('cacheddatabaseneedle')
        assert not search('resumedlocalneedle') and not search('resumeddatabaseneedle')
        assert service.rpc(config, 'resume')['pause_state'] == 'running'
        wait_until(lambda: search('resumedlocalneedle') and search('resumeddatabaseneedle'), seconds=15)
    finally:
        service.stop_service(config)


def test_database_pause_mid_page_replays_without_backoff_or_missing_rows(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, text TEXT)')
        connection.executemany('INSERT INTO records VALUES(?,?)', [(i, f'rowneedle{i}') for i in range(1, 5)])
    config = defaults(str(tmp_path / 'index'), [])
    config['semantic']['enabled'] = False
    config['resource'].update(batch_sleep_ms=0, min_available_mb=0, min_free_disk_mb=0)
    config['databases'] = [{'id': 'test-db', 'kind': 'sqlite', 'path': str(source),
        'allowed_tables': ['records'], 'allowed_columns': {'records': ['id', 'text']},
        'index': [{'table': 'records', 'id_column': 'id', 'text_columns': ['text']}]}]
    engine = Engine(config)
    try:
        apply = engine._database_apply_document

        def pause_after_first(*args):
            apply(*args)
            engine.dispatch('pause', {})

        monkeypatch.setattr(engine, '_database_apply_document', pause_after_first)
        assert engine.scan_once()['last_error'] is None
        state = json.loads(engine.store.setting('database_sync:test-db'))
        assert state['tables']['records']['cursor'] is None
        assert state['tables']['records']['scanned_rows'] == 0
        assert state['last_error'] is None and state['retry_count'] == 0 and state['next_poll_at'] == 0
        assert len(engine.store.rows("SELECT id FROM documents WHERE source_id='test-db'")) == 1
        monkeypatch.setattr(engine, '_database_apply_document', apply)
        engine.dispatch('resume', {})
        assert engine.scan_once()['last_error'] is None
        assert len(engine.store.rows("SELECT id FROM documents WHERE source_id='test-db'")) == 4
        assert all(engine.search(f'rowneedle{i}', 'keyword')['results'] for i in range(1, 5))
    finally:
        engine.close()
