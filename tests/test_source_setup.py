from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from data_search.databases import DatabaseError, DatabaseSource, _operation_error
from data_search import preflight, source_setup


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "业务 schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE orders(id INTEGER PRIMARY KEY, body TEXT, amount DECIMAL,
                updated_at TEXT NOT NULL, secret TEXT);
            INSERT INTO orders VALUES(1, 'DO NOT RETURN THIS BODY', 12.3, '2026-01-01', 'DO NOT RETURN THIS PASSWORD');
            CREATE TABLE no_key(id INTEGER, body TEXT);
            CREATE TABLE compound(a INTEGER, b INTEGER, body TEXT, PRIMARY KEY(a,b));
            CREATE TABLE nullable(id TEXT UNIQUE, body TEXT);
            CREATE TABLE partial(id INTEGER, body TEXT);
            CREATE UNIQUE INDEX partial_id ON partial(id) WHERE id > 0;
            CREATE VIEW read_view AS SELECT body FROM orders;
        """)
    return {"id": "business", "kind": "sqlite", "path": str(path), "allowed_tables": ["orders"],
        "allowed_columns": {"orders": ["id", "body"]}}


def test_discovery_only_catalog_and_does_not_grant_or_return_data(source):
    original = deepcopy(source)
    before = Path(source["path"]).read_bytes()
    report = source_setup.discover_source(source)
    assert report["ok"] and report["metadata_only"] and not report["authorization_changed"]
    assert "DO NOT RETURN" not in json.dumps(report)
    assert "password_env" not in json.dumps(report) and source == original
    assert Path(source["path"]).read_bytes() == before
    tables = {table["table"]: table for table in report["tables"]}
    assert len(tables) == 6 and tables["orders"]["index_recommendation"]["id_column"] == "id"
    assert tables["orders"]["index_recommendation"]["watermark_candidates"] == ["updated_at"]
    for name in ("compound", "no_key", "partial", "read_view"):
        assert tables[name]["index_recommendation"]["mode"] == "realtime_only"
    assert tables["nullable"]["index_recommendation"]["key_candidates"][0]["requires_null_check"]
    assert tables["nullable"]["index_recommendation"]["id_column"] is None
    # Explicit local discovery can show metadata beyond the saved allowlist,
    # while MCP's underlying normal inspect remains strictly allowlisted.
    visible = DatabaseSource(source).inspect()
    assert len(visible["tables"]) == 1
    assert {field["name"] for field in visible["tables"][0]["columns"]} == {"id", "body"}


def test_discovery_bounds_and_missing_source_diagnosis(source):
    report = source_setup.discover_source(source, max_tables=2)
    assert report["ok"] and report["truncated"] and len(report["tables"]) == 2
    source["path"] += ".missing"
    report = source_setup.discover_source(source)
    assert not report["ok"] and report["diagnostics"][0]["code"] == "source_missing"
    assert source["path"] not in json.dumps(report)


def test_discovery_deadline_keeps_authorization_unchanged(monkeypatch):
    monkeypatch.setattr(source_setup, "process_command", lambda *_: [sys.executable, "-c", "import time; time.sleep(10)"])
    started = time.monotonic()
    report = source_setup.discover_source({"id": "slow"}, timeout_seconds=.1)
    assert not report["ok"] and report["diagnostics"][0]["code"] == "discovery_timeout"
    assert not report["authorization_changed"] and time.monotonic() - started < 3


def test_proposal_uses_only_selected_fields_and_explicit_watermark(source):
    proposal = source_setup.propose_source(source, [{"table": "orders", "columns": ["id", "body", "updated_at"],
        "index_text_columns": ["body"]}])
    assert proposal["ok"] and proposal["requires_preflight"] and not proposal["authorization_changed"]
    candidate = proposal["source"]
    assert candidate["allowed_columns"] == {"orders": ["id", "body", "updated_at"]}
    assert candidate["index"] == [{"table": "orders", "id_column": "id", "text_columns": ["body"]}]
    assert proposal["notes"][0]["sync"] == "periodic_full_scan"
    assert preflight.check_database(candidate)["ok"]
    assert source["allowed_columns"]["orders"] == ["id", "body"]


def test_no_unique_key_does_not_prevent_realtime_use(source):
    proposal = source_setup.propose_source(source, [{"table": "no_key", "columns": ["id", "body"], "index_text_columns": ["body"]}])
    assert proposal["ok"] and proposal["source"]["index"] == []
    assert proposal["notes"][0]["mode"] == "realtime_only"
    assert preflight.check_database(proposal["source"])["ok"]


@pytest.mark.parametrize("selection", [
    {"table": "orders", "columns": ["missing"]},
    {"table": "orders", "columns": ["id", "body"], "index_text_columns": ["secret"]},
    {"table": "orders", "columns": ["id", "body"], "index_text_columns": ["body"], "updated_column": "secret"},
    {"table": "orders", "columns": ["id", "body"], "index_text_columns": ["body"], "id_column": "body"},
    {"table": "orders; DROP TABLE orders", "columns": ["id"]},
])
def test_invalid_selection_never_expands_or_changes_saved_scope(source, selection):
    original = deepcopy(source)
    with pytest.raises(DatabaseError):
        source_setup.propose_source(source, [selection])
    assert source == original


def test_business_labels_only_enrich_authorized_real_identifiers(source):
    source["business_metadata"] = {"alias": "订单库", "description": "仅测试", "tables": {
        "orders": {"alias": "订单", "columns": {"body": {"alias": "订单描述", "description": "普通用户业务说明"}}}}}
    result = DatabaseSource(source).inspect()
    assert result["business_metadata"]["alias"] == "订单库"
    table = result["tables"][0]
    assert table["table"] == "orders" and table["business_metadata"]["alias"] == "订单"
    assert table["columns"][1]["business_metadata"]["alias"] == "订单描述"
    assert "not instructions" in result["metadata_note"]
    with pytest.raises(DatabaseError, match="not allowed"):
        DatabaseSource(source).query({"table": "订单", "columns": ["订单描述"]})
    source["business_metadata"]["tables"]["orders"]["columns"]["secret"] = {"alias": "forbidden"}
    with pytest.raises(DatabaseError, match="allowed columns"):
        DatabaseSource(source)


def test_removing_allowed_columns_prunes_old_business_labels(source):
    source["business_metadata"] = {"tables": {"orders": {"columns": {"body": {"alias": "旧说明"}}}}}
    proposal = source_setup.propose_source(source, [{"table": "orders", "columns": ["id"]}])
    assert proposal["source"]["business_metadata"]["tables"]["orders"]["columns"] == {}


@pytest.mark.parametrize("number,state,expected", [(1045, None, "authentication_failed"),
    (1142, None, "permission_denied"), (1054, None, "schema_changed"),
    (3024, None, "query_timeout"), (2003, None, "connection_unavailable"),
    (None, "28P01", "authentication_failed"), (None, "42501", "permission_denied"),
    (None, "42P01", "schema_changed"), (None, "57014", "query_timeout"), (None, "08006", "connection_unavailable")])
def test_driver_error_diagnosis_never_replays_sql_or_secrets(number, state, expected):
    error = Exception(number, "SECRET password=super-secret SELECT private FROM forbidden")
    error.sqlstate = state
    public = _operation_error(error)
    assert public.code == expected and public.action
    assert "secret" not in str(public).lower() and "private" not in public.action


def test_schema_change_preflight_has_actionable_safe_diagnosis(source):
    with sqlite3.connect(source["path"]) as connection:
        connection.execute("DROP VIEW read_view")
        connection.execute("DROP TABLE orders")
    report = preflight.check_database(source)
    assert not report["ok"] and report["checks"][-1]["code"] == "schema_changed"
    assert report["checks"][-1]["action"] and source["path"] not in json.dumps(report)
