"""Explicit local maintenance with stable configuration and instance identity.

Search never calls these mutations. Relocation moves only managed index files;
configuration, model, credentials and host registrations keep their paths.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

from . import __version__
from .config import load_config
from .runtime_policy import _write_json
from .service import InstanceLock, ServiceError
from .upgrade import _target_path, _files, _digest


INDEX_NAMES = {"index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", "vectors.json",
               "vectors.dirty", "vectors.usearch", "vectors-catalog.sqlite",
               "vectors-catalog.sqlite-wal", "vectors-catalog.sqlite-shm"}
INDEX_SNAPSHOT = re.compile(r"vectors-[0-9a-f]{32}\.usearch\Z")
CONFIG_KEYS = {"scope", "roots", "exclude_paths", "exclude_names", "indexing", "resource",
               "scheduler", "runtime_policy", "semantic", "extraction", "databases",
               "scan_interval_seconds", "reconcile_interval_seconds"}
SECRET_KEYS = {"password", "dsn", "secret", "token", "access_token", "refresh_token", "api_key",
               "private_key", "connection_string", "credentials", "credential_ref", "credential_id"}


class MaintenanceGuard:
    """Quiesce owned model work and hold admission/worker/service locks.

    Use around runtime replacement or deletion, never from the daemon itself.
    Stopping the daemon is opt-in so preview/read operations stay nondisruptive.
    """
    def __init__(self, config: dict, *, stop=False, timeout=30):
        self.config, self.stop, self.timeout = config, stop, timeout
        self.stack = ExitStack()

    def __enter__(self):
        from .model_manager import wait_for_model_idle
        from .service import stop_service
        data = Path(self.config["data_dir"])
        try:
            self.stack.enter_context(InstanceLock(data / "model-job/manager.lock"))
            wait_for_model_idle(self.config, timeout=self.timeout)
            self.stack.enter_context(InstanceLock(data / "model-job/worker.lock"))
            if self.stop:
                stop_service(self.config)
            self.stack.enter_context(InstanceLock(data / "service.lock"))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _index_path(config):
    return _target_path(config.get("index_dir") or config["data_dir"])


def _index_files(directory):
    return sorted((p for p in directory.iterdir() if p.name in INDEX_NAMES or INDEX_SNAPSHOT.fullmatch(p.name)), key=lambda p: p.name) if directory.exists() else []


def _owner(config):
    return {"product": "one-search-index", "schema_version": 1,
            "instance_data_dir": str(_target_path(config["data_dir"]))}


class IndexDirectoryLease:
    """Held by the daemon/standalone engine, not by its own vector workers.

    OS lock prevents same-directory concurrent writers. The durable owner marker
    rejects another configured instance even when the first daemon is stopped.
    """
    def __init__(self, config: dict, *, directory=None):
        self.config, self.directory = config, _target_path(directory) if directory else _index_path(config)
        self.lock = InstanceLock(self.directory / ".one-search-index.lock")

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock.__enter__()
        try:
            marker = self.directory / ".one-search-index.json"
            expected = _owner(self.config)
            if marker.exists():
                if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != expected:
                    raise ValueError("Index directory belongs to another instance")
            else:
                if self.directory != _target_path(self.config["data_dir"]):
                    unowned = [p for p in self.directory.iterdir() if p.name != ".one-search-index.lock"]
                    if unowned:
                        raise ValueError("External index directory is nonempty and has no instance marker")
                _write_json(marker, expected)
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        self.lock.__exit__(*args)


def _size_tree(directory, *, max_files=100000):
    total, count, skipped, incomplete = 0, 0, 0, False
    if not directory.exists():
        return {"bytes": 0, "files": 0, "skipped_links": 0, "incomplete": False}
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in list(dirs):
            path = Path(base, name)
            if path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & 0x400):
                skipped += 1
                dirs.remove(name)
        for name in files:
            path = Path(base, name)
            if path.is_symlink():
                skipped += 1
                continue
            try:
                total += path.stat().st_size
            except OSError:
                incomplete = True
            count += 1
            if count >= max_files:
                return {"bytes": total, "files": count, "skipped_links": skipped, "incomplete": True}
    return {"bytes": total, "files": count, "skipped_links": skipped, "incomplete": incomplete}


def _manifest(install_dir):
    install = _target_path(install_dir)
    if install == Path(install.anchor) or install == Path.home().resolve():
        raise ValueError("Expected a dedicated installation directory")
    value = json.loads((install / "install-manifest.json").read_text(encoding="utf-8-sig"))
    if value.get("product") != "data-search" or value.get("schema_version") != 1 or value.get("install_dir") != str(install):
        raise ValueError("Install manifest does not match this managed directory")
    return install, value


def space_report(config: dict, *, install_dir=None):
    roots = []
    data, index, model = _target_path(config["data_dir"]), _index_path(config), _target_path(config["semantic"]["model_dir"])
    for path in (data, index, model):
        if any(path == p or path.is_relative_to(p) for p in roots):
            continue
        roots = [p for p in roots if not p.is_relative_to(path)] + [path]
    categories = {k: 0 for k in ("index", "model", "logs", "configuration_and_other")}
    incomplete, files, links = False, 0, 0
    for root in roots:
        for base, dirs, names in os.walk(root, followlinks=False):
            for name in list(dirs):
                p = Path(base, name)
                if p.is_symlink() or (os.name == "nt" and p.lstat().st_file_attributes & 0x400):
                    links += 1
                    dirs.remove(name)
            for name in names:
                p = Path(base, name)
                if p.is_symlink():
                    links += 1
                    continue
                category = "model" if p.is_relative_to(model) else "index" if p.parent == index and (p.name in INDEX_NAMES or INDEX_SNAPSHOT.fullmatch(p.name)) else "logs" if p.suffix == ".log" else "configuration_and_other"
                try:
                    categories[category] += p.stat().st_size
                except OSError:
                    incomplete = True
                files += 1
                if files >= 100000:
                    incomplete = True
                    break
            if files >= 100000:
                break
        if files >= 100000:
            break
    backups = retained_backups(config, install_dir=install_dir)
    return {"categories_bytes": categories, "total_bytes": sum(categories.values()), "files": files,
            "incomplete": incomplete, "skipped_links": links, "index_dir": str(index),
            "data_dir": str(data), "model_dir": str(model), "retained_backups": backups,
            "relocations": relocation_status(config),
            "note": "Backup sizes are listed separately; only owned local state is inspected."}


def _sanitize(value):
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()
                if key.casefold() not in SECRET_KEYS and not key.casefold().endswith(("_password", "_secret", "_token", "_api_key"))}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def export_config(config: dict, destination=None):
    settings = _sanitize({key: deepcopy(value) for key, value in config.items() if key in CONFIG_KEYS})
    settings.get("semantic", {}).pop("model_dir", None)
    value = {"product": "one-search-config", "schema_version": 1, "exported_at": _now(),
             "settings": settings, "requires_reconnect": [source.get("id") for source in settings.get("databases", []) if source.get("kind") != "sqlite"],
             "note": "Contains settings only. Model, index, credentials, instance identity and client registrations are not exported. password_env names are reconnect placeholders."}
    if destination is not None:
        path = _target_path(destination)
        if path == Path(config.get("config_path", Path(config["data_dir"]) / "config.json")).resolve():
            raise ValueError("Export destination must not overwrite the active configuration")
        _write_json(path, value)
    return value


def restore_config(config: dict, bundle: dict | str | Path, *, path_mappings=None, apply=False):
    value = json.loads(Path(bundle).read_text(encoding="utf-8-sig")) if not isinstance(bundle, dict) else deepcopy(bundle)
    if value.get("product") != "one-search-config" or value.get("schema_version") != 1 or not isinstance(value.get("settings"), dict):
        raise ValueError("Unsupported configuration export")
    if set(value["settings"]) - CONFIG_KEYS:
        raise ValueError("Export contains nonportable settings")
    candidate = deepcopy(config)
    candidate.update(_sanitize(value["settings"]))
    candidate.setdefault("semantic", {})["model_dir"] = config["semantic"]["model_dir"]
    mappings = sorted(((Path(a).expanduser().resolve(), Path(b).expanduser().resolve()) for a, b in (path_mappings or {}).items()), key=lambda pair: len(pair[0].parts), reverse=True)
    def remap(path):
        original = Path(path).expanduser().resolve()
        for previous, current in mappings:
            if original == previous or original.is_relative_to(previous):
                return str(current / original.relative_to(previous))
        return str(original)
    issues = []
    for group, key in ((candidate, "roots"), (candidate, "exclude_paths"),
                       (candidate.setdefault("indexing", {}), "content_roots"), (candidate["indexing"], "semantic_roots")):
        group[key] = [remap(path) for path in group.get(key, [])]
        if key != "exclude_paths":
            issues.extend({"reason": "directory_missing", "field": key, "path": path} for path in group[key] if not Path(path).is_dir())
    reconnect = []
    for source in candidate.get("databases", []):
        if source.get("kind") == "sqlite":
            source["path"] = remap(source.get("path", ""))
            if not Path(source["path"]).is_file():
                issues.append({"reason": "database_file_missing", "source_id": source.get("id"), "path": source["path"]})
        else:
            reconnect.append(source.get("id"))
    # Validate through the same loader without changing the active configuration.
    data = _target_path(config["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    scratch = data / (".restore-check-" + uuid.uuid4().hex + ".json")
    try:
        _write_json(scratch, candidate)
        checked = load_config(scratch)
        checked["config_path"] = config.get("config_path", str(data / "config.json"))
    finally:
        scratch.unlink(missing_ok=True)
    if apply:
        if issues:
            raise ValueError("Restore has missing local paths; inspect preview and provide path mappings")
        with InstanceLock(data / "service.lock"):
            _write_json(Path(checked["config_path"]), {k: v for k, v in checked.items() if k != "config_path"})
    return {"applied": bool(apply), "config": checked, "issues": issues, "requires_reconnect": reconnect,
            "restart_required": bool(apply), "scope": checked.get("scope"), "credentials_restored": False}


def registered_clients(config: dict):
    path = Path(config["data_dir"]) / "clients.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("clients"), list):
        raise ValueError("Client registry is invalid")
    return value["clients"]


def register_client(config: dict, client_id: str, *, label: str, kind="mcp"):
    for v in (client_id, label, kind):
        if not isinstance(v, str) or not v or len(v) > 160 or any(ord(c) < 32 for c in v):
            raise ValueError("Client ID, label and kind must be bounded plain text")
    data = Path(config["data_dir"])
    with InstanceLock(data / "clients.lock"):
        clients = registered_clients(config)
        existing = next((c for c in clients if c["id"] == client_id), None)
        if existing:
            existing.update(label=label, kind=kind, last_seen_at=_now())
        else:
            if len(clients) >= 100:
                raise ValueError("At most 100 clients may be registered")
            clients.append({"id": client_id, "label": label, "kind": kind, "registered_at": _now(), "last_seen_at": _now()})
        _write_json(data / "clients.json", {"schema_version": 1, "clients": clients})
    return {"registered": client_id, "clients": clients, "shared_instance": len(clients) > 1}


def remove_client(config: dict, client_id: str):
    data = Path(config["data_dir"])
    with InstanceLock(data / "clients.lock"):
        previous = registered_clients(config)
        clients = [c for c in previous if c["id"] != client_id]
        _write_json(data / "clients.json", {"schema_version": 1, "clients": clients})
    return {"removed": len(clients) != len(previous), "clients": clients,
            "daemon_stopped": False, "note": "Removing a registration does not uninstall the host plugin."}


def compatibility_info(config: dict):
    return {"version": __version__, "python": platform.python_version(), "platform": sys.platform,
            "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
            "config_schema": 1, "index_owner_schema": 1, "config_export_schema": 1,
            "data_dir": str(_target_path(config["data_dir"])), "index_dir": str(_index_path(config)),
            "clients": registered_clients(config), "multi_machine": "interface_reserved",
            "update_policy": "Use a checksummed release installer; native updates retain the previous runtime and data snapshot."}


def relocate_index(config: dict, destination, *, copy=shutil.copy2, disk_usage=shutil.disk_usage):
    source, target, data = _index_path(config), _target_path(destination), _target_path(config["data_dir"])
    if target == source:
        recovered = []
        # A crash may occur after the atomic config switch but before recording
        # completion. The verified inventory proves that this is a finished copy.
        with InstanceLock(data / 'service.lock'):
            for item in relocation_status(config):
                if item['phase'] == 'verified' and item['destination'] == str(target):
                    journal = data / 'maintenance' / (item['id'] + '.json')
                    record = json.loads(journal.read_text(encoding='utf-8'))
                    with IndexDirectoryLease(config):
                        if any(not (target / name).is_file() or _digest(target / name) != digest for name, digest in record['files'].items()):
                            raise ValueError('Interrupted relocation inventory no longer matches; old index retained')
                        record['phase'] = 'complete'
                        _write_json(journal, record)
                        recovered.append(item['id'])
        return {"changed": False, "index_dir": str(source), 'completed_transactions': recovered}
    if target == Path(target.anchor) or target == Path.home().resolve() or target == data or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Index destination must be an independent dedicated directory")
    pending = [item for item in relocation_status(config) if item['source'] == str(source)
               and item['destination'] == str(target) and item['phase'] in {'copying', 'verified', 'rolled_back'}]
    if target.exists():
        entries = [p for p in target.iterdir() if p.name not in {".one-search-index.lock", ".one-search-index.json"}]
        if entries and (not pending or any(p.name not in INDEX_NAMES and not INDEX_SNAPSHOT.fullmatch(p.name) for p in entries)):
            raise ValueError("Index destination must be empty")
    config_path = _target_path(config.get("config_path", data / "config.json"))
    if not config_path.is_file():
        raise ValueError("Index relocation requires a saved configuration")
    journal_dir = data / "maintenance"
    journal_dir.mkdir(parents=True, exist_ok=True)
    transaction = journal_dir / ("relocation-" + uuid.uuid4().hex + ".json")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    record = {"product": "one-search-relocation", "schema_version": 1, "phase": "copying",
              "created_at": _now(), "source": str(source), "destination": str(target), "data_dir": str(data), "files": {}}
    with ExitStack() as locks:
        locks.enter_context(InstanceLock(data / "service.lock"))
        locks.enter_context(IndexDirectoryLease(config, directory=source))
        locks.enter_context(IndexDirectoryLease(config, directory=target))
        current = load_config(config_path)
        if _index_path(current) != source or _target_path(current["data_dir"]) != data:
            raise ValueError("Configuration changed before relocation; reload it and retry")
        if pending:
            for path in _index_files(target):
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Interrupted relocation target contains a link or non-file artifact')
            for path in _index_files(target):
                path.unlink()
            for item in pending:
                previous_journal = journal_dir / (item['id'] + '.json')
                previous_record = json.loads(previous_journal.read_text(encoding='utf-8'))
                previous_record['phase'] = 'retried'
                _write_json(previous_journal, previous_record)
        files = _index_files(source)
        if any(p.is_symlink() or not p.is_file() for p in files):
            raise ValueError("Index relocation refuses links and non-file artifacts")
        required = sum(p.stat().st_size for p in files) + 64 * 1048576
        if disk_usage(target).free < required:
            raise ValueError("Insufficient disk space to copy and retain the existing index")
        original_config = config_path.read_bytes()
        _write_json(transaction, record)
        try:
            for path in files:
                copied_path = target / path.name
                copied.append(copied_path)
                copy(path, copied_path)
                digest = _digest(path)
                if _digest(copied_path) != digest:
                    raise ValueError("Index copy checksum verification failed")
                record["files"][path.name] = digest
                record['copied_bytes'] = record.get('copied_bytes', 0) + path.stat().st_size
                _write_json(transaction, record)
            # Full quick_check catches interrupted SQLite copies; no source write occurs.
            for name in ("index.sqlite3", "vectors-catalog.sqlite"):
                if (target / name).exists():
                    connection = sqlite3.connect((target / name).as_uri() + "?mode=ro", uri=True)
                    try:
                        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValueError("Copied index failed SQLite integrity validation")
                    finally:
                        connection.close()
            record["phase"] = "verified"
            _write_json(transaction, record)
            candidate = {k: deepcopy(v) for k, v in current.items() if k != "config_path"}
            candidate["index_dir"] = str(target)
            _write_json(config_path, candidate)
            record["phase"] = "complete"
            _write_json(transaction, record)
        except BaseException:
            # The old index never changed. Restore exact configuration before cleanup.
            if config_path.read_bytes() != original_config:
                temp = config_path.with_name(config_path.name + ".relocation-restore-" + uuid.uuid4().hex)
                temp.write_bytes(original_config)
                os.chmod(temp, 0o600)
                os.replace(temp, config_path)
            record["phase"] = "rolled_back"
            _write_json(transaction, record)
            for path in copied:
                if path.parent.resolve() != target or path.is_symlink():
                    raise ValueError("Relocation cleanup target changed")
                path.unlink(missing_ok=True)
            raise
    return {"changed": True, "index_dir": str(target), "previous_index_dir": str(source),
            "transaction": str(transaction), "copied_bytes": required - 64 * 1048576,
            "restart_required": True, "old_index_retained": True,
            "clients_affected": registered_clients(config)}


def relocation_status(config: dict):
    data = _target_path(config['data_dir'])
    directory, result = data / 'maintenance', []
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob('relocation-*.json')):
        if path.is_symlink() or not re.fullmatch(r'relocation-[0-9a-f]{32}\.json', path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('product') != 'one-search-relocation' or record.get('data_dir') != str(data):
                continue
            if any(name not in INDEX_NAMES and not INDEX_SNAPSHOT.fullmatch(name) for name in record.get('files', {})):
                continue
            result.append({'id': path.stem, 'phase': record['phase'], 'source': str(_target_path(record['source'])),
                           'destination': str(_target_path(record['destination'])), 'created_at': record.get('created_at'),
                           'copied_files': len(record.get('files', {})), 'copied_bytes': record.get('copied_bytes', 0),
                           'recommended_action': 'Retry relocate-index with the same destination while stopped' if record['phase'] in {'copying', 'verified', 'rolled_back'} else None})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result[-100:]


def retained_backups(config: dict, *, install_dir=None):
    data, result = _target_path(config["data_dir"]), []
    directory = data / "maintenance"
    if directory.exists():
        for path in sorted(directory.glob("relocation-*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("product") != "one-search-relocation" or record.get("phase") != "complete" or record.get("data_dir") != str(data):
                    continue
                source = _target_path(record["source"])
                if any(name not in INDEX_NAMES and not INDEX_SNAPSHOT.fullmatch(name) for name in record["files"]):
                    continue
                size = sum((source / name).stat().st_size for name in record["files"] if (source / name).is_file())
                result.append({"id": path.stem, "kind": "index_relocation", "path": str(source), "bytes": size,
                               "created_at": record.get("created_at"), "cleanup_eligible": source != _index_path(config)})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    if install_dir is not None:
        install, _ = _manifest(install_dir)
        for path in sorted(install.glob(".upgrade-*")):
            if not re.fullmatch(r"\.upgrade-[0-9a-f]{32}", path.name):
                continue
            try:
                record = json.loads((path / "transaction.json").read_text(encoding="utf-8"))
                if record.get("product") != "data-search-upgrade" or record.get("install_dir") != str(install) or record.get("data_dir") != str(data):
                    continue
                size = _size_tree(path)
                result.append({"id": path.name, "kind": "upgrade", "path": str(path), "bytes": size["bytes"],
                               "phase": record.get("phase"), "cleanup_eligible": record.get("phase") in {"complete", "rolled_back", "failed_before_activation"}, "incomplete": size["incomplete"]})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return result


def cleanup_backup(config: dict, backup_id: str, *, install_dir=None):
    entries = retained_backups(config, install_dir=install_dir)
    selected = next((entry for entry in entries if entry["id"] == backup_id), None)
    if selected is None or not selected["cleanup_eligible"]:
        raise ValueError("Backup is unknown, active or still in use")
    data = _target_path(config["data_dir"])
    with InstanceLock(data / "service.lock"):
        if selected["kind"] == "upgrade":
            install, _ = _manifest(install_dir)
            target = _target_path(selected["path"])
            if target.parent != install or target.name != backup_id:
                raise ValueError("Backup path escaped the managed installation")
            _files(target)  # Reject directory/file links before recursive removal.
            shutil.rmtree(target)
        else:
            journal = data / "maintenance" / (backup_id + ".json")
            record = json.loads(journal.read_text(encoding="utf-8"))
            source = _target_path(record["source"])
            if source == _index_path(config):
                raise ValueError("Cannot remove the current index")
            with IndexDirectoryLease(config, directory=source):
                paths = []
                for name, digest in record["files"].items():
                    if name not in INDEX_NAMES and not INDEX_SNAPSHOT.fullmatch(name):
                        raise ValueError("Unexpected index backup file")
                    path = source / name
                    if path.is_symlink() or path.parent.resolve() != source:
                        raise ValueError("Index backup path changed")
                    if path.exists() and _digest(path) != digest:
                        raise ValueError("Old index changed since relocation; cleanup refused")
                    paths.append(path)
                for path in paths:
                    path.unlink(missing_ok=True)
                record["phase"] = "cleaned"
                _write_json(journal, record)
    return {"cleaned": backup_id, "bytes_before_cleanup": selected["bytes"], "source_files_deleted": False}


def lifecycle_actions(config: dict, install_dir):
    install, manifest = _manifest(install_dir)
    if _target_path(manifest["data_dir"]) != _target_path(config["data_dir"]):
        raise ValueError("Installation belongs to a different instance")
    cli = _target_path(manifest.get("cli", install / ("venv/Scripts/data-search.exe" if os.name == "nt" else "venv/bin/data-search")))
    if not cli.is_relative_to(install):
        raise ValueError("Installed executable is outside the managed installation")
    config_path = str(Path(config.get("config_path", manifest["config"])).resolve())
    uninstall = ["powershell.exe", "-NoProfile", "-File", str(install / "uninstall.ps1"), "-InstallDir", str(install)] if os.name == "nt" else ["bash", str(install / "uninstall.sh"), "--install-dir", str(install)]
    return {"start": [str(cli), "start", "--config", config_path], "stop": [str(cli), "stop", "--config", config_path],
            "uninstall_preserve_data": uninstall, "uninstall_delete_data": uninstall + (["-DeleteData"] if os.name == "nt" else ["--delete-data"]),
            "autostart": manifest.get("autostart", "none"), "clients_affected": registered_clients(config),
            "external_index_dir": str(_index_path(config)) if _index_path(config) != _target_path(config["data_dir"]) else None,
            "note": "Stop/update/uninstall affects every registered client. External relocated index remains unless explicitly cleaned while stopped."}


def set_autostart(config: dict, install_dir, enabled: bool, *, runner=subprocess.run):
    """Change login startup only; stopping the live daemon is a separate action."""
    if not isinstance(enabled, bool):
        raise ValueError("Autostart enabled must be true or false")
    install, manifest = _manifest(install_dir)
    actions = lifecycle_actions(config, install)
    if os.name == "nt":
        import winreg
        from .upgrade import _startup_value, _restore_startup
        name = "DataSearch-" + hashlib.sha256(str(install).lower().encode("utf-8")).hexdigest()[:12]
        if manifest.get("startup_name") != name:
            raise ValueError("Unexpected managed startup entry")
        previous = _startup_value(name)
        launcher = _target_path(install / "launch-hidden.vbs")
        old_launcher = launcher.read_bytes() if launcher.exists() else None
        try:
            if enabled:
                command = ' '.join('"' + str(value) + '"' for value in actions['start'])
                if any(c in command for c in ('\r', '\n')):
                    raise ValueError("Startup paths cannot contain line breaks")
                launcher.write_text('CreateObject("WScript.Shell").Run "' + command.replace('"', '""') + '", 0, False\r\n', encoding='utf-16')
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, 'wscript.exe "' + str(launcher) + '"')
                else:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
            manifest['autostart'] = 'windows-user-run' if enabled else 'none'
            _write_json(install / 'install-manifest.json', manifest)
        except BaseException:
            _restore_startup(name, previous)
            if old_launcher is None:
                launcher.unlink(missing_ok=True)
            else:
                launcher.write_bytes(old_launcher)
            raise
    else:
        name = 'data-search-' + hashlib.sha256(str(install).encode()).hexdigest()[:12] + '.service'
        unit_dir = _target_path(Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user')
        unit = _target_path(unit_dir / name)
        if manifest.get('unit_name') != name or _target_path(manifest.get('unit_path', unit)) != unit:
            raise ValueError("Unexpected managed user service")
        old_unit = unit.read_bytes() if unit.exists() else None
        previous_autostart = manifest.get('autostart', 'none')
        def run(*args):
            return runner(['systemctl', '--user', *args], check=True, capture_output=True, timeout=20)
        try:
            if enabled:
                def quote(value):
                    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
                cli, _, _, conf = actions['start']
                unit_dir.mkdir(parents=True, exist_ok=True)
                unit.write_text('[Unit]\nDescription=one_search local index service\nAfter=default.target\n\n[Service]\nType=simple\nExecStart=' + quote(cli) + ' daemon --config ' + quote(conf) + '\nRestart=on-failure\nRestartSec=5\nNice=10\nUMask=0077\n\n[Install]\nWantedBy=default.target\n', encoding='utf-8')
                run('daemon-reload')
                run('enable', name)
            else:
                run('disable', name)
            manifest['autostart'] = 'systemd-user' if enabled else 'none'
            _write_json(install / 'install-manifest.json', manifest)
        except BaseException:
            if old_unit is None:
                unit.unlink(missing_ok=True)
            else:
                unit.write_bytes(old_unit)
            try:
                run('daemon-reload')
                run('enable' if previous_autostart == 'systemd-user' else 'disable', name)
            except (OSError, subprocess.SubprocessError):
                pass
            raise
    return {"autostart": manifest['autostart'], "daemon_state_changed": False,
            "clients_affected": registered_clients(config)}


def purge_external_index(config: dict):
    """Explicit DeleteData helper; caller must already hold MaintenanceGuard.

    Remove only named generated index artifacts. Unknown files and the directory
    itself are retained, so an unrelated source file can never be removed here.
    """
    index, data = _index_path(config), _target_path(config['data_dir'])
    if index == data:
        return {'external_index': False, 'deleted_files': 0}
    if not index.exists():
        return {'external_index': True, 'deleted_files': 0}
    with IndexDirectoryLease(config):
        files = _index_files(index)
        if any(p.is_symlink() or not p.is_file() for p in files):
            raise ValueError("External index cleanup refuses links and non-file artifacts")
        for path in files:
            if path.parent.resolve() != index:
                raise ValueError("External index cleanup escaped its directory")
            path.unlink()
    return {'external_index': True, 'deleted_files': len(files), 'index_dir': str(index),
            'source_files_deleted': False}
