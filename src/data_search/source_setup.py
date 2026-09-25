"""Explicit local database setup: discover metadata, then propose an allowlist.

Discovery is for the settings/CLI onboarding workflow. It does not change saved
authorization and must not be exposed as the normal MCP inspect_source method.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess

from .databases import DatabaseError, DatabaseSource
from .runtime import process_command


def _diagnostic(error):
    if isinstance(error, DatabaseError):
        return {"code": error.code, "message": str(error)[:300], "action": error.action}
    return {"code": "invalid_source", "message": "来源配置或结构发现失败。", "action": "检查连接信息，并在相同账号下测试来源。"}


def _ordered(field):
    kind = field["type"].lower()
    return not (any(token in kind for token in ("blob", "binary", "bytea", "json", "array", "bool", "real", "float", "double")) or kind.endswith("[]"))


def _discover_source(config: dict, max_tables: int = 64) -> dict:
    """Metadata only; no body SELECT, row COUNT, schema mutation or allowlist save."""
    report = {"source_id": config.get("id", ""), "ok": False, "read_only": True,
        "metadata_only": True, "authorization_changed": False, "tables": [], "truncated": False,
        "diagnostics": [], "note": "发现目录不代表允许检索；只保存用户明确选择的表和字段。水位维护正确性需用户确认。"}
    try:
        if isinstance(max_tables, bool) or not isinstance(max_tables, int) or not 1 <= max_tables <= 64:
            raise DatabaseError("Discovery accepts 1 to 64 tables")
        # Validate the incoming connection and labels, but discovery intentionally
        # enumerates metadata visible to this account, beyond its saved allowlist.
        DatabaseSource(config)
        connection_config = {**config, "allowed_tables": [], "allowed_columns": {}, "index": [], "business_metadata": {}}
        adapter = DatabaseSource(connection_config)
        with adapter._connection() as connection:
            with adapter._cursor(connection) as cursor:
                if adapter.kind == "sqlite":
                    cursor.execute("SELECT name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT ?", (max_tables + 1,))
                    names = [row[0] for row in cursor.fetchall()]
                elif adapter.kind == "mysql":
                    cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE IN ('BASE TABLE','VIEW') ORDER BY TABLE_NAME LIMIT %s", (config["database"], max_tables + 1))
                    names = [row[0] for row in cursor.fetchall()]
                else:
                    cursor.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_schema NOT LIKE 'pg_toast%%' ORDER BY table_schema, table_name LIMIT %s", (max_tables + 1,))
                    names = [row[0] + "." + row[1] for row in cursor.fetchall()]
            report["truncated"] = len(names) > max_tables
            adapter.tables = names[:max_tables]
            for name in adapter.tables:
                schema = adapter._schema(connection, name)
                fields = schema["columns"]
                unique = adapter._single_column_unique_keys(connection, name) if schema["kind"] == "table" else set()
                keys = []
                for field in fields:
                    if field["name"] not in unique or not _ordered(field):
                        continue
                    # Only INTEGER PRIMARY KEY aliases SQLite rowid; TEXT primary
                    # keys and nullable UNIQUE columns still need the NULL preflight.
                    known_non_null = not field["nullable"] or (adapter.kind == "sqlite" and field["primary_key"] and field["type"].lower() == "integer")
                    keys.append({"column": field["name"], "requires_null_check": not known_non_null,
                                 "primary_key": field["primary_key"]})
                keys.sort(key=lambda item: (item["requires_null_check"], not item["primary_key"], item["column"]))
                watermark_names = {"updated_at", "updated_on", "modified_at", "modified_on", "last_updated", "last_modified", "update_time", "mtime", "version"}
                watermarks = [field["name"] for field in fields if field["name"].lower() in watermark_names and _ordered(field)]
                texts = [field["name"] for field in fields if any(token in field["type"].lower() for token in ("char", "text", "clob"))]
                selected = next((key["column"] for key in keys if not key["requires_null_check"]), None)
                schema["columns"] = fields[:256]
                schema["columns_truncated"] = len(fields) > 256
                visible = {field["name"] for field in schema["columns"]}
                schema["index_recommendation"] = {"mode": "index_available" if keys else "realtime_only",
                    "id_column": selected if selected in visible else None,
                    "key_candidates": [key for key in keys if key["column"] in visible],
                    "text_columns": [field for field in texts if field in visible][:50],
                    "watermark_candidates": [field for field in watermarks if field in visible],
                    "watermark_requires_confirmation": True,
                    "reason": "catalog_unique_key_available" if keys else "view_or_no_supported_single_column_unique_key"}
                report["tables"].append(schema)
        report["ok"] = True
    except Exception as error:
        report["diagnostics"].append(_diagnostic(error))
    return report


def discover_source(source: dict, timeout_seconds: float = 8, max_tables: int = 64) -> dict:
    """Bounded subprocess API for a local settings action; returns no source rows."""
    payload = json.dumps({"operation": "discover_source", "source": source, "max_tables": max_tables}, ensure_ascii=True)
    if len(payload) > 1024 * 1024:
        raise ValueError("Database configuration is too large")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(process_command("data_search.preflight"), input=payload,
            capture_output=True, text=True, encoding="utf-8", timeout=max(.1, min(float(timeout_seconds), 30)), **options)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError("Invalid discovery response")
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or not isinstance(report.get("ok"), bool) or not isinstance(report.get("tables"), list):
            raise ValueError("Invalid discovery response")
        return report
    except subprocess.TimeoutExpired:
        diagnostic = {"code": "discovery_timeout", "message": "结构发现超时。", "action": "检查连接或缩小发现表数量后重试；现有授权未改变。"}
    except (ValueError, OSError):
        diagnostic = {"code": "discovery_worker_unavailable", "message": "结构发现进程不可用。", "action": "检查安装完整性；现有授权未改变。"}
    return {"source_id": source.get("id", ""), "ok": False, "tables": [], "diagnostics": [diagnostic], "authorization_changed": False}


def propose_source(source: dict, selections: list[dict], business_metadata: dict | None = None,
                   *, timeout_seconds: float = 8) -> dict:
    """Return a candidate config; caller still preflights and explicitly saves it.

    Each selection is {table, columns, index_text_columns?, id_column?,
    updated_column?}. No available unique key means realtime-only, not rejection
    of an otherwise readable table. Suggested text columns are never auto-granted.
    """
    if not isinstance(selections, list) or len(selections) > 64:
        raise DatabaseError("Select at most 64 tables")
    discovery = discover_source(source, timeout_seconds)
    if not discovery["ok"]:
        return {"ok": False, "source": None, "notes": [], "diagnostics": discovery["diagnostics"]}
    tables = {table["table"]: table for table in discovery["tables"]}
    candidate = copy.deepcopy(source)
    candidate.update(allowed_tables=[], allowed_columns={}, index=[])
    notes = []
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) - {"table", "columns", "index_text_columns", "id_column", "updated_column"}:
            raise DatabaseError("Invalid table selection")
        name, columns = selection.get("table"), selection.get("columns")
        if not isinstance(name, str) or name not in tables or name in candidate["allowed_tables"]:
            raise DatabaseError("Selected table is unavailable or repeated", "schema_changed")
        known = {field["name"] for field in tables[name]["columns"]}
        if not isinstance(columns, list) or not columns or any(not isinstance(column, str) or column not in known for column in columns):
            raise DatabaseError("Explicitly select available columns", "schema_changed")
        columns = list(dict.fromkeys(columns))
        candidate["allowed_tables"].append(name)
        candidate["allowed_columns"][name] = columns
        text = selection.get("index_text_columns", [])
        if not isinstance(text, list) or len(text) > 50 or any(not isinstance(column, str) or column not in columns for column in text):
            raise DatabaseError("Index text columns must be explicitly allowed")
        if not text:
            notes.append({"table": name, "mode": "realtime_only", "reason": "text_index_not_selected"})
            continue
        recommendation = tables[name]["index_recommendation"]
        keys = {item["column"] for item in recommendation["key_candidates"]}
        identity = selection.get("id_column") or recommendation["id_column"]
        if not keys or identity is None:
            notes.append({"table": name, "mode": "realtime_only", "reason": "no_confirmed_unique_key", "message": "仍可结构化实时查询；持续索引需要选择可验证的稳定单列唯一键。"})
            continue
        if not isinstance(identity, str) or identity not in keys or identity not in columns:
            raise DatabaseError("Index key must be an explicitly allowed single-column unique key")
        entry = {"table": name, "id_column": identity, "text_columns": list(dict.fromkeys(text))}
        updated = selection.get("updated_column")
        if updated:
            fields = {field["name"]: field for field in tables[name]["columns"]}
            if not isinstance(updated, str) or updated not in columns or not _ordered(fields[updated]):
                raise DatabaseError("Watermark must be an explicitly allowed ordered scalar")
            entry["updated_column"] = updated
        candidate["index"].append(entry)
        notes.append({"table": name, "mode": "indexed", "requires_preflight": True,
            "watermark_maintenance_required": bool(updated), "sync": "incremental_with_reconciliation" if updated else "periodic_full_scan"})
    if business_metadata is None:
        metadata = copy.deepcopy(source.get("business_metadata", {}))
        metadata["tables"] = {name: value for name, value in metadata.get("tables", {}).items() if name in candidate["allowed_tables"]}
        for name, value in metadata["tables"].items():
            value["columns"] = {column: labels for column, labels in value.get("columns", {}).items() if column in candidate["allowed_columns"][name]}
        candidate["business_metadata"] = metadata
    else:
        candidate["business_metadata"] = copy.deepcopy(business_metadata)
    DatabaseSource(candidate)
    return {"ok": True, "source": candidate, "notes": notes, "diagnostics": [],
            "requires_preflight": True, "authorization_changed": False}
