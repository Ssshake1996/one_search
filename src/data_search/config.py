from __future__ import annotations

import json
import math
import os
from pathlib import Path


def defaults(data_dir: str, roots: list[str]) -> dict:
    data = str(Path(data_dir).expanduser().resolve())
    return {
        "node_id": "local", "data_dir": data,
        "roots": [str(Path(p).expanduser().resolve()) for p in roots],
        "scan_interval_seconds": 180, "reconcile_interval_seconds": 3600,
        "exclude_names": [".git", ".venv", "node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"],
        "resource": {"memory_mb": 1024, "min_available_mb": 768, "max_disk_mb": 10240,
                     "min_free_disk_mb": 1024, "workers": 1, "batch_sleep_ms": 50},
        "extraction": {"max_file_mb": 32, "max_chars": 2_000_000, "timeout_seconds": 30},
        "semantic": {"enabled": True, "model_dir": str(Path(data) / "models" / "bge-small-zh-v1.5"),
                     "threads": 1, "idle_seconds": 120, "batch_size": 8},
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
        if k in ("resource", "extraction", "semantic"):
            if not isinstance(v, dict):
                raise ValueError(f"{k} must be an object")
            config[k].update(v)
        else:
            config[k] = v
    if not config["node_id"] or len(config["node_id"]) > 100:
        raise ValueError("invalid node_id")
    config["data_dir"] = str(Path(config["data_dir"]).expanduser().resolve())
    config["roots"] = [str(Path(r).expanduser().resolve()) for r in config["roots"]]
    config['roots'] = list(dict.fromkeys(config['roots']))
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
    for key, maximum in (('threads',2),('batch_size',32)):
        if not isinstance(config['semantic'][key],int) or config['semantic'][key] > maximum:
            raise ValueError(f'semantic.{key} must be an integer from 1 to {maximum}')
    if not isinstance(config['semantic']['enabled'],bool):
        raise ValueError('semantic.enabled must be boolean')
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
