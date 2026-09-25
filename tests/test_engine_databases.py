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
        assert result["coverage"]["source_errors"]["test-source"] == "database_snapshot_incomplete"
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
