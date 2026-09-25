"""Allowlisted, read-only database access. No caller-supplied SQL is accepted."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator


class DatabaseError(ValueError):
    """Public, deliberately credential-free database error."""


def _json_value(value):
    if isinstance(value, (datetime, date, datetime_time, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DatabaseError(f"{name} must be an integer between {low} and {high}")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise DatabaseError("Invalid identifier")
    return value


def _fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise DatabaseError("Invalid structured request fields")


class DatabaseSource:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.id = _identifier(config.get("id"))
        self.kind = config.get("kind")
        if self.kind not in {"sqlite", "mysql", "postgres"}:
            raise DatabaseError("Database kind must be sqlite, mysql or postgres")
        if "password" in config or "dsn" in config:
            raise DatabaseError("Use password_env; plaintext passwords and DSNs are not accepted")
        tables = config.get("allowed_tables", [])
        if not isinstance(tables, list) or len(tables) > 1000:
            raise DatabaseError("allowed_tables must be a list with at most 1000 tables")
        self.tables = list(dict.fromkeys(_identifier(t) for t in tables))
        for table in self.tables:
            if self.kind == "postgres" and (len(table.split(".")) != 2 or not all(table.split("."))):
                raise DatabaseError("PostgreSQL table names must use schema.table")
        allowed_columns = config.get("allowed_columns", {})
        if not isinstance(allowed_columns, dict) or set(allowed_columns) - set(self.tables):
            raise DatabaseError("allowed_columns must reference allowed tables")
        for columns in allowed_columns.values():
            if not isinstance(columns, list):
                raise DatabaseError("allowed_columns entries must be lists")
            for column in columns:
                _identifier(column)
        self.max_rows = _integer(config.get("max_rows", 200), "max_rows", 1, 10000)
        self.max_result_chars = _integer(config.get("max_result_chars", 200000), "max_result_chars", 100, 2000000)
        timeout = config.get("query_timeout_seconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.01 <= timeout <= 60:
            raise DatabaseError("query_timeout_seconds must be between 0.01 and 60")
        self.timeout = float(timeout)
        self._lock = threading.BoundedSemaphore(1)

    def _quote(self, identifier):
        quote = "`" if self.kind == "mysql" else '"'
        return quote + _identifier(identifier).replace(quote, quote * 2) + quote

    def _table(self, table):
        if table not in self.tables:
            raise DatabaseError("Table is not allowed")
        parts = table.split(".") if self.kind == "postgres" else [table]
        return ".".join(self._quote(p) for p in parts)

    @property
    def _param(self):
        return "?" if self.kind == "sqlite" else "%s"

    @contextmanager
    def _connection(self):
        if not self._lock.acquire(timeout=self.timeout):
            raise DatabaseError("Database source is busy")
        connection = None
        try:
            if self.kind == "sqlite":
                path = Path(self.config.get("path", "")).expanduser().resolve()
                if not path.is_file():
                    raise DatabaseError("SQLite database file does not exist")
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
                deadline = time.monotonic() + self.timeout
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                connection.execute("BEGIN")
            else:
                env_name = self.config.get("password_env")
                password = os.environ.get(env_name) if env_name else None
                if env_name and password is None:
                    raise DatabaseError("Configured password environment variable is missing")
                common = {"host": self.config.get("host", "127.0.0.1"),
                          "port": self.config.get("port", 3306 if self.kind == "mysql" else 5432),
                          "user": self.config.get("user"), "password": password,
                          "connect_timeout": max(1, math.ceil(self.timeout))}
                if not common["user"] or not self.config.get("database"):
                    raise DatabaseError("Database name and user are required")
                milliseconds = max(1, round(self.timeout * 1000))
                if self.kind == "mysql":
                    import pymysql
                    connection = pymysql.connect(**common, database=self.config["database"],
                        charset="utf8mb4", autocommit=False, read_timeout=max(1, math.ceil(self.timeout)),
                        write_timeout=max(1, math.ceil(self.timeout)), ssl=self.config.get("ssl"),
                        cursorclass=pymysql.cursors.SSCursor, local_infile=False)
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {milliseconds}")
                        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        cursor.execute("SET SESSION TRANSACTION READ ONLY")
                        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                else:
                    import psycopg
                    ssl = self.config.get("ssl", {}) or {}
                    if set(ssl) - {"sslmode", "sslrootcert", "sslcert", "sslkey"}:
                        raise DatabaseError("Unsupported PostgreSQL TLS options")
                    connection = psycopg.connect(**common, dbname=self.config["database"], **ssl,
                        options=f"-c default_transaction_read_only=on -c statement_timeout={milliseconds} -c lock_timeout={milliseconds}")
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield connection
        except DatabaseError:
            raise
        except ImportError:
            raise DatabaseError("Database driver is not installed") from None
        except Exception:
            # Driver errors can contain hostnames, SQL, data, usernames and credentials.
            raise DatabaseError("Database operation failed; check connectivity, permissions, schema and timeout") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._lock.release()

    @contextmanager
    def _cursor(self, connection, *, stream=False):
        cursor = connection.cursor(name="data_search_stream") if stream and self.kind == "postgres" else connection.cursor()
        if stream and self.kind == "postgres":
            cursor.itersize = 16
        try:
            yield cursor
        finally:
            cursor.close()

    def _schema(self, connection, table):
        self._table(table)
        with self._cursor(connection) as cursor:
            if self.kind == "sqlite":
                cursor.execute("SELECT type FROM sqlite_schema WHERE name = ? AND type IN ('table','view')", (table,))
                found = cursor.fetchone()
                if found is None:
                    raise DatabaseError("Configured table is unavailable")
                kind = found[0]
                cursor.execute("SELECT name, type, [notnull], pk FROM pragma_table_info(?)", (table,))
                columns = [{"name": row[0], "type": row[1], "nullable": not bool(row[2]), "primary_key": bool(row[3])} for row in cursor.fetchall()]
            elif self.kind == "mysql":
                cursor.execute("SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (self.config["database"], table))
                found = cursor.fetchone()
                cursor.fetchall()
                if found is None:
                    raise DatabaseError("Configured table is unavailable")
                kind = "view" if found[0] == "VIEW" else "table"
                cursor.execute("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (self.config["database"], table))
                columns = [{"name": r[0], "type": r[1], "nullable": r[2] == "YES", "primary_key": r[3] == "PRI", "comment": r[4]} for r in cursor.fetchall()]
            else:
                schema, name = table.split(".")
                cursor.execute("SELECT table_type FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (schema, name))
                found = cursor.fetchone()
                if found is None:
                    raise DatabaseError("Configured table is unavailable")
                kind = "view" if found[0] == "VIEW" else "table"
                cursor.execute("SELECT c.column_name, c.data_type, c.is_nullable, col_description(pc.oid, pa.attnum), EXISTS (SELECT 1 FROM pg_index pi WHERE pi.indrelid=pc.oid AND pi.indisprimary AND pa.attnum=ANY(pi.indkey)) FROM information_schema.columns c JOIN pg_namespace pn ON pn.nspname=c.table_schema JOIN pg_class pc ON pc.relnamespace=pn.oid AND pc.relname=c.table_name JOIN pg_attribute pa ON pa.attrelid=pc.oid AND pa.attname=c.column_name WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position", (schema, name))
                columns = [{"name": r[0], "type": r[1], "nullable": r[2] == "YES", "comment": r[3], "primary_key": r[4]} for r in cursor.fetchall()]
        allowed = self.config.get("allowed_columns", {}).get(table)
        if allowed is not None:
            columns = [column for column in columns if column["name"] in allowed]
        return {"table": table, "kind": kind, "columns": columns}

    def inspect(self):
        with self._connection() as connection:
            schemas = [self._schema(connection, table) for table in self.tables]
            with self._cursor(connection) as cursor:
                cursor.execute("SELECT sqlite_version()" if self.kind == "sqlite" else "SELECT version()")
                version = cursor.fetchone()[0]
                if self.kind == "mysql":
                    cursor.fetchall()
        return {"source_id": self.id, "kind": self.kind, "version": str(version), "read_only": True, "tables": schemas}

    def _compile(self, connection, request):
        _fields(request, {"table", "columns", "filters", "order_by", "limit", "offset", "joins", "aggregates", "group_by"}, {"table"})
        base = request["table"]
        self._table(base)
        joins = request.get("joins", [])
        if not isinstance(joins, list) or len(joins) > 4:
            raise DatabaseError("At most four joins are allowed")
        tables = [base]
        for join in joins:
            _fields(join, {"table", "left", "right", "type"}, {"table", "left", "right"})
            self._table(join["table"])
            if join["table"] in tables:
                raise DatabaseError("Repeated table joins are not supported")
            tables.append(join["table"])
        schemas = {t: {c["name"] for c in self._schema(connection, t)["columns"]} for t in tables}
        aliases = {t: f"t{i}" for i, t in enumerate(tables)}

        def column(reference):
            _identifier(reference)
            if reference in schemas[base]:
                table, name = base, reference
            else:
                matches = [(t, reference[len(t) + 1:]) for t in tables if reference.startswith(t + ".")]
                matches = [(t, n) for t, n in matches if n in schemas[t]]
                if len(matches) != 1:
                    raise DatabaseError("Column is unavailable or not allowed")
                table, name = matches[0]
            return aliases[table] + "." + self._quote(name), table

        selected = request.get("columns", sorted(schemas[base]) if not request.get("aggregates") else [])
        if not isinstance(selected, list) or len(selected) > 100:
            raise DatabaseError("columns must contain at most 100 names")
        labels, expressions = [], []
        for name in selected:
            expressions.append(column(name)[0] + " AS " + self._quote(name))
            labels.append(name)
        aggregates = request.get("aggregates", [])
        if not isinstance(aggregates, list) or len(aggregates) > 20:
            raise DatabaseError("At most 20 aggregates are allowed")
        aggregate_aliases = set()
        for aggregate in aggregates:
            _fields(aggregate, {"function", "column", "alias"}, {"function", "column", "alias"})
            function = aggregate["function"]
            if function not in {"count", "sum", "avg", "min", "max"}:
                raise DatabaseError("Unsupported aggregate function")
            alias = _identifier(aggregate["alias"])
            expression = "*" if function == "count" and aggregate["column"] == "*" else column(aggregate["column"])[0]
            expressions.append(function.upper() + "(" + expression + ") AS " + self._quote(alias))
            aggregate_aliases.add(alias)
            labels.append(alias)
        if not expressions or len(set(labels)) != len(labels):
            raise DatabaseError("Select at least one column; result names must be unique")
        sql = "SELECT " + ", ".join(expressions) + " FROM " + self._table(base) + " t0"
        for index, join in enumerate(joins, 1):
            join_type = join.get("type", "inner")
            if join_type not in {"inner", "left"}:
                raise DatabaseError("Join type must be inner or left")
            left, left_table = column(join["left"])
            right, right_table = column(join["right"])
            new_table = join["table"]
            if not ((left_table == new_table and right_table in tables[:index]) or (right_table == new_table and left_table in tables[:index])):
                raise DatabaseError("Join must connect the new table to an earlier table")
            sql += f" {join_type.upper()} JOIN {self._table(new_table)} t{index} ON {left} = {right}"
        params, predicates = [], []
        filters = request.get("filters", [])
        if not isinstance(filters, list) or len(filters) > 50:
            raise DatabaseError("At most 50 filters are allowed")

        def parameter(value):
            if not isinstance(value, (str, int, float, bool, type(None))) or (isinstance(value, str) and len(value) > 10000):
                raise DatabaseError("Filter values must be bounded JSON scalars")
            if isinstance(value, float) and not math.isfinite(value):
                raise DatabaseError("Non-finite filter values are not supported")
            params.append(value)
            return self._param

        for predicate in filters:
            _fields(predicate, {"column", "op", "value"}, {"column", "op"})
            name = column(predicate["column"])[0]
            op, value = predicate["op"], predicate.get("value")
            comparison = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
            if op in comparison:
                if value is None and op in {"eq", "ne"}:
                    predicates.append(name + (" IS NULL" if op == "eq" else " IS NOT NULL"))
                elif value is None:
                    raise DatabaseError("Null comparisons require eq, ne, is_null or not_null")
                else:
                    predicates.append(name + " " + comparison[op] + " " + parameter(value))
            elif op in {"is_null", "not_null"}:
                predicates.append(name + (" IS NULL" if op == "is_null" else " IS NOT NULL"))
            elif op in {"in", "not_in"}:
                if not isinstance(value, list) or not 1 <= len(value) <= 500:
                    raise DatabaseError("in/not_in requires 1 to 500 values")
                placeholders = ", ".join(parameter(v) for v in value)
                predicates.append(name + (" IN (" if op == "in" else " NOT IN (") + placeholders + ")")
            elif op in {"contains", "starts_with"}:
                if not isinstance(value, str):
                    raise DatabaseError("Text filters require strings")
                escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                predicates.append(name + " LIKE " + parameter(("%" if op == "contains" else "") + escaped + "%") + " ESCAPE '!' ")
            elif op == "between":
                if not isinstance(value, list) or len(value) != 2:
                    raise DatabaseError("between requires two values")
                predicates.append(name + " BETWEEN " + parameter(value[0]) + " AND " + parameter(value[1]))
            else:
                raise DatabaseError("Unsupported filter operator")
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        groups = request.get("group_by", [])
        if not isinstance(groups, list) or len(groups) > 20:
            raise DatabaseError("At most 20 grouping columns are allowed")
        group_expressions = [column(name)[0] for name in groups]
        if aggregates and any(column(name)[0] not in group_expressions for name in selected):
            raise DatabaseError("Every selected non-aggregate column must appear in group_by")
        if groups:
            sql += " GROUP BY " + ", ".join(group_expressions)
        orders = request.get("order_by", [])
        if not isinstance(orders, list) or len(orders) > 20:
            raise DatabaseError("At most 20 ordering columns are allowed")
        order_expressions = []
        for order in orders:
            _fields(order, {"column", "direction"}, {"column"})
            direction = order.get("direction", "asc")
            if direction not in {"asc", "desc"}:
                raise DatabaseError("Order direction must be asc or desc")
            reference = order["column"]
            expression = self._quote(reference) if reference in aggregate_aliases else column(reference)[0]
            order_expressions.append(expression + " " + direction.upper())
        if order_expressions:
            sql += " ORDER BY " + ", ".join(order_expressions)
        limit = _integer(request.get("limit", min(20, self.max_rows)), "limit", 1, self.max_rows)
        offset = _integer(request.get("offset", 0), "offset", 0, 1000000)
        sql += f" LIMIT {self._param} OFFSET {self._param}"
        params.extend([limit + 1, offset])
        return sql, params, labels, limit

    def query(self, request):
        started = time.monotonic()
        with self._connection() as connection:
            sql, params, labels, limit = self._compile(connection, request)
            rows, truncated, chars = [], False, 0
            with self._cursor(connection, stream=True) as cursor:
                cursor.execute(sql, params)
                for row in cursor:
                    if len(rows) == limit:
                        truncated = True
                        break
                    record = {label: _json_value(value) for label, value in zip(labels, row)}
                    length = len(json.dumps(record, ensure_ascii=False))
                    if chars + length > self.max_result_chars:
                        truncated = True
                        break
                    rows.append(record)
                    chars += length
        return {"source_id": self.id, "columns": labels, "rows": rows, "row_count": len(rows),
                "truncated": truncated, "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "queried_at": datetime.now().astimezone().isoformat()}

    def iter_documents(self, max_rows: int = 10000) -> Iterator[dict]:
        """Yield documents followed by one completeness marker; never infer completeness from EOF."""
        _integer(max_rows, "max_rows", 1, 1000000)
        entries = self.config.get("index", [])
        if not isinstance(entries, list) or len(entries) > 1000:
            raise DatabaseError("index must be a list")
        count, scanned_tables, seen = 0, [], set()
        complete, reason = True, None
        try:
            with self._connection() as connection:
                for entry in entries:
                    _fields(entry, {"table", "id_column", "text_columns", "updated_column"}, {"table", "id_column", "text_columns"})
                    table, id_column = entry["table"], entry["id_column"]
                    self._table(table)
                    if table in scanned_tables:
                        raise DatabaseError("Only one index specification per table is supported")
                    text_columns = entry["text_columns"]
                    if not isinstance(text_columns, list) or not text_columns or len(text_columns) > 50:
                        raise DatabaseError("text_columns requires 1 to 50 column names")
                    schema = {c["name"] for c in self._schema(connection, table)["columns"]}
                    required = [id_column] + text_columns + ([entry["updated_column"]] if entry.get("updated_column") else [])
                    if any(name not in schema for name in required):
                        raise DatabaseError("Index column is unavailable or not allowed")
                    scanned_tables.append(table)
                    # Bound each cell before transfer, including unexpected binary/large columns.
                    def bounded_text(name):
                        cast = "CHAR" if self.kind == "mysql" else "TEXT"
                        return "SUBSTR(CAST(" + self._quote(name) + f" AS {cast}), 1, 12001)"
                    selected = [self._quote(id_column)] + [bounded_text(c) for c in text_columns]
                    sql = "SELECT " + ", ".join(selected) + " FROM " + self._table(table) + " ORDER BY " + self._quote(id_column) + f" LIMIT {self._param}"
                    with self._cursor(connection, stream=True) as cursor:
                        cursor.execute(sql, (max_rows - count + 1,))
                        for row in cursor:
                            if count >= max_rows:
                                complete, reason = False, "row_limit"
                                break
                            identity = _json_value(row[0])
                            if identity is None:
                                raise DatabaseError("Indexed records require non-null unique identities")
                            key_material = json.dumps([self.id, table, id_column, identity], ensure_ascii=False, sort_keys=True)
                            if len(key_material) > 4096:
                                raise DatabaseError("Indexed record identity is too large")
                            key = "db:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                            if key in seen:
                                raise DatabaseError("Indexed record identities are not unique")
                            seen.add(key)
                            parts = [f"{name}: {value}" for name, value in zip(text_columns, row[1:]) if value is not None]
                            full_text = "\n".join(parts)
                            text = full_text[:12000]
                            truncated = len(full_text) > 12000
                            version = hashlib.sha256(text.encode("utf-8")).hexdigest()
                            count += 1
                            yield {"kind": "document", "key": key, "table": table, "text": text, "version": version,
                                   "locator": {"source_id": self.id, "table": table, "id_column": id_column, "id": identity,
                                               "columns": text_columns, "truncated": truncated}}
                    if not complete:
                        break
        except DatabaseError as error:
            complete, reason = False, str(error)
        yield {"kind": "snapshot", "complete": complete, "tables": scanned_tables, "row_count": count, "reason": reason}
