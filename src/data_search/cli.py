"""Command-line entry point. Machine-readable output stays on stdout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .config import defaults, load_config
from .resources import ResourceLimit
from .service import InstanceLock, ServiceError, rpc, run_daemon, service_status, start_service, stop_service


def _parser():
    parser = argparse.ArgumentParser(prog="data-search", description="Local, resource-bounded file and database search")
    parser.add_argument("--config", help="Configuration JSON path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for command in ["init", "daemon", "start", "stop", "status", "scan", "pause", "resume", "search", "fetch", "inspect", "query", "mcp", "model-download", "compact", "preflight"]:
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", default=argparse.SUPPRESS, help="Configuration JSON path")
        commands[command] = subparser
    commands["init"].add_argument("--data-dir", required=True)
    commands["init"].add_argument("--root", action="append", help="Search only this directory; repeat for more directories. Default: local machine disks")
    commands["init"].add_argument("--exclude", action="append", default=[], help="Exclude this directory tree; repeat for more exclusions")
    commands["init"].add_argument("--node-id", default="local")
    commands["search"].add_argument("query")
    commands["search"].add_argument("--mode", choices=["hybrid", "keyword", "semantic", "files"], default="hybrid")
    commands["search"].add_argument("--limit", type=int, default=20)
    commands["search"].add_argument("--source", dest="source_id")
    commands["search"].add_argument("--extension")
    commands["fetch"].add_argument("id")
    commands["fetch"].add_argument("--offset", type=int, default=0)
    commands["fetch"].add_argument("--limit", type=int, default=5)
    commands["inspect"].add_argument("--source", dest="source_id")
    commands['preflight'].add_argument('--source',dest='source_id')
    commands["query"].add_argument("--source", dest="source_id", required=True)
    commands["query"].add_argument("--request", required=True, help="JSON object, or @path to a JSON file")
    for name in ["search", "fetch", "inspect", "query", "status"]:
        commands[name].add_argument("--node-id")
    return parser


def _initialize(args):
    path = Path(args.config).expanduser().resolve()
    roots = [str(Path(root).expanduser().resolve()) for root in args.root] if args.root else None
    if any(not Path(root).is_dir() for root in roots or []):
        raise ValueError("Every search root must be an existing directory")
    config = defaults(args.data_dir, roots)
    config['exclude_paths'] = [str(Path(p).expanduser().resolve()) for p in args.exclude]
    config["node_id"] = args.node_id
    config["nodes"] = [{"id": args.node_id, "transport": "local"}]
    if not args.node_id or len(args.node_id) > 100:
        raise ValueError("node_id must contain 1 to 100 characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL protects user configuration even if installers race.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"status": "initialized", "config": str(path), "data_dir": config["data_dir"],
            "scope": config['scope'], "roots": config['roots'], "exclude_paths": config['exclude_paths']}


def main(argv=None):
    # Redirected Windows console streams otherwise depend on the user's legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config is required")
    try:
        if args.command == "init":
            result = _initialize(args)
        else:
            config = load_config(args.config)
            command = args.command
            if command == "daemon":
                from .runtime import configure_native_threads
                configure_native_threads(config["semantic"]["threads"])
                run_daemon(config)
                return 0
            if command == "mcp":
                from .mcp_server import run_mcp
                run_mcp(config)
                return 0
            if command == "start":
                result = start_service(config)
            elif command == 'preflight':
                from .preflight import check_databases
                sources = [source for source in config['databases'] if not args.source_id or source['id']==args.source_id]
                if args.source_id and not sources:
                    raise ValueError('Unknown database source')
                result = check_databases(sources)
            elif command == "stop":
                result = stop_service(config)
            elif command == "status":
                result = {"service": service_status(config), "index": rpc(config, "index_status", {"node_id": args.node_id} if args.node_id else {})}
            elif command in {"scan", "pause", "resume"}:
                result = rpc(config, command)
            elif command == "search":
                parameters = {key: getattr(args, key) for key in ["query", "mode", "limit", "source_id", "extension", "node_id"] if getattr(args, key) is not None}
                result = rpc(config, "search", parameters)
            elif command == "fetch":
                parameters = {key: getattr(args, key) for key in ["id", "offset", "limit", "node_id"] if getattr(args, key) is not None}
                result = rpc(config, "fetch", parameters)
            elif command == "inspect":
                parameters = {key: getattr(args, key) for key in ["source_id", "node_id"] if getattr(args, key) is not None}
                result = rpc(config, "inspect_source", parameters)
            elif command == "query":
                raw = Path(args.request[1:]).read_text(encoding="utf-8-sig") if args.request.startswith("@") else args.request
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("Query request must be a JSON object")
                parameters = {"source_id": args.source_id, "request": request}
                if args.node_id:
                    parameters["node_id"] = args.node_id
                result = rpc(config, "query_database", parameters)
            elif command == "model-download":
                from .model import download_model
                result = download_model(config["semantic"]["model_dir"])
            elif command == "compact":
                from .resources import Budget
                from .store import Store
                # The same OS lock as daemon startup prevents a check/start race.
                with InstanceLock(Path(config['data_dir']) / 'service.lock'):
                    index = Path(config['data_dir']) / 'index.sqlite3'
                    if not index.is_file():
                        raise ValueError('No index exists to compact')
                    budget = Budget(config)
                    budget.check(disk=True, reserve_mb=2 * index.stat().st_size / 1048576 + 16)
                    store = Store(config['data_dir'])
                    try:
                        result = {'status': 'compacted', **store.compact()}
                    finally:
                        store.close()
            else:
                raise ValueError("Unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (ValueError, OSError, ServiceError, ResourceLimit) as error:
        print(f"data-search: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"data-search: operation failed ({type(error).__name__}); check configuration and service status", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
