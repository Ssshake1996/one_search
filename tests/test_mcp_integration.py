"""Real local daemon + official MCP stdio client, with synthetic data only."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from data_search.config import defaults, load_config
from data_search.service import ServiceError, rpc, service_status, start_service, stop_service


def test_real_daemon_and_official_mcp_stdio(tmp_path):
    root = tmp_path / "source files"
    root.mkdir()
    (root / "server-plan.txt").write_text("服务器费用降低计划：清理闲置机器，合并低负载服务。\nOwner: infrastructure team.", encoding="utf-8")
    db_path = tmp_path / "business.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, description TEXT, secret TEXT)")
        connection.execute("INSERT INTO tickets VALUES (1, '通过缓存降低数据库负载', 'not-authorized')")
    config_path = tmp_path / "config.json"
    config = defaults(str(tmp_path / "index data"), [str(root)])
    config["semantic"]["enabled"] = False
    config["resource"]["min_available_mb"] = 0
    config["resource"]["min_free_disk_mb"] = 0
    config["resource"]["batch_sleep_ms"] = 0
    config["databases"] = [{"id": "test-db", "kind": "sqlite", "path": str(db_path),
        "allowed_tables": ["tickets"], "allowed_columns": {"tickets": ["id", "description"]},
        "index": [{"table": "tickets", "id_column": "id", "text_columns": ["description"]}]}]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config = load_config(config_path)
    before_pids = set(psutil.pids())
    health = start_service(config)
    daemon_pid = health["pid"]
    state = json.loads((Path(config["data_dir"]) / "service.json").read_text())
    token = state["token"]
    try:
        assert daemon_pid not in before_pids
        assert start_service(config)["pid"] == daemon_pid
        assert "token" not in health
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = rpc(config, "index_status")
            coverage = status["coverage"]
            if not coverage["scanning"] and coverage["last_scan"]:
                break
            time.sleep(.1)
        else:
            pytest.fail("Initial indexing did not finish")
        assert not status["last_error"], status
        assert not coverage["source_errors"], coverage
        result = rpc(config, "search", {"query": "服务器", "mode": "keyword", "source_id": "files"})
        assert result["results"], result
        file_id = result["results"][0]["id"]
        fetched = rpc(config, "fetch", {"id": file_id})
        assert "服务器" in json.dumps(fetched, ensure_ascii=False)
        inspected = rpc(config, "inspect_source", {"source_id": "test-db"})
        assert "secret" not in json.dumps(inspected)
        queried = rpc(config, "query_database", {"source_id": "test-db", "request": {"table": "tickets", "columns": ["id", "description"]}})
        assert queried["row_count"] == 1
        assert "not-authorized" not in json.dumps(queried)
        with pytest.raises(ServiceError, match="Remote nodes"):
            rpc(config, "search", {"query": "a", "node_id": "another-server"})

        async def mcp_roundtrip():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "data_search", "mcp", "--config", str(config_path)], env=os.environ.copy())
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {"search", "fetch", "inspect_source", "query_database", "index_status"}
                    calls = [
                        ("search", {"query": "服务器", "mode": "keyword"}),
                        ("fetch", {"id": file_id}),
                        ("inspect_source", {"source_id": "test-db"}),
                        ("query_database", {"source_id": "test-db", "request": {"table": "tickets", "columns": ["id"]}}),
                        ("index_status", {}),
                    ]
                    for name, arguments in calls:
                        response = await session.call_tool(name, arguments)
                        assert not response.isError, response
                        text = " ".join(block.text for block in response.content if hasattr(block, "text"))
                        assert token not in text
                    denied = await session.call_tool("index_status", {"node_id": "remote"})
                    assert denied.isError
        asyncio.run(mcp_roundtrip())
        command = subprocess.run([sys.executable, "-m", "data_search", "status", "--config", str(config_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert command.returncode == 0, command.stderr
        assert token not in command.stdout + command.stderr
        assert json.loads(command.stdout)["service"]["pid"] == daemon_pid
    finally:
        stop_service(config)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(daemon_pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not psutil.pid_exists(daemon_pid)
        assert not (Path(config["data_dir"]) / "service.json").exists()
        assert stop_service(config)["stopped"] is False
