import copy
import json
import sqlite3

import pytest

from data_search.config import defaults
from data_search.engine import Engine


@pytest.fixture
def database_config(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, public_text TEXT, private_text TEXT)")
        connection.executemany("INSERT INTO records VALUES (?, ?, ?)", [
            (1, "oldmarker server operating costs", "secretmarker private content"),
            (2, "secondmarker storage planning", "private two"),
            (3, "thirdmarker network configuration", "private three"),
        ])
    config = defaults(str(tmp_path / "index"), [])
    config["semantic"]["enabled"] = False
    config["resource"].update(min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
    config["databases"] = [{"id": "test-source", "kind": "sqlite", "path": str(path),
        "allowed_tables": ["records"], "allowed_columns": {"records": ["id", "public_text", "private_text"]},
        "index": [{"table": "records", "id_column": "id", "text_columns": ["public_text", "private_text"]}]}]
    return config, path


def search(engine, text):
    return engine.search(text, mode="keyword", source_id="test-source")["results"]


def scan(engine):
    result = engine.scan_once()
    assert result["last_error"] is None, result
    return result


def test_full_database_snapshot_updates_and_removes_deleted_records(database_config):
    config, path = database_config
    engine = Engine(config)
    try:
        scan(engine)
        before = search(engine, "oldmarker")
        assert len(before) == 1
        assert search(engine, "secondmarker")
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='newmarker efficient server plan' WHERE id=1")
            connection.execute("DELETE FROM records WHERE id=2")
        result = scan(engine)
        assert not result["coverage"]["source_errors"]
        assert not search(engine, "oldmarker")
        assert search(engine, "newmarker")
        assert not search(engine, "secondmarker")
        assert search(engine, "thirdmarker")
    finally:
        engine.close()


def test_incomplete_snapshot_cannot_remove_unseen_records(database_config):
    config, path = database_config
    engine = Engine(config)
    try:
        scan(engine)
        assert search(engine, "thirdmarker")
        config["databases"][0]["index_max_rows"] = 1
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='newmarker current first record' WHERE id=1")
            connection.execute("DELETE FROM records WHERE id=3")
        result = scan(engine)
        progress = result['database_sync']['test-source']['tables']['records']
        assert progress['phase'] == 'scanning'
        assert progress['scanned_rows'] == 1
        assert not result['coverage']['source_errors'], 'An unfinished bounded cycle is progress, not failure'
        assert search(engine, "newmarker")
        assert search(engine, "thirdmarker"), "An incomplete snapshot must retain records it did not inspect"
        config["databases"][0]["index_max_rows"] = 10
        scan(engine)
        assert not search(engine, "thirdmarker")
    finally:
        engine.close()


@pytest.mark.parametrize("revocation", ["field", "index_field", "table", "source"])
def test_restart_revocation_removes_cached_text_before_next_scan(database_config, revocation):
    config, _ = database_config
    engine = Engine(config)
    try:
        scan(engine)
        assert search(engine, "secretmarker")
    finally:
        engine.close()
    new_config = copy.deepcopy(config)
    source = new_config["databases"][0]
    if revocation in {"field", "index_field"}:
        source["index"][0]["text_columns"] = ["public_text"]
        if revocation == "field":
            source["allowed_columns"]["records"] = ["id", "public_text"]
    elif revocation == "table":
        source["allowed_tables"] = []
        source["allowed_columns"] = {}
        source["index"] = []
    else:
        new_config["databases"] = []
    engine = Engine(new_config)
    try:
        assert not search(engine, "secretmarker"), "Revoked cached text must disappear immediately on restart"
        if revocation in {"field", "index_field"}:
            scan(engine)
            assert search(engine, "oldmarker")
            assert not search(engine, "secretmarker")
    finally:
        engine.close()


def test_database_fetch_reads_current_source_and_never_serves_stale_row(database_config):
    config, path = database_config
    engine = Engine(config)
    try:
        scan(engine)
        result_id = search(engine, "oldmarker")[0]["id"]
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='livemarker updated without reindex' WHERE id=1")
        fetched = engine.fetch(result_id)
        text = json.dumps(fetched)
        assert "livemarker" in text
        assert "oldmarker" not in text
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM records WHERE id=1")
        assert engine.fetch(result_id)["rows"] == []
    finally:
        engine.close()



def progress(engine, table='records'):
    return engine.status()['database_sync']['test-source']['tables'][table]


def source_count(engine):
    return engine.store.rows("SELECT count(*) n FROM documents WHERE source_id='test-source'")[0]['n']


def complete_cycle(engine, max_ticks=30):
    for _ in range(max_ticks):
        scan(engine)
        if progress(engine)['phase'] == 'idle':
            return
    pytest.fail('Database cycle did not finish within its bounded tick budget')


def test_more_than_thousand_rows_resume_after_restart(database_config):
    config, path = database_config
    with sqlite3.connect(path) as connection:
        connection.execute('DELETE FROM records')
        connection.executemany('INSERT INTO records VALUES(?,?,?)', [(i, f'recordmarker{i} payload', 'private') for i in range(1, 1128)])
    config['databases'][0]['sync'] = {'page_size': 137, 'max_pages_per_tick': 2}
    engine = Engine(config)
    try:
        scan(engine)
        assert source_count(engine) == 274
        assert progress(engine)['cursor'] == 274
        generation = progress(engine)['generation']
    finally:
        engine.close()
    engine = Engine(config)
    try:
        scan(engine)
        assert source_count(engine) == 548, 'A restarted sync must resume instead of repeating the first rows'
        assert progress(engine)['generation'] == generation
        complete_cycle(engine)
        assert source_count(engine) == 1127
        assert progress(engine)['scanned_rows'] == 1127
        assert search(engine, 'recordmarker1127')
    finally:
        engine.close()


def test_watermarks_updates_same_stamp_and_periodic_delete_reconcile(database_config):
    config, path = database_config
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE records ADD COLUMN updated INTEGER NOT NULL DEFAULT 10')
    conf = config['databases'][0]
    conf['allowed_columns']['records'].append('updated')
    conf['index'][0]['updated_column'] = 'updated'
    conf['sync'] = {'page_size': 2, 'max_pages_per_tick': 4, 'reconcile_interval_seconds': 3600}
    engine = Engine(config)
    try:
        complete_cycle(engine)
        assert progress(engine)['watermark'] == [10, 3]
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='incrementmarker', updated=11 WHERE id=1")
            connection.execute('DELETE FROM records WHERE id=2')
            connection.execute("UPDATE records SET public_text='backdatedmarker', updated=9 WHERE id=3")
            connection.execute("INSERT INTO records VALUES(4,'insertmarker','private',11)")
        complete_cycle(engine)
        assert progress(engine)['mode'] == 'incremental'
        assert search(engine, 'incrementmarker') and search(engine, 'insertmarker')
        assert search(engine, 'secondmarker'), 'An incremental cycle cannot prove source deletions'
        assert not search(engine, 'backdatedmarker')
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='samestampmarker' WHERE id=1")
        complete_cycle(engine)
        assert search(engine, 'samestampmarker'), 'Replay equal-watermark IDs on a subsequent cycle'
        state = json.loads(engine.store.setting('database_sync:test-source'))
        state['tables']['records']['last_full_at'] = 0
        engine.store.set_setting('database_sync:test-source', json.dumps(state))
        complete_cycle(engine)
        assert progress(engine)['mode'] == 'full'
        assert not search(engine, 'secondmarker')
        assert search(engine, 'backdatedmarker')
    finally:
        engine.close()


def test_page_error_retains_old_rows_and_cursor(database_config, monkeypatch):
    config, path = database_config
    engine = Engine(config)
    try:
        scan(engine)
        config['databases'][0]['sync'] = {'page_size': 1, 'max_pages_per_tick': 1}
        with sqlite3.connect(path) as connection:
            connection.execute('DELETE FROM records WHERE id=3')
        scan(engine)
        assert progress(engine)['cursor'] == 1
        original = engine.database.request
        def fail_page(*args, **kwargs):
            raise RuntimeError('temporary source failure')
        monkeypatch.setattr(engine.database, 'request', fail_page)
        result = scan(engine)
        assert result['coverage']['source_errors']['test-source']
        assert progress(engine)['cursor'] == 1
        assert search(engine, 'thirdmarker')
        monkeypatch.setattr(engine.database, 'request', original)
        complete_cycle(engine)
        assert not search(engine, 'thirdmarker')
        assert not engine.status()['coverage']['source_errors']
    finally:
        engine.close()


def test_partial_page_application_replays_without_losing_chunks(database_config, monkeypatch):
    config, _ = database_config
    engine = Engine(config)
    try:
        original = engine._write_chunks
        calls = 0
        def interrupted(doc_id, chunks):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated interruption')
            return original(doc_id, chunks)
        monkeypatch.setattr(engine, '_write_chunks', interrupted)
        scan(engine)
        assert progress(engine)['cursor'] is None
        assert engine.store.rows("SELECT version FROM documents WHERE locator LIKE '%2%'")[0]['version'] is None
        monkeypatch.setattr(engine, '_write_chunks', original)
        complete_cycle(engine)
        assert source_count(engine) == 3
        assert search(engine, 'secondmarker')
    finally:
        engine.close()


def test_configuration_change_invalidates_midscan_checkpoint(database_config):
    config, path = database_config
    config['databases'][0]['sync'] = {'page_size': 1, 'max_pages_per_tick': 1}
    engine = Engine(config)
    try:
        scan(engine)
        generation = progress(engine)['generation']
        config['databases'][0]['index'][0]['text_columns'] = ['public_text']
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE records SET public_text='changedconfigmarker' WHERE id=1")
        scan(engine)
        assert progress(engine)['cursor'] == 1
        assert progress(engine)['generation'] != generation
        assert search(engine, 'changedconfigmarker')
        assert not search(engine, 'secretmarker')
    finally:
        engine.close()


def test_deletion_cleanup_is_bounded_and_resumable(database_config):
    config, path = database_config
    config['databases'][0]['sync'] = {'page_size': 2, 'max_pages_per_tick': 1}
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO records VALUES(?,?,?)', [(i, 'record', 'private') for i in range(4, 16)])
    engine = Engine(config)
    try:
        complete_cycle(engine)
        assert source_count(engine) == 15
        with sqlite3.connect(path) as connection:
            connection.execute('DELETE FROM records')
        scan(engine)
        assert progress(engine)['phase'] == 'reconciling'
        assert source_count(engine) == 15
        scan(engine)
        assert source_count(engine) == 13
        assert progress(engine)['deleted_rows'] == 2
    finally:
        engine.close()
    engine = Engine(config)
    try:
        assert source_count(engine) == 13
        assert progress(engine)['deleted_rows'] == 2
        complete_cycle(engine)
        assert source_count(engine) == 0
        assert progress(engine)['phase'] == 'idle'
    finally:
        engine.close()


def test_multiple_tables_share_tick_budget_without_starvation(database_config):
    config, path = database_config
    conf = config['databases'][0]
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE other (id INTEGER PRIMARY KEY, body TEXT)')
        connection.execute("INSERT INTO other VALUES(1, 'othermarker')")
    conf['allowed_tables'].append('other')
    conf['allowed_columns']['other'] = ['id', 'body']
    conf['index'].append({'table': 'other', 'id_column': 'id', 'text_columns': ['body']})
    conf['sync'] = {'page_size': 1, 'max_pages_per_tick': 1}
    engine = Engine(config)
    try:
        scan(engine)
        assert progress(engine)['cursor'] == 1
        scan(engine)
        assert progress(engine, 'other')['scanned_rows'] == 1
        assert search(engine, 'othermarker')
    finally:
        engine.close()
