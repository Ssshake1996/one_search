from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from data_search.config import defaults
from data_search import preflight, setup_ui, upgrade
from data_search.host_integration import register_mcp


def database(tmp_path):
    path = tmp_path / "资料.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT, updated INTEGER NOT NULL)")
        connection.execute("INSERT INTO notes VALUES(1, 'private row content', 5)")
        connection.execute("CREATE TABLE duplicate_keys(id INTEGER, body TEXT)")
        connection.execute("CREATE TABLE null_keys(id TEXT UNIQUE, body TEXT)")
        connection.execute("INSERT INTO null_keys VALUES(NULL, 'test')")
    return {"id": "local", "kind": "sqlite", "path": str(path), "allowed_tables": ["notes"],
        "allowed_columns": {"notes": ["id", "body", "updated"]},
        "index": [{"table": "notes", "id_column": "id", "text_columns": ["body"], "updated_column": "updated"}]}


def test_database_preflight_is_read_only_and_never_returns_rows(tmp_path):
    source = database(tmp_path)
    before = Path(source["path"]).read_bytes()
    result = preflight.check_database(source)
    assert result["ok"]
    assert {item["name"] for item in result["checks"]} == {"connection", "columns", "stable_key"}
    assert "private row content" not in json.dumps(result)
    assert Path(source["path"]).read_bytes() == before


@pytest.mark.parametrize("problem", ["missing_column", "duplicate_key", "null_key", "missing_environment"])
def test_preflight_rejects_invalid_sources_without_activation(tmp_path, problem):
    source = database(tmp_path)
    if problem == "missing_column":
        source["allowed_columns"]["notes"].append("missing")
    elif problem in {"duplicate_key", "null_key"}:
        table = "duplicate_keys" if problem == "duplicate_key" else "null_keys"
        source.update(allowed_tables=[table], allowed_columns={table: ["id", "body"]},
            index=[{"table": table, "id_column": "id", "text_columns": ["body"]}])
    else:
        source["password_env"] = "ONE_SEARCH_TEST_MISSING_SECRET_X"
    result = preflight.check_database(source)
    assert not result["ok"]
    assert not result["checks"][-1]["ok"]


def test_preflight_deadline_terminates_child(monkeypatch):
    monkeypatch.setattr(preflight, "process_command", lambda *_: [sys.executable, "-c", "import time; time.sleep(10)"])
    started = time.monotonic()
    report = preflight.check_database({"id": "slow"}, timeout_seconds=.1)
    assert not report["ok"] and "超时" in report["checks"][0]["message"]
    assert time.monotonic() - started < 3


def test_unchecked_database_change_preserves_running_config(tmp_path, monkeypatch):
    current = defaults(str(tmp_path / "data"), [])
    path = tmp_path / "config.json"
    original = json.dumps(current).encode()
    path.write_bytes(original)
    candidate = deepcopy(current)
    candidate["databases"] = [database(tmp_path)]
    calls = []
    monkeypatch.setattr("data_search.service.stop_service", lambda *_: calls.append("stop"))
    with pytest.raises(ValueError, match="测试数据库"):
        setup_ui.activate_settings(path, current, candidate)
    assert not calls and path.read_bytes() == original
    # Even a previously successful test cannot authorize changed fields.
    tested = preflight.database_fingerprint(candidate["databases"])
    candidate["databases"][0]["allowed_columns"]["notes"].append("other")
    with pytest.raises(ValueError):
        setup_ui.activate_settings(path, current, candidate, tested)
    assert not calls and path.read_bytes() == original


def test_settings_start_failure_restores_previous_config(tmp_path, monkeypatch):
    current = defaults(str(tmp_path / "data"), [])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(current), encoding="utf-8")
    candidate = deepcopy(current)
    candidate["resource"]["memory_mb"] = 777
    calls = []
    monkeypatch.setattr("data_search.service.stop_service", lambda config: calls.append(("stop", config["resource"]["memory_mb"])))
    def start(config):
        value = config["resource"]["memory_mb"]
        calls.append(("start", value))
        if value == 777:
            raise RuntimeError("injected start failure")
    monkeypatch.setattr("data_search.service.start_service", start)
    with pytest.raises(RuntimeError, match="injected"):
        setup_ui.activate_settings(path, current, candidate)
    assert json.loads(path.read_text()) == current
    assert calls == [("stop", 1024), ("start", 777), ("stop", 777), ("start", 1024)]


def test_status_presents_known_queues_errors_and_budgets():
    result = dict(setup_ui.status_rows({"index": {"paused": False, "last_error": "disk_budget_exceeded",
        "coverage": {"documents": {"pending": 7, "ready": 4, "budget": 2}, "embedded_chunks": 5,
                     "semantic_eligible_chunks": 9, "source_errors": {"local": "offline"}},
        "scheduler": {"queued_directories": 3, "queued_files": 8, "queued_events": 2},
        "resources": {"rss_mb": 12, "available_mb": 100, "disk_mb": 40, "free_disk_mb": 800}}}))
    assert result["正文待处理"] == "7" and result["受预算限制"] == "2"
    assert "待处理 4" in result["语义片段"] and result["来源错误 · local"] == "offline"
    assert result["最近错误 / 暂停原因"] == "disk_budget_exceeded"
    assert result["等待发现的目录"] == "3" and result["持久化正文任务"] == "8"


def native_fixture(tmp_path):
    install, data, bundle = (tmp_path / name for name in ("app", "data", "bundle"))
    (install / "runtime").mkdir(parents=True)
    data.mkdir()
    (bundle / "runtime/lib").mkdir(parents=True)
    (bundle / "runtime/data-search.exe").write_bytes(b"new runtime")
    (bundle / "runtime/lib/library.dll").write_bytes(b"new library")
    checksums = {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (bundle / "runtime").rglob("*") if path.is_file()}
    (bundle / "SHA256SUMS.json").write_text(json.dumps(checksums))
    (install / "runtime/data-search.exe").write_bytes(b"previous runtime")
    (install / "mcp.json").write_text('{"preserved":true}')
    (install / "install-manifest.json").write_text(json.dumps({"product": "data-search", "schema_version": 1,
        "install_dir": str(install), "data_dir": str(data), "cli": str(install / "runtime/data-search.exe")}))
    (data / ".data-search-data.json").write_text(json.dumps({"product": "data-search", "data_dir": str(data)}))
    config = defaults(str(data), [])
    config["semantic"]["model_dir"] = str(data / "models")
    (data / "config.json").write_text(json.dumps(config))
    (data / "index.sqlite3").write_bytes(b"consistent previous sqlite image")
    (data / "segments").mkdir()
    (data / "segments/catalog.json").write_text('{"generation":1}')
    (data / "models").mkdir()
    (data / "models/model.onnx").write_bytes(b"model excluded from snapshot")
    request = {"InstallDir": str(install), "DataDir": str(data), "RuntimeDir": str(bundle / "runtime"),
        "Installer": str(bundle / "scripts/install.ps1"), "NoAutostart": True}
    return request, install, data


def test_native_upgrade_low_disk_and_copy_failure_leave_service_unchanged(tmp_path):
    request, install, data = native_fixture(tmp_path)
    calls = []
    runner = lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0)
    with pytest.raises(ValueError, match="free"):
        upgrade.upgrade_native(request, runner=runner, disk_usage=lambda _: SimpleNamespace(free=0))
    assert not calls and not list(install.glob(".upgrade-*"))
    def fail_copy(source, target):
        raise OSError("injected copy failure")
    with pytest.raises(OSError, match="injected"):
        upgrade.upgrade_native(request, runner=runner, copy=fail_copy)
    assert not calls
    assert (install / "runtime/data-search.exe").read_bytes() == b"previous runtime"
    assert (data / "index.sqlite3").read_bytes() == b"consistent previous sqlite image"


def test_native_upgrade_bad_bundle_is_rejected_before_stop(tmp_path):
    request, install, data = native_fixture(tmp_path)
    Path(request["RuntimeDir"], "lib/library.dll").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="integrity"):
        upgrade.upgrade_native(request, runner=lambda *_a, **_k: pytest.fail("must not run service"))
    assert (install / "runtime/data-search.exe").read_bytes() == b"previous runtime"


def test_native_upgrade_start_failure_restores_runtime_index_and_configuration(tmp_path):
    request, install, data = native_fixture(tmp_path)
    before = {path.relative_to(data): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    calls = []
    def runner(args, **_kwargs):
        calls.append(args)
        if "-NativeTransactionChild" in args:
            (data / "index.sqlite3").write_bytes(b"new incompatible migration")
            (data / "segments/catalog.json").write_text('{"generation":2}')
            (data / "segments/new.usearch").write_bytes(b"new segment")
            (data / "config.json").write_text('{"broken":true}')
            (install / "mcp.json").write_text('{"changed":true}')
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    with pytest.raises(RuntimeError, match="Installation command"):
        upgrade.upgrade_native(request, runner=runner)
    assert (install / "runtime/data-search.exe").read_bytes() == b"previous runtime"
    assert (install / "mcp.json").read_text() == '{"preserved":true}'
    assert all((data / name).read_bytes() == raw for name, raw in before.items())
    assert not (data / "segments/new.usearch").exists()
    assert calls[-1][1] == "start"
    transaction = next(install.glob(".upgrade-*"))
    assert json.loads((transaction / "transaction.json").read_text())["phase"] == "rolled_back"
    # A directory called models is not proof that its contents are immutable.
    assert (transaction / "data-snapshot/models/model.onnx").read_bytes() == before[Path("models/model.onnx")]


def test_upgrade_excludes_only_verified_model_assets(tmp_path, monkeypatch):
    from data_search import model
    root = tmp_path / "model"
    root.mkdir()
    asset = root / "model.onnx"
    asset.write_bytes(b"pinned model fixture")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"model_id": model.MODEL_ID}))
    unknown = root / "new-catalog.sqlite3"
    unknown.write_bytes(b"must be backed up")
    monkeypatch.setattr(model, "SHA256", {"model.onnx": hashlib.sha256(asset.read_bytes()).hexdigest()})
    excluded = upgrade._verified_model_files(root)
    assert set(excluded) == {asset, manifest}
    assert upgrade._files(root, tuple(excluded)) == [unknown]
    asset.write_bytes(b"changed model")
    assert upgrade._verified_model_files(root) == []


def test_native_success_retains_complete_pre_migration_snapshot(tmp_path):
    request, install, data = native_fixture(tmp_path)
    result = upgrade.upgrade_native(request, runner=lambda *a, **k: SimpleNamespace(returncode=0))
    assert result["ok"] and result["rollback_snapshot_retained"]
    transaction = Path(result["transaction"])
    assert (transaction / "previous-runtime/data-search.exe").read_bytes() == b"previous runtime"
    assert (transaction / "data-snapshot/segments/catalog.json").read_text() == '{"generation":1}'
    assert (install / "runtime/data-search.exe").read_bytes() == b"new runtime"


def test_native_backup_failure_restarts_unchanged_old_service(tmp_path):
    request, install, data = native_fixture(tmp_path)
    calls = []
    def copy(source, target):
        if source.name == "index.sqlite3":
            raise OSError("injected backup failure")
        return shutil.copy2(source, target)
    with pytest.raises(OSError, match="backup"):
        upgrade.upgrade_native(request, copy=copy,
            runner=lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    assert calls[-1][1] == "start"
    assert (install / "runtime/data-search.exe").read_bytes() == b"previous runtime"
    assert (data / "index.sqlite3").read_bytes() == b"consistent previous sqlite image"


def test_native_rollback_refuses_to_overwrite_locked_data(tmp_path):
    request, install, data = native_fixture(tmp_path)
    held_lock = upgrade.InstanceLock(data / "service.lock")
    def runner(args, **kwargs):
        if "-NativeTransactionChild" in args:
            (data / "index.sqlite3").write_bytes(b"live database must not be overwritten")
            held_lock.__enter__()
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    try:
        with pytest.raises(RuntimeError, match="could not safely complete"):
            upgrade.upgrade_native(request, runner=runner)
        assert (data / "index.sqlite3").read_bytes() == b"live database must not be overwritten"
        transaction = next(install.glob(".upgrade-*"))
        assert json.loads((transaction / "transaction.json").read_text())["phase"] == "rollback_blocked"
        assert (transaction / "data-snapshot/index.sqlite3").read_bytes() == b"consistent previous sqlite image"
    finally:
        held_lock.__exit__()


def test_explicit_host_registration_preserves_other_settings_and_backups(tmp_path):
    path = tmp_path / "host.json"
    existing = {"theme": "dark", "mcpServers": {"other": {"command": "other.exe", "env": {"TOKEN": "retained-private-value"}}}}
    original = json.dumps(existing).encode()
    path.write_bytes(original)
    entry = {"command": sys.executable, "args": ["-m", "data_search", "mcp", "--config", "config.json"]}
    result = register_mcp(path, entry)
    assert Path(result["backup"]).read_bytes() == original
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["theme"] == existing["theme"] and updated["mcpServers"]["other"] == existing["mcpServers"]["other"]
    assert updated["mcpServers"]["data-search"] == entry
    assert "retained-private-value" not in json.dumps(result)
    assert not register_mcp(path, entry)["changed"]
    different = {"command": sys.executable, "args": ["changed"]}
    with pytest.raises(ValueError, match="replace"):
        register_mcp(path, different)
    register_mcp(path, different, replace=True)
    assert json.loads(path.read_text())["mcpServers"]["data-search"] == different


def test_failed_host_atomic_write_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "host.json"
    path.write_bytes(b'{"other":42}')
    monkeypatch.setattr("data_search.host_integration.os.replace", lambda *_: (_ for _ in ()).throw(OSError("injected replacement failure")))
    with pytest.raises(OSError):
        register_mcp(path, {"command": sys.executable, "args": []})
    assert path.read_bytes() == b'{"other":42}'
    assert next(tmp_path.glob("*.bak")).read_bytes() == path.read_bytes()


def test_host_registration_requires_known_json_shape_and_explicit_path(tmp_path):
    entry = {"command": sys.executable, "args": []}
    with pytest.raises(ValueError, match="absolute"):
        register_mcp("relative.json", entry)
    path = tmp_path / "cordis.patch.yml"
    path.write_text('[{"name":"some-plugin"}]')
    with pytest.raises(ValueError, match="mcpServers"):
        register_mcp(path, entry)


@pytest.mark.skipif(os.name != "nt", reason="Windows installer continuation")
def test_powershell_native_continuation_on_synthetic_source(tmp_path):
    """Exercise the real PS installer with a relocatable venv console launcher.

    This is not the Python-free native smoke test; that runs on final frozen assets.
    """
    launcher = Path(sys.executable).with_name("data-search.exe")
    if not launcher.is_file():
        pytest.skip("The active environment has no installed console launcher")
    install, data, bundle, docs = (tmp_path / name for name in ("app", "data", "bundle", "synthetic docs"))
    runtime = bundle / "runtime"
    runtime.mkdir(parents=True)
    docs.mkdir()
    (docs / "test.txt").write_text("A bounded installer verification sample", encoding="utf-8")
    shutil.copy2(launcher, runtime / "data-search.exe")
    (bundle / "SHA256SUMS.json").write_text(json.dumps({"runtime/data-search.exe": upgrade._digest(runtime / "data-search.exe")}))
    request = {"InstallDir": str(install), "DataDir": str(data), "RuntimeDir": str(runtime),
        "Installer": str(Path(__file__).resolve().parents[1] / "scripts/install.ps1"), "Root": [str(docs)],
        "ModelDir": "", "SkipModel": True, "NoAutostart": True,
        "PowerShell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}
    try:
        first = upgrade.upgrade_native(request)
        assert first["ok"]
        saved = json.loads((data / "config.json").read_text(encoding="utf-8-sig"))
        assert saved["scope"] == "directories" and saved["roots"] == [str(docs)]
        request["Root"] = [str(tmp_path)]
        second = upgrade.upgrade_native(request)
        assert second["ok"]
        assert json.loads((data / "config.json").read_text(encoding="utf-8-sig")) == saved
        assert (Path(second["transaction"]) / "data-snapshot/index.sqlite3").is_file()
    finally:
        if (install / "runtime/data-search.exe").is_file() and (data / "config.json").is_file():
            subprocess.run([str(install / "runtime/data-search.exe"), "stop", "--config", str(data / "config.json")],
                capture_output=True, timeout=30, check=True)
