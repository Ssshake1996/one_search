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


def _json_argument(raw):
    return json.loads(Path(raw[1:]).read_text(encoding='utf-8-sig') if raw.startswith('@') else raw)


def _parser():
    parser = argparse.ArgumentParser(prog="data-search", description="Local, resource-bounded file and database search")
    parser.add_argument("--config", help="Configuration JSON path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {}
    for command in ["init", "daemon", "start", "stop", "status", "scan", "pause", "resume", "search", "fetch", "inspect", "query", "mcp", "model-download", "compact", "preflight",
                    "diagnose", "prioritize", "refresh", "context", "open", "model-status", "model-start", "model-import", "model-cancel", "model-quiesce", "installation-status",
                    "space", "version", "cleanup-backup", "export-config", "restore-config", "relocate-index", "clients", "register-client", "remove-client", "preset",
                    "discover-database", "propose-database", "store-credential", "credential-status", "delete-credential", "autostart", "lifecycle", "purge-external-index"]:
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
    commands['search'].add_argument('--extensions',nargs='+')
    commands['search'].add_argument('--directory')
    commands['search'].add_argument('--modified-after')
    commands['search'].add_argument('--modified-before')
    commands['search'].add_argument('--min-size',type=int)
    commands['search'].add_argument('--max-size',type=int)
    commands['search'].add_argument('--category')
    commands['search'].add_argument('--sort',choices=['relevance','modified_desc','modified_asc','name'],default='relevance')
    commands['search'].add_argument('--fold-duplicates',action='store_true')
    commands['pause'].add_argument('--seconds',type=float)
    for name in ['diagnose','prioritize','refresh']:
        commands[name].add_argument('path')
    commands['diagnose'].add_argument('--query')
    for name in ['context','open']:
        commands[name].add_argument('id')
    commands['context'].add_argument('--before',type=int,default=1)
    commands['context'].add_argument('--after',type=int,default=2)
    commands['open'].add_argument('--folder',action='store_true')
    commands['model-import'].add_argument('--source',required=True)
    commands['installation-status'].add_argument('--install-dir')
    for name in ['space','cleanup-backup']:
        commands[name].add_argument('--install-dir')
    for name in ['autostart','lifecycle']:
        commands[name].add_argument('--install-dir',required=True)
    commands['autostart'].add_argument('state',choices=['enable','disable'])
    commands['cleanup-backup'].add_argument('backup_id')
    commands['export-config'].add_argument('destination')
    commands['restore-config'].add_argument('bundle')
    commands['restore-config'].add_argument('--mappings',help='JSON object or @file mapping old directories to new')
    commands['restore-config'].add_argument('--apply',action='store_true')
    commands['relocate-index'].add_argument('destination')
    commands['register-client'].add_argument('client_id')
    commands['register-client'].add_argument('--label',required=True)
    commands['register-client'].add_argument('--kind',default='mcp')
    commands['remove-client'].add_argument('client_id')
    commands['preset'].add_argument('name',choices=['low','balanced','fast'])
    commands['preset'].add_argument('--apply',action='store_true')
    for name in ['discover-database','propose-database']:
        commands[name].add_argument('--source',dest='source_id')
        commands[name].add_argument('--request',help='JSON source or @file; for propose, object with source, selections, business_metadata')
    commands['store-credential'].add_argument('--reference')
    commands['store-credential'].add_argument('--stdin',action='store_true',help='Read secret from stdin instead of secure prompt; never use a command-line password')
    for name in ['credential-status','delete-credential']:
        commands[name].add_argument('reference')
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
                result = rpc(config, command, {'seconds':args.seconds} if command=='pause' and args.seconds is not None else {})
            elif command == "search":
                parameters = {key: getattr(args, key) for key in ["query", "mode", "limit", "source_id", "extension", "node_id",'extensions','directory','modified_after','modified_before','min_size','max_size','category','sort','fold_duplicates'] if getattr(args, key) is not None}
                result = rpc(config, "search", parameters)
            elif command in {'diagnose','prioritize','refresh','context','open'}:
                method, keys = {'diagnose':('diagnose_path',['path','query']), 'prioritize':('prioritize_path',['path']),
                    'refresh':('refresh_path',['path']), 'context':('read_context',['id','before','after']), 'open':('open_source',['id','folder'])}[command]
                result = rpc(config,method,{k:getattr(args,k) for k in keys if getattr(args,k) is not None})
            elif command in {'model-status','model-start','model-import','model-cancel','model-quiesce'}:
                from .model_manager import model_status,start_model_job,cancel_model_job,wait_for_model_idle
                result = (model_status(config) if command=='model-status' else cancel_model_job(config) if command=='model-cancel'
                          else wait_for_model_idle(config) if command=='model-quiesce' else start_model_job(config,args.source if command=='model-import' else None))
            elif command=='installation-status':
                from .installation import installation_status
                result = installation_status(config,args.install_dir)
            elif command in {'space','version','cleanup-backup','export-config','restore-config','relocate-index','clients','register-client','remove-client','preset'}:
                from . import maintenance
                if command=='space': result = maintenance.space_report(config,install_dir=args.install_dir)
                elif command=='version': result = maintenance.compatibility_info(config)
                elif command=='cleanup-backup': result = maintenance.cleanup_backup(config,args.backup_id,install_dir=args.install_dir)
                elif command=='export-config': result = maintenance.export_config(config,args.destination)
                elif command=='restore-config': result = maintenance.restore_config(config,args.bundle,path_mappings=_json_argument(args.mappings) if args.mappings else None,apply=args.apply)
                elif command=='relocate-index': result = maintenance.relocate_index(config,args.destination)
                elif command=='clients': result = {'clients':maintenance.registered_clients(config)}
                elif command=='register-client': result = maintenance.register_client(config,args.client_id,label=args.label,kind=args.kind)
                elif command=='remove-client': result = maintenance.remove_client(config,args.client_id)
                else:
                    from .runtime_policy import apply_preset
                    from .setup_ui import activate_settings
                    candidate = apply_preset(config,args.name)
                    candidate.pop('config_path',None)
                    if args.apply:
                        activate_settings(Path(args.config),config,candidate)
                    result = {'applied':args.apply,'preset':args.name,'scope_changed':False,
                              'resource':candidate['resource'],'scheduler':candidate['scheduler'],'semantic':candidate['semantic']}
            elif command in {'autostart','lifecycle','purge-external-index'}:
                from .maintenance import set_autostart,lifecycle_actions,purge_external_index
                if command=='autostart': result = set_autostart(config,args.install_dir,args.state=='enable')
                elif command=='lifecycle': result = lifecycle_actions(config,args.install_dir)
                else: result = purge_external_index(config)
            elif command in {'discover-database','propose-database'}:
                from .source_setup import discover_source,propose_source
                request = _json_argument(args.request) if args.request else None
                existing = next((s for s in config['databases'] if s['id']==args.source_id),None) if args.source_id else None
                if args.source_id and existing is None:
                    raise ValueError('Unknown database source')
                if command=='discover-database':
                    if request is None and existing is None:
                        raise ValueError('Provide --source or --request with a source object')
                    result = discover_source(request if request is not None else existing)
                else:
                    if not isinstance(request,dict) or 'selections' not in request or (existing is None and 'source' not in request):
                        raise ValueError('Proposal requires --request {source,selections}; with --source, request needs selections only')
                    result = propose_source(existing if existing is not None else request['source'],request['selections'],business_metadata=request.get('business_metadata'))
            elif command in {'store-credential','credential-status','delete-credential'}:
                from .credentials import store_credential,credential_status,delete_credential
                if command=='store-credential':
                    import getpass
                    secret = sys.stdin.readline().rstrip('\r\n') if args.stdin else getpass.getpass('Database password: ')
                    reference = store_credential(secret,args.reference)
                    del secret
                    result = {'credential_ref':reference,**credential_status(reference)}
                elif command=='credential-status': result = credential_status(args.reference)
                else:
                    delete_credential(args.reference)
                    result = {'deleted':True}
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
                from .model_manager import download_model_blocking
                result = download_model_blocking(config)
            elif command == "compact":
                from .resources import Budget
                from .store import Store
                from .maintenance import IndexDirectoryLease
                # The same OS lock as daemon startup prevents a check/start race.
                with InstanceLock(Path(config['data_dir']) / 'service.lock'), IndexDirectoryLease(config):
                    index = Path(config.get('index_dir',config['data_dir'])) / 'index.sqlite3'
                    if not index.is_file():
                        raise ValueError('No index exists to compact')
                    budget = Budget(config)
                    budget.check(disk=True, reserve_mb=2 * index.stat().st_size / 1048576 + 16)
                    store = Store(config.get('index_dir',config['data_dir']))
                    try:
                        result = {'status': 'compacted', **store.compact()}
                    finally:
                        store.close()
            else:
                raise ValueError("Unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command=='installation-status' and not result['ok']:
            return 1
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (ValueError, OSError, ServiceError, ResourceLimit) as error:
        print(json.dumps({'ok':False,'operation':args.command,'error':{'code':type(error).__name__,'message':str(error)}}))
        print(f"data-search: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"data-search: operation failed ({type(error).__name__}); check configuration and service status", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
