from __future__ import annotations

import json
import math
import os
from pathlib import Path


def defaults(data_dir: str, roots: list[str] | None = None) -> dict:
    data = str(Path(data_dir).expanduser().resolve())
    return {
        "node_id": "local", "data_dir": data,
        "scope": "machine" if roots is None else "directories",
        "roots": [str(Path(p).expanduser().resolve()) for p in roots or []],
        "exclude_paths": [],
        "indexing": {"content_scope": "all", "content_roots": [], "content_extensions": [], "content_exclude_paths": [],
                     "semantic_scope": "all", "semantic_roots": [], "semantic_extensions": [], "semantic_exclude_paths": [],
                     "sensitive_content_excluded": False},
        "scan_interval_seconds": 180, "reconcile_interval_seconds": 3600,
        "scheduler": {"metadata_batch_size": 256, "metadata_items_per_tick": 2000,
                      "directories_per_tick": 64, "files_per_tick": 32,
                      "embedding_batches_per_tick": 4, "phase_seconds": 2.0,
                      "tick_seconds": 1.0, "journal_events_per_tick": 512},
        "exclude_names": [".git", ".venv", "node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"],
        "resource": {"memory_mb": 1024, "min_available_mb": 768, "max_disk_mb": 10240,
                     "min_free_disk_mb": 1024, "workers": 1, "batch_sleep_ms": 50,
                     "worker_memory_mb": 512, "worker_cpu_percent": 25},
        "extraction": {"max_file_mb": 32, "max_chars": 2_000_000, "timeout_seconds": 30},
        "semantic": {"enabled": True, "model_dir": str(Path(data) / "models" / "bge-small-zh-v1.5"),
                     "threads": 1, "idle_seconds": 120, "batch_size": 8,
                     "segment_size": 20000, "max_segments_per_sync": 4,
                     "compact_deleted_ratio": 0.3},
        "databases": [], "nodes": [{"id": "local", "transport": "local"}],
    }


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_config(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    value = json.loads(p.read_text(encoding="utf-8-sig"))
    config = defaults(value["data_dir"], value.get("roots", []))
    for k, v in value.items():
        if k in ("resource", "extraction", "semantic", "indexing", "scheduler"):
            if not isinstance(v, dict):
                raise ValueError(f"{k} must be an object")
            config[k].update(v)
        else:
            config[k] = v
    if not config["node_id"] or len(config["node_id"]) > 100:
        raise ValueError("invalid node_id")
    config["data_dir"] = str(Path(config["data_dir"]).expanduser().resolve())
    if 'index_dir' in config:
        if not isinstance(config['index_dir'],str) or not config['index_dir']:
            raise ValueError('index_dir must be a nonempty path')
        config['index_dir'] = str(Path(config['index_dir']).expanduser().resolve())
    from .runtime_policy import validate_policy
    config['runtime_policy'] = validate_policy(config.get('runtime_policy',{}))
    if not isinstance(config['indexing']['sensitive_content_excluded'],bool):
        raise ValueError('sensitive_content_excluded must be boolean')
    if config['scope'] not in {'machine', 'directories'}:
        raise ValueError('scope must be machine or directories')
    for key in ('roots', 'exclude_paths', 'exclude_names'):
        if not isinstance(config[key], list) or any(not isinstance(v, str) or not v for v in config[key]):
            raise ValueError(f'{key} must be a list of nonempty strings')
    config["roots"] = [str(Path(r).expanduser().resolve()) for r in config["roots"]]
    config['roots'] = list(dict.fromkeys(config['roots']))
    config['exclude_paths'] = list(dict.fromkeys(str(Path(r).expanduser().resolve()) for r in config['exclude_paths']))
    if config['scope'] == 'machine' and config['roots']:
        raise ValueError('machine scope discovers disks automatically; use directories scope with roots')
    for layer in ('content', 'semantic'):
        indexing = config['indexing']
        if indexing[f'{layer}_scope'] not in {'all', 'directories', 'none'}:
            raise ValueError(f'indexing.{layer}_scope must be all, directories or none')
        for suffix in ('roots', 'extensions', 'exclude_paths'):
            key = f'{layer}_{suffix}'
            if not isinstance(indexing[key], list) or any(not isinstance(item, str) or not item for item in indexing[key]):
                raise ValueError(f'indexing.{key} must be a list of nonempty strings')
        indexing[f'{layer}_roots'] = list(dict.fromkeys(str(Path(item).expanduser().resolve()) for item in indexing[f'{layer}_roots']))
        indexing[f'{layer}_exclude_paths'] = list(dict.fromkeys(str(Path(item).expanduser().resolve()) for item in indexing[f'{layer}_exclude_paths']))
        if any(not item.startswith('.') or '/' in item or '\\' in item for item in indexing[f'{layer}_extensions']):
            raise ValueError(f'indexing.{layer}_extensions must contain extensions beginning with a dot')
        indexing[f'{layer}_extensions'] = list(dict.fromkeys(item.lower() for item in indexing[f'{layer}_extensions']))
    config['semantic']['model_dir'] = str(Path(config['semantic']['model_dir']).expanduser().resolve())
    for group, keys in (("resource", ["memory_mb", "max_disk_mb"]),
                        ("extraction", ["max_file_mb", "max_chars", "timeout_seconds"]),
                        ("semantic", ["threads", "idle_seconds", "batch_size"])):
        for key in keys:
            if (isinstance(config[group][key], bool) or not isinstance(config[group][key], (float, int))
                or not math.isfinite(config[group][key]) or config[group][key] <= 0):
                raise ValueError(f"{group}.{key} must be positive")
    for key in ('min_available_mb', 'min_free_disk_mb', 'batch_sleep_ms'):
        number = config['resource'][key]
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or number < 0:
            raise ValueError(f'resource.{key} must be a finite nonnegative number')
    if config['resource']['workers'] != 1:
        raise ValueError('Only one background indexing worker is supported')
    for key, minimum, maximum in (('worker_memory_mb',64,65536), ('worker_cpu_percent',1,100)):
        number = config['resource'][key]
        if isinstance(number,bool) or not isinstance(number,int) or not minimum <= number <= maximum:
            raise ValueError(f'resource.{key} must be an integer from {minimum} to {maximum}')
    for key, maximum in (('threads',2),('batch_size',32)):
        if not isinstance(config['semantic'][key],int) or config['semantic'][key] > maximum:
            raise ValueError(f'semantic.{key} must be an integer from 1 to {maximum}')
    if not isinstance(config['semantic']['enabled'],bool):
        raise ValueError('semantic.enabled must be boolean')
    for key, minimum, maximum in (('segment_size',256,50000), ('max_segments_per_sync',1,32)):
        number = config['semantic'][key]
        if isinstance(number,bool) or not isinstance(number,int) or not minimum <= number <= maximum:
            raise ValueError(f'semantic.{key} must be an integer from {minimum} to {maximum}')
    ratio = config['semantic']['compact_deleted_ratio']
    if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise ValueError('semantic.compact_deleted_ratio must be greater than 0 and at most 1')
    for key, maximum in (('metadata_batch_size',2000),('metadata_items_per_tick',100000),
                         ('directories_per_tick',10000),('files_per_tick',1000),
                         ('embedding_batches_per_tick',1000),('journal_events_per_tick',4096)):
        number = config['scheduler'][key]
        if isinstance(number,bool) or not isinstance(number,int) or not 1 <= number <= maximum:
            raise ValueError(f'scheduler.{key} must be an integer from 1 to {maximum}')
    for key in ('phase_seconds','tick_seconds'):
        number = config['scheduler'][key]
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not .05 <= number <= 60:
            raise ValueError(f'scheduler.{key} must be between 0.05 and 60 seconds')
    if not isinstance(config['extraction']['max_chars'],int):
        raise ValueError('extraction.max_chars must be an integer')
    if config["scan_interval_seconds"] < 1 or config["reconcile_interval_seconds"] < 1:
        raise ValueError("scan intervals must be >= 1")
    ids = [d["id"] for d in config["databases"]]
    if any(not isinstance(i,str) or not i or i == 'files' for i in ids):
        raise ValueError('database IDs must be nonempty strings other than files')
    if len(ids) != len(set(ids)):
        raise ValueError("database IDs must be unique")
    for database in config['databases']:
        cap = database.get('index_max_rows',1000)
        if isinstance(cap,bool) or not isinstance(cap,int) or not 1 <= cap <= 10000:
            raise ValueError('index_max_rows must be an integer from 1 to 10000')
        sync = database.get('sync', {})
        if not isinstance(sync, dict):
            raise ValueError('database sync must be an object')
        for key, default, maximum in (('page_size',250,1000), ('max_pages_per_tick',4,100),
                                      ('reconcile_interval_seconds',3600,None)):
            number = sync.get(key, default)
            if isinstance(number,bool) or not isinstance(number,int) or number < 1 or (maximum is not None and number > maximum):
                raise ValueError(f'database sync.{key} must be a positive integer' + (f' <= {maximum}' if maximum else ''))
    for node in config.get("nodes", []):
        if node.get("transport") != "local":
            raise ValueError("Remote nodes are reserved, not implemented in this release")
    config["config_path"] = str(p)
    return config


def contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False
