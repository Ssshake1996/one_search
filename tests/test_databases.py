import json
import os
from pathlib import Path
import sqlite3

import pytest

from data_search.databases import DatabaseError, DatabaseSource


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test data.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, secret TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, description TEXT,
                amount NUMERIC, updated_at TEXT, secret TEXT);
            CREATE TABLE forbidden (id INTEGER PRIMARY KEY, password TEXT);
            INSERT INTO customers VALUES (1, 'Alice', 'customer-secret'), (2, '李雷', 'private');
            INSERT INTO orders VALUES
                (1, 1, '服务器成本优化', 120, '2026-01-01', 'hidden-one'),
                (2, 1, '100% literal_under_score!', 40, '2026-01-02', 'hidden-two'),
                (3, 2, 'storage migration', 80, '2026-01-03', 'hidden-three'),
                (4, 2, NULL, 10, '2026-01-04', 'hidden-four');
            INSERT INTO forbidden VALUES (1, 'not-for-search');
            CREATE VIEW public_orders AS SELECT id, description FROM orders;
        """)
    return path


@pytest.fixture
def config(database):
    return {"id": "test-db", "kind": "sqlite", "path": str(database),
            "allowed_tables": ["customers", "orders", "public_orders"],
            "allowed_columns": {"customers": ["id", "name"],
                                "orders": ["id", "customer_id", "description", "amount", "updated_at"]},
            "index": [{"table": "orders", "id_column": "id", "text_columns": ["description"],
                       "updated_column": "updated_at"}], "max_rows": 10}


def test_inspection_only_exposes_authorized_schema(config):
    result = DatabaseSource(config).inspect()
    assert result["read_only"]
    assert result["version"] == sqlite3.sqlite_version
    assert {t["table"] for t in result["tables"]} == {"customers", "orders", "public_orders"}
    assert "secret" not in json.dumps(result)
    assert next(t for t in result["tables"] if t["table"] == "public_orders")["kind"] == "view"


def test_filter_sort_pagination_and_bound_parameters(config):
    source = DatabaseSource(config)
    result = source.query({"table": "orders", "columns": ["id", "amount"],
        "filters": [{"column": "amount", "op": "gte", "value": 40}],
        "order_by": [{"column": "amount", "direction": "desc"}], "limit": 1, "offset": 1})
    assert result["rows"] == [{"id": 3, "amount": 80}]
    assert result["truncated"] is True
    malicious = "' OR 1=1; DROP TABLE orders; --"
    assert source.query({"table": "orders", "filters": [{"column": "description", "op": "eq", "value": malicious}]})["rows"] == []
    assert len(source.query({"table": "orders"})["rows"]) == 4


def test_join_aggregate_and_group(config):
    result = DatabaseSource(config).query({"table": "customers", "columns": ["name"],
        "joins": [{"table": "orders", "left": "customers.id", "right": "orders.customer_id"}],
        "aggregates": [{"function": "sum", "column": "orders.amount", "alias": "total"},
                       {"function": "count", "column": "*", "alias": "number"}],
        "group_by": ["name"], "order_by": [{"column": "total", "direction": "desc"}]})
    assert result["rows"] == [{"name": "Alice", "total": 160, "number": 2}, {"name": "李雷", "total": 90, "number": 2}]


@pytest.mark.parametrize("query_request", [
    {"table": "forbidden"},
    {"table": "orders", "columns": ["secret"]},
    {"table": "orders", "filters": [{"column": "secret", "op": "eq", "value": "x"}]},
    {"table": "orders", "order_by": [{"column": "secret"}]},
    {"table": "orders", "group_by": ["secret"]},
    {"table": "orders", "aggregates": [{"function": "min", "column": "secret", "alias": "x"}]},
    {"table": "orders", "joins": [{"table": "customers", "left": "orders.secret", "right": "customers.id"}]},
    {"table": "orders", "joins": [{"table": "forbidden", "left": "id", "right": "forbidden.id"}]},
    {"table": "orders", "columns": ["id; DROP TABLE orders"]},
    {"table": "orders", "where": "1=1"},
    {"sql": "SELECT * FROM orders"},
    {"table": "orders", "limit": 1000000},
    {"table": "orders", "offset": -1},
    {"table": "orders", "limit": True},
    {"table": "orders", "filters": [{"column": "id", "op": "custom", "value": 1}]},
    {"table": "orders", "order_by": [{"column": "id", "direction": "asc; DROP TABLE orders"}]},
    {"table": "orders", "columns": ["id"], "aggregates": [{"function": "count", "column": "*", "alias": "n"}]},
])
def test_rejects_disallowed_queries(config, query_request):
    with pytest.raises(DatabaseError):
        DatabaseSource(config).query(query_request)


@pytest.mark.parametrize("operator,value,expected", [
    ("contains", "%", [2]), ("contains", "_", [2]), ("contains", "!", [2]),
    ("starts_with", "服务器", [1]), ("eq", None, [4]), ("is_null", None, [4]),
    ("not_null", None, [1, 2, 3]), ("in", ["storage migration"], [3]),
    ("not_in", ["storage migration"], [1, 2]),
])
def test_filter_semantics(config, operator, value, expected):
    rows = DatabaseSource(config).query({"table": "orders", "columns": ["id"],
        "filters": [{"column": "description", "op": operator, "value": value}],
        "order_by": [{"column": "id"}]})["rows"]
    assert [r["id"] for r in rows] == expected


def test_connection_really_is_read_only(config):
    source = DatabaseSource(config)
    with source._connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM orders")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE new_table (id int)")


def test_empty_allowlist_does_not_discover_other_tables(config):
    config["allowed_tables"], config["allowed_columns"], config["index"] = [], {}, []
    assert DatabaseSource(config).inspect()["tables"] == []
    snapshot = list(DatabaseSource(config).iter_documents())
    assert snapshot == [{"kind": "snapshot", "complete": True, "tables": [], "row_count": 0, "reason": None}]


def test_snapshot_versions_keys_and_deletion(config, database):
    source = DatabaseSource(config)
    first = list(source.iter_documents())
    assert first[-1]["complete"] is True and first[-1]["row_count"] == 4
    assert first[0]["locator"]["id"] == 1
    assert "服务器成本优化" in first[0]["text"]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE orders SET description='服务器费用降低' WHERE id=1")
        connection.execute("DELETE FROM orders WHERE id=2")
    second = list(source.iter_documents())
    assert first[0]["key"] == second[0]["key"]
    assert first[0]["version"] != second[0]["version"]
    assert second[-1]["complete"] is True and second[-1]["row_count"] == 3
    assert first[1]["key"] not in {r.get("key") for r in second}


def test_bounded_snapshot_never_claims_completeness(config):
    rows = list(DatabaseSource(config).iter_documents(max_rows=2))
    assert len(rows) == 3
    assert rows[-1] == {"kind": "snapshot", "complete": False, "tables": ["orders"], "row_count": 2, "reason": "row_limit"}
    assert list(DatabaseSource(config).iter_documents(max_rows=4))[-1]["complete"] is True


def test_index_failures_mark_snapshot_incomplete(config):
    config["index"][0]["text_columns"] = ["secret"]
    rows = list(DatabaseSource(config).iter_documents())
    assert rows[-1]["complete"] is False
    assert rows[-1]["row_count"] == 0


def test_duplicate_and_null_keys_fail_snapshot(config, database):
    config["index"][0]["id_column"] = "customer_id"
    rows = list(DatabaseSource(config).iter_documents())
    assert rows[-1]["complete"] is False
    assert "not unique" in rows[-1]["reason"]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE orders SET customer_id=NULL WHERE id=1")
    rows = list(DatabaseSource(config).iter_documents())
    assert rows[-1]["complete"] is False
    assert "non-null" in rows[-1]["reason"]


def test_result_and_index_text_limits(config, database):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE orders SET description=? WHERE id=1", ("a" * 20000,))
    config["max_result_chars"] = 100
    source = DatabaseSource(config)
    result = source.query({"table": "orders", "columns": ["description"], "order_by": [{"column": "id"}]})
    assert result["truncated"] and not result["rows"]
    docs = list(source.iter_documents())
    assert len(docs[0]["text"]) == 12000
    assert docs[0]["locator"]["truncated"] is True


def test_timeout_interrupts_expensive_query(config, database):
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE large (id INTEGER)")
        connection.executemany("INSERT INTO large VALUES (?)", [(n,) for n in range(20000)])
        connection.execute("CREATE VIEW slow AS SELECT sum(a.id*b.id) AS result FROM large a, large b")
    config["allowed_tables"].append("slow")
    config["query_timeout_seconds"] = 0.01
    with pytest.raises(DatabaseError, match="timeout"):
        DatabaseSource(config).query({"table": "slow"})


def test_errors_do_not_expose_driver_messages(config, monkeypatch):
    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("password=SUPERSECRET host=private.internal secret SQL")
    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    with pytest.raises(DatabaseError) as error:
        DatabaseSource(config).inspect()
    assert "SUPERSECRET" not in str(error.value)
    assert "private.internal" not in str(error.value)


def test_hostile_but_allowed_identifier_is_quoted(tmp_path):
    path = tmp_path / "weird.db"
    table = 'orders"; DROP TABLE orders; --'
    quoted = '"' + table.replace('"', '""') + '"'
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {quoted} (id INTEGER)")
        connection.execute(f"INSERT INTO {quoted} VALUES (9)")
    source = DatabaseSource({"id": "weird", "kind": "sqlite", "path": str(path), "allowed_tables": [table]})
    assert source.query({"table": table})["rows"] == [{"id": 9}]


def test_plaintext_password_rejected(config):
    config["password"] = "do-not-leak"
    with pytest.raises(DatabaseError, match="password_env"):
        DatabaseSource(config)


@pytest.mark.parametrize("engine", ["mysql", "postgres"])
def test_service_database_integration(engine):
    """Optional isolated engine fixtures: never connects to a business database by default."""
    config_path = os.environ.get("DATA_SEARCH_TEST_" + engine.upper() + "_CONFIG")
    if not config_path:
        pytest.skip("Isolated engine fixture not configured")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    source = DatabaseSource(config)
    prefix = "public." if engine == "postgres" else ""
    customers, orders = prefix + "customers", prefix + "orders"
    inspected = source.inspect()
    assert len(inspected["tables"]) == 2
    assert "secret" not in json.dumps(inspected)
    result = source.query({"table": customers, "columns": ["name"],
        "joins": [{"table": orders, "left": customers + ".id", "right": orders + ".customer_id"}],
        "aggregates": [{"function": "sum", "column": orders + ".amount", "alias": "total"}],
        "group_by": ["name"], "order_by": [{"column": "total", "direction": "desc"}]})
    assert [r["name"] for r in result["rows"]] == ["Alice", "李雷"]
    assert float(result["rows"][0]["total"]) == 160
    for needle in ["%", "_", "!"]:
        result = source.query({"table": orders, "columns": ["id"],
            "filters": [{"column": "description", "op": "contains", "value": needle}]})
        assert result["rows"] == [{"id": 2}]
    result = source.query({"table": orders, "columns": ["id"], "order_by": [{"column": "id"}], "limit": 2, "offset": 1})
    assert result["rows"] == [{"id": 2}, {"id": 3}] and result["truncated"]
    with pytest.raises(DatabaseError):
        source.query({"table": orders, "columns": ["secret"]})
    with pytest.raises(DatabaseError):
        with source._connection() as connection:
            with source._cursor(connection) as cursor:
                cursor.execute("DELETE FROM " + source._table(orders))
    docs = list(source.iter_documents())
    assert docs[-1]["complete"] and docs[-1]["row_count"] == 4
    assert "服务器成本优化" in docs[0]["text"]
    assert list(source.iter_documents(max_rows=2))[-1]["complete"] is False
    for mode in ['full'] + (['incremental'] if config['index'][0].get('updated_column') else []):
        after, boundary, identities = None, None, []
        for _ in range(5):
            page = source.index_page(config['index'][0], mode=mode, after=after, boundary=boundary, page_size=1)
            identities += [document['locator']['id'] for document in page['documents']]
            after, boundary = page['next_cursor'], page['boundary']
            if page['complete']:
                break
        assert page['complete'] and identities == [1, 2, 3, 4]


@pytest.mark.parametrize("engine", ["mysql", "postgres"])
def test_service_database_onboarding_and_os_credential_rotation(engine):
    from data_search import credentials, preflight, source_setup
    config_path = os.environ.get("DATA_SEARCH_TEST_" + engine.upper() + "_CONFIG")
    if not config_path:
        pytest.skip("Set explicit isolated test database config to run service onboarding")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    table = "public.orders" if engine == "postgres" else "orders"
    report = source_setup.discover_source(config)
    assert report["ok"] and report["metadata_only"] and "hidden" not in json.dumps(report)
    entry = next(item for item in report["tables"] if item["table"] == table)
    assert entry["index_recommendation"]["id_column"] == "id"
    assert "updated_at" in entry["index_recommendation"]["watermark_candidates"]
    proposal = source_setup.propose_source(config, [{"table": table,
        "columns": ["id", "description", "amount", "updated_at"], "index_text_columns": ["description"],
        "updated_column": "updated_at"}], {"alias": "订单库", "tables": {table: {"alias": "订单"}}})
    assert proposal["ok"] and preflight.check_database(proposal["source"])["ok"]
    assert proposal["source"]["allowed_columns"][table] == ["id", "description", "amount", "updated_at"]
    assert DatabaseSource(proposal["source"]).inspect()["tables"][0]["business_metadata"]["alias"] == "订单"
    if os.name == "nt":
        password = os.environ[config["password_env"]]
        reference = credentials.store_credential(password)
        try:
            candidate = {**proposal["source"], "credential_ref": reference}
            candidate.pop("password_env", None)
            assert preflight.check_database(candidate)["ok"]
            assert len(DatabaseSource(candidate).query({"table": table, "columns": ["id"]})["rows"]) == 4
            credentials.store_credential("known-invalid-synthetic-password", reference)
            failed = preflight.check_database(candidate)
            assert not failed["ok"] and failed["checks"][-1]["code"] == "authentication_failed"
            credentials.store_credential(password, reference)
            assert preflight.check_database(candidate)["ok"]
            assert password not in json.dumps(failed)
        finally:
            credentials.delete_credential(reference)



def test_keyset_pages_cover_ties_without_offset(config, database):
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE orders SET updated_at='2026-01-01'")
    source = DatabaseSource(config)
    for mode in ('full', 'incremental'):
        after, boundary, ids = None, None, []
        for _ in range(5):
            page = source.index_page(config['index'][0], mode=mode, after=after, boundary=boundary, page_size=1)
            ids += [d['locator']['id'] for d in page['documents']]
            after, boundary = page['next_cursor'], page['boundary']
            if page['complete']:
                break
        assert ids == [1, 2, 3, 4]
        assert page['complete']
        assert boundary['watermark'] == ['2026-01-01', 4]


def test_keyset_boundary_finishes_despite_new_rows(config, database):
    source = DatabaseSource(config)
    first = source.index_page(config['index'][0], page_size=2)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO orders(id,description,updated_at) VALUES(5,'later','2026-02-01')")
    second = source.index_page(config['index'][0], after=first['next_cursor'], boundary=first['boundary'], page_size=2)
    assert [d['locator']['id'] for d in second['documents']] == [3, 4]
    assert second['complete']
    incremental = source.index_page(config['index'][0], mode='incremental', watermark=first['boundary']['watermark'])
    assert [d['locator']['id'] for d in incremental['documents']] == [4, 5]


@pytest.mark.parametrize('change,error', [
    ('nonunique', 'single-column'), ('composite', 'single-column'),
    ('partial', 'single-column'), ('null_id', 'non-null'),
    ('null_watermark', 'non-null'), ('blob_watermark', 'ordered scalar'),
    ('view', 'single-column'), ('hidden_watermark', 'not allowed'),
])
def test_index_page_rejects_unreliable_keys_and_watermarks(config, database, change, error):
    entry = config['index'][0]
    with sqlite3.connect(database) as connection:
        if change in {'nonunique', 'composite', 'partial'}:
            entry['id_column'] = 'customer_id'
            if change == 'composite':
                connection.execute('CREATE UNIQUE INDEX composite_key ON orders(customer_id, id)')
            if change == 'partial':
                connection.execute('CREATE UNIQUE INDEX partial_key ON orders(customer_id) WHERE id=1')
        elif change == 'null_id':
            connection.execute('UPDATE orders SET customer_id=id')
            connection.execute('CREATE UNIQUE INDEX nullable_key ON orders(customer_id)')
            connection.execute('UPDATE orders SET customer_id=NULL WHERE id=1')
            entry['id_column'] = 'customer_id'
        elif change == 'null_watermark':
            connection.execute('UPDATE orders SET updated_at=NULL WHERE id=1')
        elif change == 'blob_watermark':
            connection.execute("UPDATE orders SET updated_at=x'1020' WHERE id=4")
        elif change == 'view':
            entry.update(table='public_orders')
            entry.pop('updated_column')
        elif change == 'hidden_watermark':
            config['allowed_columns']['orders'].remove('updated_at')
    with pytest.raises(DatabaseError, match=error):
        DatabaseSource(config).index_page(entry)


def test_single_column_unique_key_and_quoted_values_are_bound(config, database):
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE names (name TEXT NOT NULL UNIQUE, body TEXT)")
        connection.executemany('INSERT INTO names VALUES(?,?)', [("a' OR 1=1 --", 'first'), ('z', 'last')])
    config['allowed_tables'].append('names')
    entry = {'table': 'names', 'id_column': 'name', 'text_columns': ['body']}
    config['index'] = [entry]
    source = DatabaseSource(config)
    first = source.index_page(entry, page_size=1)
    second = source.index_page(entry, page_size=1, after=first['next_cursor'], boundary=first['boundary'])
    assert first['documents'][0]['locator']['id'] == "a' OR 1=1 --"
    assert [d['locator']['id'] for d in second['documents']] == ['z']
    assert second['complete']


@pytest.mark.parametrize('arguments', [{'page_size': True}, {'page_size': 1001},
    {'mode': 'arbitrary'}, {'after': {}}, {'mode':'incremental','after':['x']},
    {'watermark': [None, 1]}, {'watermark': [float('inf'), 1]}, {'boundary': {'id':1}}])
def test_index_page_rejects_invalid_cursors(config, arguments):
    with pytest.raises(DatabaseError):
        DatabaseSource(config).index_page(config['index'][0], **arguments)
