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
import shutil
import subprocess
import sys
import uuid

from .service import InstanceLock


APP_FILES = ("install-manifest.json", "mcp.json", "Settings.vbs", "launch-hidden.vbs", "uninstall.ps1", "plugin")
TRANSIENT_DATA = {"service.lock", "service.json", "daemon.log", "daemon.previous.log",
                  ".one-search-index.lock", "model-job/manager.lock", "model-job/worker.lock"}


def _validate_target(path, marker, expected):
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ValueError("Upgrade target must be a dedicated application directory")
    for ancestor in [path, *path.parents]:
        if ancestor.exists() and (ancestor.is_symlink() or (os.name == "nt" and ancestor.lstat().st_file_attributes & 0x400)):
            raise ValueError("Upgrade target must not use directory links")
    if not path.exists():
        return
    marker_path = path / marker
    if marker_path.exists():
        actual = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing managed directory marker does not match the upgrade target")
    elif any(path.iterdir()):
        raise ValueError("Upgrade refuses a nonempty unmanaged directory")


def _target_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    for ancestor in [path, *path.parents]:
        if ancestor.exists() and (ancestor.is_symlink() or (os.name == "nt" and ancestor.lstat().st_file_attributes & 0x400)):
            raise ValueError("Upgrade target must not use directory links")
    return path.resolve()


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
    # Every removal is an exact file within an explicitly managed target, never a shell glob.
    root = destination.resolve()
    for path in current_files:
        if not path.resolve().is_relative_to(root):
            raise ValueError("Rollback target escaped the managed directory")
        path.unlink()
    _copy_files(_files(source), source, destination)


def upgrade_native(request: dict, *, runner=subprocess.run, copy=shutil.copy2, disk_usage=shutil.disk_usage):
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
        "policy": "Immediate failed-start rollback only; no automatic later downgrade. Model files are excluded."}
    journal = transaction / "transaction.json"
    def mark(phase):
        record["phase"] = phase
        journal.write_text(json.dumps(record, indent=2), encoding="utf-8")
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
    swapped = False
    snapshot_ready = False
    try:
        if old_existed:
            command([old_cli, "stop", "--config", config_path])
        with ExitStack() as guards:
            guards.enter_context(MaintenanceGuard(previous_config) if previous_config else InstanceLock(data / "service.lock"))
            if previous_config:
                guards.enter_context(IndexDirectoryLease(previous_config, directory=index))
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
            if new_cli.parent.exists():
                new_cli.parent.rename(old_runtime)
            stage.rename(new_cli.parent)
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
            if new_cli.exists():
                command([new_cli, "stop", "--config", config_path], check=False)
            try:
                with ExitStack() as guards:
                    guards.enter_context(MaintenanceGuard(previous_config) if previous_config else InstanceLock(data / "service.lock"))
                    if previous_config:
                        guards.enter_context(IndexDirectoryLease(previous_config, directory=index))
                    if new_cli.parent.exists():
                        new_cli.parent.rename(transaction / "failed-runtime")
                    if old_runtime.exists():
                        old_runtime.rename(new_cli.parent)
                    if snapshot_ready:
                        _restore_files(snapshot, data, _files(data, excluded))
                        if index != data:
                            _restore_files(index_snapshot, index, _index_files(index))
                        current_app = _app_files(install)
                        _restore_files(app_snapshot, install, current_app)
                    _restore_startup(startup_name, startup_state)
                mark("rolled_back")
            except Exception as rollback_error:
                mark("rollback_blocked")
                raise RuntimeError(f"Upgrade failed; automatic restore could not safely complete. Retained recovery snapshot: {transaction}") from rollback_error
        else:
            mark("failed_before_activation")
        if was_running:
            command([old_cli, "start", "--config", config_path])
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
        print(json.dumps({"ok": False, "error": str(error)[:700]}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
