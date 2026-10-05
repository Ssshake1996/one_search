"""Check benchmark transaction accounting against real SQLite boundaries."""
import importlib.util
import sqlite3
from pathlib import Path


def test_indexing_benchmark_counts_outer_release_and_commit_separately():
    spec = importlib.util.spec_from_file_location("bench_indexing", Path(__file__).parents[1] / "scripts" / "bench_indexing.py")
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    connection = sqlite3.connect(":memory:")
    counter = benchmark.Transactions()
    connection.set_trace_callback(counter)
    try:
        connection.execute("CREATE TABLE data(value)")
        with connection:
            connection.execute("INSERT INTO data VALUES (1)")
            connection.execute("SAVEPOINT inner")
            connection.execute("INSERT INTO data VALUES (2)")
            connection.execute("RELEASE inner")
        connection.execute("SAVEPOINT outer")
        connection.execute("INSERT INTO data VALUES (3)")
        connection.execute("SAVEPOINT inner")
        connection.execute("INSERT INTO data VALUES (4)")
        connection.execute("ROLLBACK TO inner")
        connection.execute("RELEASE inner")
        connection.execute("RELEASE outer")
        assert connection.execute("SELECT value FROM data").fetchall() == [(1,), (2,), (3,)]
        assert counter.report() == {"begin": 1, "savepoint": 3, "release": 3,
                                    "commit": 1, "rollback_to": 1,
                                    "outer_savepoint_release": 1, "durable_transactions": 2}
        assert not connection.in_transaction
        # SQLite resolves duplicate savepoint names from the newest outward.
        connection.execute("SAVEPOINT repeated")
        connection.execute("INSERT INTO data VALUES (5)")
        connection.execute("SAVEPOINT repeated")
        connection.execute("INSERT INTO data VALUES (6)")
        connection.execute("ROLLBACK TO repeated")
        connection.execute("RELEASE repeated")
        assert connection.in_transaction
        assert counter.report()["durable_transactions"] == 2
        connection.execute("RELEASE repeated")
        assert not connection.in_transaction
        assert counter.report()["durable_transactions"] == 3
        assert connection.execute("SELECT value FROM data").fetchall() == [(1,), (2,), (3,), (5,)]
    finally:
        connection.close()
