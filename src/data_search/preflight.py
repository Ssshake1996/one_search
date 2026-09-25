"""Explicit, bounded, read-only checks for a proposed database configuration."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

from .databases import DatabaseError, DatabaseSource
from .runtime import process_command


def database_fingerprint(sources: list[dict]) -> str:
    return hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def require_preflight(current: dict, candidate: dict, successful_fingerprint: str | None) -> None:
    """Editing unrelated settings never makes a new database connection."""
    sources = candidate.get("databases", [])
    if sources and sources != current.get("databases", []):
        if database_fingerprint(sources) != successful_fingerprint:
            raise ValueError("数据库配置已改变。请先点击“测试数据库”，通过后再保存。")


def _check_database(source: dict) -> dict:
    checks = []
    report = {"source_id": source.get("id", ""), "ok": False, "checks": checks}
    try:
        adapter = DatabaseSource(source)
        if len(adapter.tables) > 64 or len(source.get("index", [])) > 64:
            raise DatabaseError("Preflight checks at most 64 tables and index specifications per source")
        password_env = source.get("password_env")
        if password_env and password_env not in os.environ:
            raise DatabaseError("Configured password environment variable is missing in this process", "password_environment_missing", "在服务启动环境中设置密码变量；修改后重新启动服务并测试。")
        with adapter._connection() as connection:
            checks.append({"name": "connection", "ok": True, "message": "只读连接可用"})
            schemas = {}
            for table in adapter.tables:
                schema = adapter._schema(connection, table)
                fields = {field["name"]: field for field in schema["columns"]}
                expected = source.get("allowed_columns", {}).get(table, [])
                if not set(expected).issubset(fields):
                    raise DatabaseError("An allowed column is unavailable", "schema_changed", "重新发现结构，核对字段是否被改名、删除或撤销权限。")
                if not fields:
                    raise DatabaseError("No allowed readable columns are available", "no_readable_columns", "重新选择至少一个当前账号有权读取的字段。")
                # Check column SELECT permissions without transferring any row values.
                with adapter._cursor(connection) as cursor:
                    selected = ", ".join(adapter._quote(name) for name in fields)
                    cursor.execute("SELECT " + selected + " FROM " + adapter._table(table) + " WHERE 1=0")
                    cursor.fetchall()
                schemas[table] = (schema, fields)
                checks.append({"name": "columns", "table": table, "ok": True, "message": "表和允许字段可读取"})
            for entry in source.get("index", []):
                table, identity = entry["table"], entry["id_column"]
                if table not in schemas:
                    raise DatabaseError("Indexed table is unavailable or not allowed", "schema_changed", "重新选择授权表，或取消该表的正文索引。")
                schema, fields = schemas[table]
                text = entry.get("text_columns", [])
                ordered = [identity] + ([entry["updated_column"]] if entry.get("updated_column") else [])
                if not text or not set([*text, *ordered]).issubset(fields):
                    raise DatabaseError("Index column is unavailable or not allowed", "schema_changed", "核对正文、唯一键和水位字段是否仍在允许读取的字段中。")
                if schema["kind"] != "table" or not adapter._index_key_is_unique(connection, table, identity):
                    raise DatabaseError("Index identity requires a single-column primary key or non-partial UNIQUE index", "index_key_unavailable", "选择可验证的稳定单列唯一键，或保留该表为仅实时查询。")
                for column in ordered:
                    kind = fields[column]["type"].lower()
                    if any(token in kind for token in ("blob", "binary", "bytea", "json", "array", "bool")) or kind.endswith("[]"):
                        raise DatabaseError("Index identities and watermarks must use ordered scalar column types", "invalid_index_column_type", "选择整数、日期时间或其他可排序标量字段；不使用二进制、JSON、数组或布尔字段。")
                    with adapter._cursor(connection) as cursor:
                        cursor.execute("SELECT 1 FROM " + adapter._table(table) + " WHERE " + adapter._quote(column) + " IS NULL LIMIT 1")
                        if cursor.fetchall():
                            raise DatabaseError("Index identities and watermarks must be non-null", "index_column_has_null", "选择无空值的稳定键或水位；也可仅实时查询，不建立正文索引。")
                checks.append({"name": "stable_key", "table": table, "ok": True, "message": "唯一索引键及水位字段有效"})
        report["ok"] = True
    except DatabaseError as error:
        checks.append({"name": "validation", "ok": False, "message": str(error)[:300], "code": error.code, "action": error.action})
    except Exception:
        checks.append({"name": "validation", "ok": False, "message": "数据库配置或连接无效；请检查字段、权限和超时。"})
    return report


def check_database(source: dict, timeout_seconds: float = 8) -> dict:
    """A driver/network stall cannot outlive the requested parent-process deadline."""
    timeout_seconds = max(.1, min(float(timeout_seconds), 30))
    payload = json.dumps(source, ensure_ascii=True)
    if len(payload) > 1024 * 1024:
        raise ValueError("Database configuration is too large")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(process_command("data_search.preflight"), input=payload,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, **options)
        if result.returncode or len(result.stdout) > 256 * 1024:
            raise ValueError("Invalid preflight worker response")
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or not isinstance(report.get("ok"), bool):
            raise ValueError("Invalid preflight worker response")
        return report
    except subprocess.TimeoutExpired:
        message = "预检超时；现有配置和服务保持不变。"
    except (OSError, ValueError):
        message = "预检工作进程不可用；现有配置和服务保持不变。"
    return {"source_id": source.get("id", ""), "ok": False,
            "checks": [{"name": "worker", "ok": False, "message": message}]}


def check_databases(sources: list[dict], timeout_seconds: float = 30) -> dict:
    deadline = time.monotonic() + max(.1, min(float(timeout_seconds), 120))
    reports = []
    for source in sources:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reports.append({"source_id": source.get("id", ""), "ok": False,
                "checks": [{"name": "deadline", "ok": False, "message": "本次预检总时限已到，未测试此来源。"}]})
        else:
            reports.append(check_database(source, min(8, remaining)))
    return {"preflight": True, "ok": all(item["ok"] for item in reports), "databases": reports,
            "fingerprint": database_fingerprint(sources),
            "note": "只读预检；未读取正文。系统凭据属于当前账号；环境变量仅验证本次进程，登录启动环境需单独验证。"}


def main():
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        source = json.loads(raw)
        if not isinstance(source, dict):
            raise ValueError()
        if source.get("operation") == "discover_source":
            from .source_setup import _discover_source
            report = _discover_source(source["source"], source.get("max_tables", 64))
        else:
            report = _check_database(source)
    except Exception:
        report = {"ok": False, "checks": [{"name": "input", "ok": False, "message": "Invalid preflight request"}]}
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
