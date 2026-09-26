"""Transactional Windows native installation, including immediate migration rollback.

The complete index/configuration snapshot is retained after a successful upgrade.
It is not a general downgrade facility: only this installation's failed activation
is automatically rolled back, after proving that the data directory is unlocked.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .service import InstanceLock
from .config import atomic_json
from .runtime_use import assert_runtime_available
from .upgrade_hosts import UpgradeCoordinationError, UpgradeSession


APP_FILES = ("install-manifest.json", "mcp.json", "Settings.vbs", "launch-hidden.vbs", "uninstall.ps1", "plugin")
TRANSIENT_DATA = {"service.lock", "service.json", "daemon.log", "daemon.previous.log",
                  ".one-search-index.lock", "model-job/manager.lock", "model-job/worker.lock",
                  "upgrade.lock", "upgrade-state.json", "host-clients"}


def _validate_target(path, marker, expected, *, recovery=False):
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ValueError("Upgrade target must be a dedicated application directory")
    for ancestor in [path, *path.parents]:
        if ancestor.exists() and (ancestor.is_symlink() or (os.name == "nt" and ancestor.lstat().st_file_attributes & 0x400)):
            raise ValueError("Upgrade target must not use directory links")
    if not path.exists():
        return
    marker_path = path / marker
    if marker_path.exists():
        try:
            actual = json.loads(marker_path.read_text(encoding="utf-8-sig"))
            if not isinstance(actual, dict):
                raise ValueError("Managed directory marker must be an object")
        except (OSError, ValueError):
            if recovery:
                return
            raise
        if any(actual.get(key) != value for key, value in expected.items()):
            if recovery:
                return
            raise ValueError("Existing managed directory marker does not match the upgrade target")
    elif any(path.iterdir()) and not (marker == ".data-search-data.json" and
            all(item.name == "host-clients" and item.is_dir() and not item.is_symlink() and
                all(child.is_file() and not child.is_symlink() and child.name.endswith((".json", ".json.tmp")) for child in item.iterdir())
                for item in path.iterdir())):
        if not recovery:
            raise ValueError("Upgrade refuses a nonempty unmanaged directory")


def _recovery_identity(install, data):
    """Prove ownership from complete snapshots if activation damaged a marker."""
    try:
        marker_path = data / "upgrade-state.json"
        if marker_path.is_symlink() or marker_path.stat().st_size > 65536:
            return False
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("install_dir") != str(install) or marker.get("data_dir") != str(data):
            return False
        transaction = _target_path(marker["transaction"])
        if transaction.parent != install or not re.fullmatch(r"\.upgrade-[0-9a-f]{32}", transaction.name):
            return False
        journal = json.loads((transaction / "transaction.json").read_text(encoding="utf-8"))
        if (journal.get("product") != "data-search-upgrade" or journal.get("install_dir") != str(install) or
                journal.get("data_dir") != str(data) or journal.get("phase") not in
                {"swapping", "activating", "rolling_back", "restored", "rollback_blocked"}):
            return False
        for relative, expected in (("app-snapshot/install-manifest.json", {
                "product": "data-search", "install_dir": str(install), "data_dir": str(data)}),
                ("data-snapshot/.data-search-data.json", {"product": "data-search", "data_dir": str(data)})):
            path = _target_path(transaction / relative)
            if not path.is_relative_to(transaction):
                return False
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if any(saved.get(key) != value for key, value in expected.items()):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _target_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    for ancestor in [path, *path.parents]:
        if ancestor.exists() and (ancestor.is_symlink() or (os.name == "nt" and ancestor.lstat().st_file_attributes & 0x400)):
            raise ValueError("Upgrade target must not use directory links")
    return path.resolve()


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _runtime_identity(directory):
    inventory = {path.relative_to(directory).as_posix(): _digest(path) for path in _files(directory)}
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def _verified_model_files(directory):
    """Exclude known immutable assets only; unknown files still need a snapshot."""
    from .model import MODEL_ID, SHA256
    manifest = directory / "manifest.json"
    try:
        if manifest.is_symlink() or json.loads(manifest.read_text(encoding="utf-8")).get("model_id") != MODEL_ID:
            return []
        assets = [directory / name for name in SHA256]
        if any(path.is_symlink() or not path.is_file() or _digest(path) != SHA256[path.name] for path in assets):
            return []
        return [manifest, *assets]
    except (OSError, ValueError):
        return []


def _files(root: Path, excluded: tuple[Path, ...] = ()):
    if not root.exists():
        return []
    if root.is_symlink() or (os.name == "nt" and root.lstat().st_file_attributes & 0x400):
        raise ValueError("Upgrade snapshot root must not be a directory link")
    result = []
    for base, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(base, name)
            if any(path == item or path.is_relative_to(item) for item in excluded):
                directories.remove(name)
            elif path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & 0x400):
                raise ValueError("Upgrade snapshot refuses directory links outside the excluded model")
        for name in files:
            path = Path(base, name)
            if any(path == item or path.is_relative_to(item) for item in excluded):
                continue
            if path.is_symlink():
                raise ValueError("Upgrade snapshot refuses symbolic links")
            result.append(path)
    return result


def verify_runtime(runtime: Path):
    checksums = json.loads((runtime.parent / "SHA256SUMS.json").read_text(encoding="utf-8"))
    expected = {}
    for name, digest in checksums.items():
        if name.startswith("runtime/"):
            relative = Path(name.removeprefix("runtime/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid runtime checksum path")
            expected[relative.as_posix()] = digest
    actual = {path.relative_to(runtime).as_posix(): path for path in _files(runtime)}
    if not expected or set(expected) != set(actual) or "data-search.exe" not in expected:
        raise ValueError("Runtime checksum inventory does not match bundle contents")
    for name, path in actual.items():
        if _digest(path) != expected[name]:
            raise ValueError("Runtime integrity validation failed")
    return expected


def _copy_files(files, source, destination, copy=shutil.copy2):
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy(path, target)


def _app_files(install):
    result = []
    for name in APP_FILES:
        path = install / name
        if path.is_dir():
            result.extend(_files(path))
        elif path.is_file():
            if path.is_symlink():
                raise ValueError("Managed application metadata must not use symbolic links")
            result.append(path)
    return result


def _startup_value(name):
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            return winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None


def _restore_startup(name, previous):
    if os.name != "nt" or _startup_value(name) == previous:
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        if previous is None:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, name, 0, previous[1], previous[0])


def _restore_files(source: Path, destination: Path, current_files):
    # Replace originals atomically before deleting additional migration artifacts.
    # An interrupted restore must not erase the managed-directory identity markers.
    root = destination.resolve()
    restored = set()
    for path in _files(source):
        target = destination / path.relative_to(source)
        if not target.resolve().is_relative_to(root):
            raise ValueError("Rollback target escaped the managed directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".restore-" + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copy2(path, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        restored.add(target)
    # Every removal is an exact file within a validated managed destination.
    for path in current_files:
        if not path.resolve().is_relative_to(root):
            raise ValueError("Rollback target escaped the managed directory")
        if path not in restored:
            path.unlink()


def _rename_runtime(source: Path, destination: Path, *, timeout=5):
    """Allow Windows image/file handles to close after the service lock is released."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            source.rename(destination)
            return
        except OSError as error:
            # The authenticated service may have removed its state and released
            # its locks while native process teardown still has DLLs mapped.
            # Keep the maintenance locks held and retain the ordinary failure
            # path if the directory remains locked or permissions are wrong.
            remaining = deadline - time.monotonic()
            if getattr(error, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                raise
            time.sleep(min(0.1, remaining))


def _wait_runtime_free(install, data, *, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            assert_runtime_available(install, data, allow_service_workers=False)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def _rollback_configuration(previous_config, data, index):
    if previous_config is not None:
        return previous_config
    # A failed first installation can already have queued a model worker.
    # Its new, verified instance configuration must be quiesced too, before
    # restoring the empty pre-install snapshot and removing the new runtime.
    path = data / "config.json"
    if not path.exists():
        return None
    from .config import load_config
    config = load_config(path)
    if _target_path(config["data_dir"]) != data or _target_path(config.get("index_dir") or data) != index:
        raise ValueError("Activated configuration does not match the recovery instance")
    return config


def _recover_interrupted(previous, session, *, runner=subprocess.run):
    """Retry the same installer to recover a killed updater before a new upgrade.

    The journal is written before either runtime rename. Restoring the immutable
    snapshots is repeatable, including after a second interruption during restore.
    No incomplete activation is opened to reconnecting clients.
    """
    from .maintenance import IndexDirectoryLease, MaintenanceGuard, _index_files
    from .service import stop_service

    install, data = session.install, session.data
    transaction_name = previous.get("transaction")
    if not transaction_name and previous.get("phase") == "preparing":
        session.safe = True
        return
    try:
        transaction = _target_path(transaction_name)
        if transaction.parent != install or not re.fullmatch(r"\.upgrade-[0-9a-f]{32}", transaction.name):
            raise ValueError("Invalid retained transaction location")
        journal = transaction / "transaction.json"
        record = json.loads(journal.read_text(encoding="utf-8"))
        if (record.get("product") != "data-search-upgrade" or record.get("install_dir") != str(install) or
                record.get("data_dir") != str(data)):
            raise ValueError("Retained transaction does not match the instance")
        phase = record["phase"]
        if phase in {"complete", "rolled_back"}:
            session.safe = True
            return
        old_cli = _target_path(record.get("old_cli", install / "runtime/data-search.exe"))
        if not old_cli.is_relative_to(install):
            raise ValueError("Previous executable is outside the installation")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

        def restart():
            if record.get("was_running"):
                result = runner([str(old_cli), "start", "--config", str(data / "config.json")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, **options)
                if result.returncode:
                    raise ValueError("Previous runtime was restored but could not restart")

        if phase in {"staging", "snapshot_complete", "failed_before_activation"}:
            assert_runtime_available(install, data)
            restart()
            record["phase"] = "failed_before_activation"
            atomic_json(journal, record)
            session.safe = True
            return
        if phase not in {"swapping", "activating", "rolling_back", "rollback_blocked", "restored"}:
            raise ValueError("Unknown interrupted upgrade phase")
        snapshot, app_snapshot, index_snapshot = (transaction / name for name in
            ("data-snapshot", "app-snapshot", "index-snapshot"))
        if not snapshot.is_dir() or not app_snapshot.is_dir():
            raise ValueError("Retained snapshots are missing")
        index = _target_path(record["index_dir"])
        if index != data and any(index.is_relative_to(path) or path.is_relative_to(index) for path in (data, install)):
            raise ValueError("Invalid external index snapshot destination")
        config_file = snapshot / "config.json"
        config = json.loads(config_file.read_text(encoding="utf-8-sig")) if record.get("had_config") else None
        if config is not None:
            if _target_path(config["data_dir"]) != data or _target_path(config.get("index_dir") or data) != index:
                raise ValueError("Snapshot configuration does not match the instance")
            config["config_path"] = str(data / "config.json")
        excluded = []
        for relative in record.get("excluded_data", []):
            path = _target_path(data / relative)
            if not path.is_relative_to(data):
                raise ValueError("Snapshot exclusion escaped the instance")
            excluded.append(path)
        excluded.extend(data / name for name in TRANSIENT_DATA)
        assert_runtime_available(install, data)
        # The incoming updater can stop either old or new daemon through its
        # authenticated instance endpoint even if the swapped CLI cannot start.
        stop_service(config or {"data_dir": str(data), "config_path": str(data / "config.json")})
        record["phase"] = "rolling_back"
        atomic_json(journal, record)
        session.update("rolling_back", transaction=transaction, safe=False)
        guard_config = _rollback_configuration(config, data, index)
        with ExitStack() as guards:
            guards.enter_context(MaintenanceGuard(guard_config) if guard_config else InstanceLock(data / "service.lock"))
            if guard_config:
                guards.enter_context(IndexDirectoryLease(guard_config, directory=index))
            _wait_runtime_free(install, data)
            runtime, old_runtime = install / "runtime", transaction / "previous-runtime"
            if old_runtime.exists():
                if runtime.exists():
                    _rename_runtime(runtime, transaction / ("failed-runtime-" + uuid.uuid4().hex))
                _rename_runtime(old_runtime, runtime)
            elif record.get("had_runtime"):
                # Either the first rename had not happened, or a previous
                # recovery restored the runtime before being interrupted again.
                if not (runtime / "data-search.exe").is_file() or _runtime_identity(runtime) != record.get("old_runtime_identity"):
                    raise ValueError("Cannot establish the identity of the previous runtime")
            elif runtime.exists():
                _rename_runtime(runtime, transaction / ("failed-runtime-" + uuid.uuid4().hex))
            _restore_files(snapshot, data, _files(data, tuple(excluded)))
            if index != data:
                if not index_snapshot.is_dir():
                    raise ValueError("Retained external index snapshot is missing")
                _restore_files(index_snapshot, index, _index_files(index))
            _restore_files(app_snapshot, install, _app_files(install))
            _restore_startup(record["startup_name"], record.get("startup_state"))
        record["phase"] = "restored"
        atomic_json(journal, record)
        restart()
        record["phase"] = "rolled_back"
        atomic_json(journal, record)
        session.safe = True
    except Exception as error:
        raise UpgradeCoordinationError("upgrade_recovery_required",
            "Interrupted upgrade could not be safely restored. Close remaining runtime users and retry this installer; keep the maintenance marker and retained transaction") from error


def upgrade_native(request: dict, *, runner=subprocess.run, copy=shutil.copy2, disk_usage=shutil.disk_usage):
    install, data, runtime = (_target_path(request[key]) for key in ("InstallDir", "DataDir", "RuntimeDir"))
    if install == data or install.is_relative_to(data) or data.is_relative_to(install):
        raise ValueError("Install and data directories must be independent")
    if runtime.is_relative_to(install):
        raise ValueError("Run the upgrade from a separately extracted release bundle")
    if runtime.is_relative_to(data) or data.is_relative_to(runtime) or install.is_relative_to(runtime):
        raise ValueError("The incoming runtime must be independent of the installation and its data")
    recovery = _recovery_identity(install, data)
    _validate_target(install, "install-manifest.json", {"product": "data-search", "install_dir": str(install), "data_dir": str(data)}, recovery=recovery)
    _validate_target(data, ".data-search-data.json", {"product": "data-search", "data_dir": str(data)}, recovery=recovery)
    verify_runtime(runtime)
    # Registration precedes native startup, including on a fresh DSH install.
    data.mkdir(parents=True, exist_ok=True)
    if not (data / ".data-search-data.json").exists() and not recovery:
        atomic_json(data / ".data-search-data.json", {"product": "data-search", "schema_version": 1, "data_dir": str(data)})
    with UpgradeSession(install, data, recover=lambda record, session:
            _recover_interrupted(record, session, runner=runner)) as session:
        assert_runtime_available(install, data)
        return _upgrade_native(request, session=session, runner=runner, copy=copy, disk_usage=disk_usage)


def _upgrade_native(request: dict, *, session, runner=subprocess.run, copy=shutil.copy2, disk_usage=shutil.disk_usage):
    from .maintenance import IndexDirectoryLease, MaintenanceGuard, _index_files
    install, data, runtime = (_target_path(request[key]) for key in ("InstallDir", "DataDir", "RuntimeDir"))
    if install == data or install.is_relative_to(data) or data.is_relative_to(install):
        raise ValueError("Install and data directories must be independent")
    if runtime.is_relative_to(install):
        raise ValueError("Run the upgrade from a separately extracted release bundle")
    _validate_target(install, "install-manifest.json", {"product": "data-search", "install_dir": str(install), "data_dir": str(data)})
    _validate_target(data, ".data-search-data.json", {"product": "data-search", "data_dir": str(data)})
    inventory = verify_runtime(runtime)
    config_path = data / "config.json"
    previous_config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else None
    if previous_config and Path(previous_config["data_dir"]).resolve() != data:
        raise ValueError("Configured data directory does not match the installation")
    excluded = [data / name for name in TRANSIENT_DATA]
    index = _target_path(previous_config.get('index_dir') or data) if previous_config else data
    if index != data and (index.is_relative_to(install) or install.is_relative_to(index) or index.is_relative_to(data) or data.is_relative_to(index)):
        raise ValueError("External index must be independent of the managed installation and data directories")
    index_files = _index_files(index) if index != data else []
    if any(path.is_symlink() or not path.is_file() for path in index_files):
        raise ValueError("External index snapshot refuses links and non-file artifacts")
    if previous_config:
        model = Path(previous_config["semantic"]["model_dir"]).resolve()
        if model == data or data.is_relative_to(model):
            raise ValueError("Model directory must not contain the index directory during upgrade")
        if model.is_relative_to(data):
            excluded.extend(_verified_model_files(model))
    excluded = tuple(excluded)
    data_files = _files(data, excluded)
    app_files = _app_files(install)
    required = sum((runtime / name).stat().st_size for name in inventory)
    required += sum(path.stat().st_size for path in data_files + app_files + index_files) + 64 * 1024 * 1024
    volume = install
    while not volume.exists():
        volume = volume.parent
    if disk_usage(volume).free < required:
        raise ValueError(f"Upgrade needs {required // 1048576 + 1} MiB free for staging and a complete index/configuration backup; existing service is unchanged")
    install.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    startup_name = "DataSearch-" + hashlib.sha256(str(install).lower().encode("utf-8")).hexdigest()[:12]
    startup_state = _startup_value(startup_name)
    if not (install / "install-manifest.json").exists():
        (install / "install-manifest.json").write_text(json.dumps({"product": "data-search", "schema_version": 1,
            "install_dir": str(install), "data_dir": str(data), "config": str(config_path),
            "cli": str(install / "runtime/data-search.exe"), "startup_name": startup_name, "autostart": "none"}), encoding="utf-8")
    if not (data / ".data-search-data.json").exists():
        (data / ".data-search-data.json").write_text(json.dumps({"product": "data-search", "schema_version": 1, "data_dir": str(data)}), encoding="utf-8")
    app_files = _app_files(install)
    transaction = install / (".upgrade-" + uuid.uuid4().hex)
    transaction.mkdir()
    stage, old_runtime = transaction / "staged-runtime", transaction / "previous-runtime"
    snapshot, app_snapshot = transaction / "data-snapshot", transaction / "app-snapshot"
    index_snapshot = transaction / "index-snapshot"
    snapshot.mkdir()
    app_snapshot.mkdir()
    if index != data:
        index_snapshot.mkdir()
    record = {"product": "data-search-upgrade", "phase": "staging", "install_dir": str(install),
        "data_dir": str(data), "index_dir": str(index), "backup_bytes": sum(path.stat().st_size for path in data_files + index_files),
        "startup_name": startup_name, "startup_state": startup_state,
        "excluded_data": [str(path.relative_to(data)) for path in excluded],
        "had_runtime": (install / "runtime").exists(), "had_config": previous_config is not None,
        "old_runtime_identity": _runtime_identity(install / "runtime") if (install / "runtime").is_dir() else None,
        "policy": "Immediate failed-start rollback only; no automatic later downgrade. Model files are excluded."}
    journal = transaction / "transaction.json"
    def mark(phase):
        record["phase"] = phase
        atomic_json(journal, record)
        session.update(phase, transaction=transaction, safe=phase in {
            "staging", "snapshot_complete", "complete", "rolled_back", "failed_before_activation"})
    mark("staging")
    # Copy and verify completely before stopping the previous service.
    _copy_files(_files(runtime), runtime, stage, copy)
    for name, digest in inventory.items():
        if _digest(stage / name) != digest:
            raise ValueError("Staged runtime integrity validation failed; existing service is unchanged")
    manifest_path = install / "install-manifest.json"
    old_cli = install / "runtime/data-search.exe"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        old_cli = Path(manifest.get("cli", install / "venv/Scripts/data-search.exe")).resolve()
        if not old_cli.is_relative_to(install):
            raise ValueError("Previous executable is outside the managed installation")
    new_cli = install / "runtime/data-search.exe"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    def command(args, *, check=True):
        result = runner([str(item) for item in args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, **options)
        with (transaction / "activation.log").open("a", encoding="utf-8") as log:
            log.write((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or ""))
        if check and result.returncode:
            raise RuntimeError("Installation command failed; see the retained upgrade transaction")
        return result
    old_existed = old_cli.is_file()
    was_running = old_existed and command([old_cli, "status", "--config", config_path], check=False).returncode == 0
    record.update(old_cli=str(old_cli), was_running=was_running)
    mark("staging")
    swapped = False
    snapshot_ready = False
    try:
        if old_existed:
            command([old_cli, "stop", "--config", config_path])
        with ExitStack() as guards:
            guards.enter_context(MaintenanceGuard(previous_config) if previous_config else InstanceLock(data / "service.lock"))
            if previous_config:
                guards.enter_context(IndexDirectoryLease(previous_config, directory=index))
            _wait_runtime_free(install, data)
            # Re-enumerate after stopping, including any final WAL/catalog writes.
            data_files = _files(data, excluded)
            index_files = _index_files(index) if index != data else []
            needed = sum(path.stat().st_size for path in data_files + app_files + index_files) + 64 * 1024 * 1024
            if disk_usage(install).free < needed:
                raise ValueError("Insufficient space for a consistent index backup after stopping")
            _copy_files(data_files, data, snapshot, copy)
            _copy_files(app_files, install, app_snapshot, copy)
            if index != data:
                _copy_files(index_files, index, index_snapshot, copy)
            snapshot_ready = True
            mark("snapshot_complete")
            mark("swapping")
            if new_cli.parent.exists():
                _rename_runtime(new_cli.parent, old_runtime)
            _rename_runtime(stage, new_cli.parent)
            swapped = True
            mark("activating")
        continuation = dict(request, RuntimeDir=str(new_cli.parent))
        request_path = transaction / "continuation.json"
        request_path.write_text(json.dumps(continuation, ensure_ascii=False), encoding="utf-8")
        command([request.get("PowerShell", "powershell.exe"), "-NoProfile", "-File", request["Installer"],
                 "-NativeTransactionChild", "-UpgradeRequest", request_path])
        command([new_cli, "status", "--config", config_path])
        mark("complete")
        return {"ok": True, "transaction": str(transaction), "backup_bytes": record["backup_bytes"],
                "rollback_snapshot_retained": True, "message": "Native installation passed health checks; previous runtime and pre-migration snapshot retained"}
    except Exception:
        if swapped or old_runtime.exists():
            mark("rolling_back")
            if new_cli.exists():
                command([new_cli, "stop", "--config", config_path], check=False)
            try:
                guard_config = _rollback_configuration(previous_config, data, index)
                with ExitStack() as guards:
                    guards.enter_context(MaintenanceGuard(guard_config) if guard_config else InstanceLock(data / "service.lock"))
                    if guard_config:
                        guards.enter_context(IndexDirectoryLease(guard_config, directory=index))
                    _wait_runtime_free(install, data)
                    if new_cli.parent.exists():
                        _rename_runtime(new_cli.parent, transaction / "failed-runtime")
                    if old_runtime.exists():
                        _rename_runtime(old_runtime, new_cli.parent)
                    if snapshot_ready:
                        _restore_files(snapshot, data, _files(data, excluded))
                        if index != data:
                            _restore_files(index_snapshot, index, _index_files(index))
                        current_app = _app_files(install)
                        _restore_files(app_snapshot, install, current_app)
                    _restore_startup(startup_name, startup_state)
                mark("restored")
            except Exception as rollback_error:
                mark("rollback_blocked")
                raise RuntimeError(f"Upgrade failed; automatic restore could not safely complete. Retained recovery snapshot: {transaction}") from rollback_error
        else:
            mark("failed_before_activation")
        if was_running:
            command([old_cli, "start", "--config", config_path])
        if record["phase"] == "restored":
            mark("rolled_back")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = upgrade_native(json.loads(args.request.read_text(encoding="utf-8-sig")))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({"schema_version": 1, "event": "installation_result", "ok": False,
            "error": {"code": getattr(error, "code", "installation_failed"), "message": str(error)[:700],
                      "processes": getattr(error, "details", [])}}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
