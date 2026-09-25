"""Exercise a frozen Windows build using only a temporary synthetic directory."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    executable = str(args.exe.resolve())
    with tempfile.TemporaryDirectory(prefix="one-search-native-smoke-") as temp:
        work = Path(temp)
        root = work / "synthetic documents"
        root.mkdir()
        (root / "server-plan.txt").write_text("服务器费用降低计划：清理闲置机器，合并低负载服务。", encoding="utf-8")
        (root / "unrelated.txt").write_text("面包食谱：面粉、鸡蛋与牛奶。", encoding="utf-8")
        database = work / "sample.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE tickets(id INTEGER PRIMARY KEY, description TEXT, secret TEXT)")
            connection.execute("INSERT INTO tickets VALUES(1, '通过缓存降低数据库负载', 'not-authorized')")
        connection.close()
        config_path = work / "data/config.json"
        def cli(*command):
            result = subprocess.run([executable, *command, "--config", str(config_path)], capture_output=True, text=True, encoding="utf-8", timeout=90)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
        cli("init", "--data-dir", str(config_path.parent), "--root", str(root))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["scope"] == "directories" and config["roots"] == [str(root)]
        config["resource"].update(min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
        config["semantic"]["enabled"] = bool(args.model_dir)
        if args.model_dir:
            config["semantic"]["model_dir"] = str(args.model_dir.resolve())
        config["databases"] = [{"id": "synthetic-db", "kind": "sqlite", "path": str(database),
            "allowed_tables": ["tickets"], "allowed_columns": {"tickets": ["id", "description"]},
            "index": [{"table": "tickets", "id_column": "id", "text_columns": ["description"]}]}]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            health = cli("start")
            assert health["started"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                status = cli("status")
                coverage = status["index"]["coverage"]
                if coverage["last_scan"] and not coverage["scanning"] and (not args.model_dir or coverage["embedded_chunks"] >= 3):
                    break
                time.sleep(.25)
            else:
                raise AssertionError(f"Indexing incomplete: {status}")
            assert not status["index"]["last_error"], status
            assert not coverage["source_errors"], status
            result = cli("search", "服务器", "--mode", "keyword", "--source", "files")
            assert result["results"], result
            file_id = result["results"][0]["id"]
            assert "服务器" in json.dumps(cli("fetch", file_id), ensure_ascii=False)
            if args.model_dir:
                semantic = cli("search", "如何降低服务器成本", "--mode", "semantic", "--source", "files")
                assert any("server-plan.txt" in json.dumps(row) for row in semantic["results"]), semantic
            async def mcp_roundtrip():
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
                params = StdioServerParameters(command=executable, args=["mcp", "--config", str(config_path)], env=os.environ.copy())
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tool_list = await session.list_tools()
                        names = {tool.name for tool in tool_list.tools}
                        assert names == {'search', 'fetch', 'inspect_source', 'query_database', 'index_status',
                                         'diagnose_path', 'read_context', 'refresh_path', 'prioritize_path',
                                         'pause_indexing', 'resume_indexing'}, names
                        for name, arguments in [("search", {"query": "服务器", "mode": "keyword"}),
                            ("fetch", {"id": file_id}), ("inspect_source", {"source_id": "synthetic-db"}),
                            ("query_database", {"source_id": "synthetic-db", "request": {"table": "tickets", "columns": ["id", "description"]}}),
                            ("index_status", {}), ('diagnose_path', {'path': str(root / 'server-plan.txt')}),
                            ('read_context', {'id': file_id}), ('pause_indexing', {'seconds': 60}),
                            ('refresh_path', {'path': str(root / 'server-plan.txt')}),
                            ('prioritize_path', {'path': str(root)}), ('resume_indexing', {})]:
                            reply = await session.call_tool(name, arguments)
                            assert not reply.isError, reply
                            assert "not-authorized" not in str(reply)
            asyncio.run(mcp_roundtrip())
            print(json.dumps({"native_exe": executable, "files": 2, "sqlite_rows": 1,
                              "embedded_chunks": coverage["embedded_chunks"], "mcp_tools": 11, "passed": True}, indent=2))
        finally:
            cli("stop")


if __name__ == "__main__":
    main()
