"""Known-identity documents can migrate chunking without losing coverage.

Legacy records without file identity are separately tested for rejection/reparse
in test_product_journeys; an unknown historical file cannot retain trusted text.
"""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.store import Store, text_hash


def configuration(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    config = defaults(str(tmp_path / "index"), [str(root)])
    config["semantic"]["enabled"] = False
    config["resource"].update(batch_sleep_ms=0, min_available_mb=0, min_free_disk_mb=0)
    config["scheduler"].update(metadata_batch_size=2, metadata_items_per_tick=2,
                               files_per_tick=1, phase_seconds=5)
    return root, config


def legacy_files(tmp_path, count=5):
    root, config = configuration(tmp_path)
    store = Store(config["data_dir"])
    originals = {}
    try:
        with store.db:
            for number in range(count):
                path = root / f"part{number}.md"
                body = f"persistmarker{number} 背景资料。\n\n# 第二节\n保持完整的后续证据。"
                path.write_text(body, encoding="utf-8")
                stat = path.stat()
                doc_id = store.db.execute(
                    "INSERT INTO documents(key,source_id,path,name,extension,size,mtime_ns,status,seen) "
                    "VALUES(?,'files',?,?,'.md',?,?,?,'old-generation')",
                    ("file:" + str(path), str(path), path.name, stat.st_size, stat.st_mtime_ns,
                     "partial" if number == count - 1 else "ready"),
                ).lastrowid
                from data_search.product import file_identity
                store.db.execute('UPDATE documents SET file_identity=? WHERE id=?',(file_identity(stat),doc_id))
                store.db.execute("INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)",
                                 (doc_id, body, text_hash(body), json.dumps({"line_start": 1})))
                originals[doc_id] = (path, stat.st_size, stat.st_mtime_ns)
    finally:
        store.close()
    # Exercise opening the actual older document schema, not only a zero flag.
    with sqlite3.connect(Path(config["data_dir"]) / "index.sqlite3") as connection:
        connection.execute("ALTER TABLE documents DROP COLUMN chunking_version")
    return config, originals


def parser(engine, calls):
    def extract(request, *_args, **_kwargs):
        path = Path(request["path"])
        calls.append(path)
        return {"status": "ready", "chunks": [
            {"text": path.read_text(encoding="utf-8"), "locator": {"line_start": 1}}]}
    engine.parser.request = extract


def test_unchanged_legacy_files_migrate_in_bounded_pages_and_resume(tmp_path):
    config, originals = legacy_files(tmp_path)
    ids = sorted(originals)
    calls = []
    engine = Engine(config)
    parser(engine, calls)
    try:
        assert all(row["chunking_version"] == 0 for row in engine.store.rows("SELECT chunking_version FROM documents"))
        assert all(engine.search(f"persistmarker{i}", "keyword")["results"] for i in range(5))
        engine.catalog.migrate_chunks()
        state = json.loads(engine.store.setting("file_chunking_migration"))
        assert state == {"after": ids[1], "ceiling": ids[-1], "done": False}
        assert [row["doc_id"] for row in engine.store.rows("SELECT doc_id FROM file_work ORDER BY doc_id")] == ids[:2]
        assert len(engine.store.rows("SELECT id FROM documents WHERE status='pending'")) == 2
        # Queuing an old document never removes its existing searchable text.
        assert all(engine.search(f"persistmarker{i}", "keyword")["results"] for i in range(5))
        assert engine.catalog.parse() == 1
        first_chunks = engine.store.rows("SELECT id,locator FROM chunks WHERE doc_id=? ORDER BY id", (ids[0],))
        assert len(first_chunks) == 2 and all("source_spans" in json.loads(row["locator"]) for row in first_chunks)
        assert calls == [originals[ids[0]][0]]
    finally:
        engine.close()

    engine = Engine(config)
    parser(engine, calls)
    try:
        assert json.loads(engine.store.setting("file_chunking_migration")) == state
        assert [row["doc_id"] for row in engine.store.rows("SELECT doc_id FROM file_work")] == [ids[1]]
        engine.catalog.migrate_chunks()
        assert json.loads(engine.store.setting("file_chunking_migration"))["after"] == ids[3]
        assert len(engine.store.rows("SELECT doc_id FROM file_work")) == 3
        for _ in range(10):
            result = engine.scan_once(full=False)
            assert result["last_error"] is None, result
            if not engine._has_work():
                break
        else:
            pytest.fail("Legacy file chunk migration did not finish")
        assert len(calls) == 5 and len(set(calls)) == 5
        assert engine.store.rows("SELECT id,locator FROM chunks WHERE doc_id=? ORDER BY id", (ids[0],)) == first_chunks
        assert all(row["chunking_version"] == 3 for row in engine.store.rows("SELECT chunking_version FROM documents"))
        assert json.loads(engine.store.setting("file_chunking_migration"))["done"]
        for number, doc_id in enumerate(ids):
            path, size, mtime = originals[doc_id]
            assert (path.stat().st_size, path.stat().st_mtime_ns) == (size, mtime)
            assert engine.search(f"persistmarker{number}", "keyword")["results"]
        chunks = engine.store.rows("SELECT id,doc_id,text,locator FROM chunks ORDER BY id")
        engine.scan_once(full=True)
        assert len(calls) == 5
        assert engine.store.rows("SELECT id,doc_id,text,locator FROM chunks ORDER BY id") == chunks
    finally:
        engine.close()


def test_file_migration_preserves_old_text_during_parse_and_failed_retry(tmp_path):
    config, originals = legacy_files(tmp_path, count=1)
    engine = Engine(config)
    observed = []
    try:
        original = engine.store.rows("SELECT id,text,locator FROM chunks")
        engine.catalog.migrate_chunks()
        def failing_parser(*_args, **_kwargs):
            observed.append(bool(engine.search("persistmarker0", "keyword")["results"]))
            raise OSError("temporary parser failure")
        engine.parser.request = failing_parser
        engine.catalog.parse()
        assert observed == [True], "The old chunk must remain readable while its replacement is parsed"
        assert engine.search("persistmarker0", "keyword")["results"]
        assert engine.store.rows("SELECT id,text,locator FROM chunks") == original
        assert engine.store.rows("SELECT chunking_version FROM documents")[0]["chunking_version"] == 0
        with engine.store.db:
            engine.store.db.execute("UPDATE file_work SET available_at=0")
        calls = []
        parser(engine, calls)
        engine.catalog.parse()
        assert calls == [next(iter(originals.values()))[0]]
        assert engine.store.rows("SELECT chunking_version FROM documents")[0]["chunking_version"] == 3
        assert engine.search("persistmarker0", "keyword")["results"]
    finally:
        engine.close()


def database_state(engine):
    return json.loads(engine.store.setting("database_sync:legacy-db", "{}"))


def complete_database_cycle(engine):
    for _ in range(15):
        engine._database_scan(force=True)
        state = database_state(engine)
        assert not state.get("last_error"), state
        if state["tables"]["records"]["phase"] == "idle":
            return state
    pytest.fail("Bounded database cycle failed to complete")


def test_legacy_database_watermark_rechunks_unchanged_rows_once_across_restart(tmp_path):
    _, config = configuration(tmp_path)
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, body TEXT, updated INTEGER NOT NULL)")
        connection.executemany("INSERT INTO records VALUES(?,?,?)", [
            (number, f"dbpersistmarker{number} 背景。\n\n# 第二节\n仍需迁移的旧正文。", 10 + number)
            for number in range(1, 6)])
    config["databases"] = [{"id": "legacy-db", "kind": "sqlite", "path": str(source),
        "allowed_tables": ["records"], "allowed_columns": {"records": ["id", "body", "updated"]},
        "index": [{"table": "records", "id_column": "id", "text_columns": ["body"], "updated_column": "updated"}],
        "sync": {"page_size": 2, "max_pages_per_tick": 1, "reconcile_interval_seconds": 3600}}]
    engine = Engine(config)
    try:
        state = complete_database_cycle(engine)
        assert state["tables"]["records"]["watermark"] == [15, 5]
        versions = engine.store.rows("SELECT id,version FROM documents ORDER BY id")
        # Retain real source versions and a completed high watermark, but replace
        # the cache with pre-v0.3 unsplit chunks and no migration marker.
        with engine.store.db:
            for document in engine.store.rows("SELECT id,locator FROM documents ORDER BY id"):
                chunks = engine.store.rows("SELECT text FROM chunks WHERE doc_id=? ORDER BY id", (document["id"],))
                body = "".join(row["text"] for row in chunks)
                engine.store.clear_chunks(document["id"])
                engine.store.db.execute("INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)",
                    (document["id"], body, text_hash(body), document["locator"]))
            engine.store.db.execute("UPDATE documents SET chunking_version=0")
        state.pop("chunking_version")
        state["next_poll_at"] = time.time() + 3600
        engine.store.set_setting("database_sync:legacy-db", json.dumps(state))
        old_generation = state["tables"]["records"]["generation"]
    finally:
        engine.close()

    engine = Engine(config)
    try:
        assert all(engine.search(f"dbpersistmarker{i}", "keyword", source_id="legacy-db")["results"] for i in range(1, 6))
        # Migration must bypass the old next-poll delay and incremental watermark.
        engine._database_scan(deadline=time.monotonic() + 5)
        state = database_state(engine)
        table = state["tables"]["records"]
        assert state["chunking_version"] == 3 and table["mode"] == "full"
        assert table["generation"] != old_generation and table["cursor"] == 2
        assert len(engine.store.rows("SELECT id FROM documents WHERE chunking_version=3")) == 2
        first_chunks = engine.store.rows("SELECT id,doc_id,locator FROM chunks WHERE doc_id<=2 ORDER BY id")
        assert all("source_spans" in json.loads(row["locator"]) for row in first_chunks)
        assert engine.search("dbpersistmarker5", "keyword", source_id="legacy-db")["results"]
    finally:
        engine.close()

    engine = Engine(config)
    try:
        engine._database_scan(deadline=time.monotonic() + 5)
        resumed = database_state(engine)["tables"]["records"]
        assert resumed["generation"] == table["generation"] and resumed["cursor"] == 4
        assert engine.store.rows("SELECT id,doc_id,locator FROM chunks WHERE doc_id<=2 ORDER BY id") == first_chunks
        assert engine.search("dbpersistmarker5", "keyword", source_id="legacy-db")["results"]
        complete_database_cycle(engine)
        assert engine.store.rows("SELECT id,version FROM documents ORDER BY id") == versions
        assert all(row["chunking_version"] == 3 for row in engine.store.rows("SELECT chunking_version FROM documents"))
        migrated = engine.store.rows("SELECT id,doc_id,text,locator FROM chunks ORDER BY id")
        subsequent = complete_database_cycle(engine)
        assert subsequent["tables"]["records"]["mode"] == "incremental"
        assert subsequent["tables"]["records"]["generation"] == table["generation"]
        assert engine.store.rows("SELECT id,doc_id,text,locator FROM chunks ORDER BY id") == migrated
    finally:
        engine.close()
