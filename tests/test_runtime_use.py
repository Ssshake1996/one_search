import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import venv

import psutil
import pytest

from data_search import runtime_use
from data_search.runtime_use import RuntimeInUseError, assert_runtime_available, inspect_runtime_users


class Process:
    def __init__(self, pid, exe, args, *, parent=1, name=None, denied=()):
        self.pid, self.path, self.args, self.parent = pid, str(exe), args, parent
        self.process_name = name or Path(exe).name
        self.denied = denied

    def _get(self, key, value):
        if key in self.denied:
            raise psutil.AccessDenied(self.pid)
        return value

    def name(self): return self._get("name", self.process_name)
    def ppid(self): return self._get("parent", self.parent)
    def exe(self): return self._get("exe", self.path)
    def cmdline(self): return self._get("args", self.args)


@pytest.fixture
def paths(tmp_path):
    install, data = tmp_path / "app", tmp_path / "data"
    (install / "runtime").mkdir(parents=True)
    (data / "model-job").mkdir(parents=True)
    config = data / "config.json"
    config.write_text(json.dumps({"data_dir": str(data), "semantic": {"model_dir": str(data / "model")}}))
    return install, data, config, install / "runtime/data-search.exe"


def inventory(monkeypatch, processes):
    monkeypatch.setattr(runtime_use.psutil, "process_iter", lambda: iter(processes))


def daemon_state(data, config, pid):
    (data / "service.json").write_text(json.dumps({"pid": pid, "config_path": str(config), "token": "DO-NOT-REPORT"}))


def test_mcp_settings_and_cli_block_without_leaking_arguments(paths, monkeypatch):
    install, data, config, exe = paths
    inventory(monkeypatch, [Process(21, exe, [str(exe), "mcp", "--config", str(config)]),
        Process(22, exe, [str(exe), "setup", "--config", str(config)]),
        Process(23, exe, [str(exe), "query", "--request", "PRIVATE_ROW_AND_PASSWORD"]),
        Process(24, exe, [str(exe), "search", "daemon"])])
    with pytest.raises(RuntimeInUseError) as caught:
        assert_runtime_available(install, data)
    assert caught.value.code == "runtime_in_use"
    details = caught.value.details
    assert [item["role"] for item in details] == ["mcp", "settings", "cli", "cli"]
    assert all(item["executable"] == "runtime/data-search.exe" for item in details)
    serialized = json.dumps(details)
    assert "PRIVATE_ROW" not in serialized and str(config) not in serialized and "DO-NOT-REPORT" not in serialized
    assert all(set(item) == {"pid", "parent_pid", "name", "role", "executable", "blocking", "reason"} for item in details)


def test_only_state_owned_daemon_and_workers_are_allowed(paths, monkeypatch):
    install, data, config, exe = paths
    daemon_state(data, config, 10)
    args = [str(exe), "--internal-module", "data_search", "daemon", "--config", str(config)]
    inventory(monkeypatch, [Process(9, exe, args), Process(10, exe, args, parent=9),
        Process(11, exe, [str(exe), "--internal-module", "data_search.worker"], parent=10),
        Process(12, exe, [str(exe), "mcp", "--config", str(config)], parent=10),
        Process(13, exe, args),
        Process(14, exe, [str(exe), "daemon", "--config", str(data / "other.json")])])
    result = inspect_runtime_users(install, data)
    assert {item["pid"] for item in result if not item["blocking"]} == {9, 10, 11}
    assert {item["pid"] for item in result if item["blocking"]} == {12, 13, 14}
    assert all(item["blocking"] for item in inspect_runtime_users(install, data, allow_service_workers=False))


def test_stale_or_other_instance_state_does_not_whitelist_daemon(paths, monkeypatch):
    install, data, config, exe = paths
    inventory(monkeypatch, [Process(10, exe, [str(exe), "daemon", "--config", str(config)])])
    daemon_state(data, data / "other.json", 10)
    assert inspect_runtime_users(install, data)[0]["blocking"]
    daemon_state(data, config, 10)
    config.write_text(json.dumps({"data_dir": str(data / "other")}))
    assert inspect_runtime_users(install, data)[0]["blocking"]


def test_model_allowance_requires_pid_job_config_and_model_identity(paths, monkeypatch):
    install, data, config, exe = paths
    state = {"pid": 30, "state": "running", "job_id": "job123", "model_dir": str(data / "model")}
    (data / "model-job/status.json").write_text(json.dumps(state))
    (data / "model-job/request.json").write_text(json.dumps({"job_id": "job123"}))
    args = [str(exe), "--internal-module", "data_search.model_manager", "worker", "--config", str(config), "--job", "job123"]
    inventory(monkeypatch, [Process(30, exe, args), Process(31, exe, args)])
    assert [item["blocking"] for item in inspect_runtime_users(install, data)] == [False, True]
    for changed in ({"job_id": "stale"}, {"model_dir": str(data / "another-model")}, {"state": "ready"}):
        (data / "model-job/status.json").write_text(json.dumps(state | changed))
        assert all(item["blocking"] for item in inspect_runtime_users(install, data))


def test_other_install_release_coordinator_and_shared_index_are_excluded(paths, monkeypatch):
    install, data, config, exe = paths
    processes = [Process(1, install.parent / "other-app/runtime/data-search.exe", ["data-search.exe", "mcp", "--config", str(config)]),
        Process(2, install.parent / "release/runtime/data-search.exe", ["data-search.exe", "--internal-module", "data_search.upgrade"]),
        Process(3, sys.executable, [sys.executable, "-m", "data_search", "mcp", "--config", str(config)]),
        Process(4, str(install) + "-other/runtime/data-search.exe", ["data-search.exe", "mcp"])]
    inventory(monkeypatch, processes)
    assert assert_runtime_available(install, data) == []


def test_identity_denial_fails_closed_for_possible_target(paths, monkeypatch):
    install, data, config, exe = paths
    inventory(monkeypatch, [Process(1, exe, [str(exe), "mcp"], denied={"args"}),
        Process(2, exe, [str(exe), "mcp"], denied={"exe", "args"}),
        Process(3, exe, [str(exe), "mcp"], denied={"name"}),
        Process(4, "system.exe", [], denied={"exe", "args"}),
        Process(5, exe, [str(exe), "mcp"], denied={"exe", "args", "parent"})])
    result = inspect_runtime_users(install, data)
    assert {item["pid"] for item in result} == {1, 2, 3, 5}
    assert all(item["blocking"] and item["reason"] == "identity_unavailable" for item in result)
    assert result[1]["executable"] is None


def test_exited_process_is_ignored(paths, monkeypatch):
    install, data, _, exe = paths
    process = Process(1, exe, [])
    def disappeared(): raise psutil.NoSuchProcess(1)
    process.cmdline = disappeared
    inventory(monkeypatch, [process])
    assert inspect_runtime_users(install, data) == []


def test_cli_reports_safe_actionable_error(paths, monkeypatch, capsys):
    install, data, _, exe = paths
    inventory(monkeypatch, [Process(1, exe, [str(exe), "mcp"])])
    assert runtime_use.main(["--install-dir", str(install), "--data-dir", str(data)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "runtime_in_use" and result["error"]["processes"][0]["role"] == "mcp"


def test_live_venv_mcp_process_is_detected_and_never_terminated(paths, tmp_path):
    install, data, config, _ = paths
    environment = install / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # This deliberately small stand-in keeps the real process/venv invocation
    # shape without opening any user daemon, model, database, or MCP connection.
    module = tmp_path / "fixture-module/data_search"
    module.mkdir(parents=True)
    (module / "__main__.py").write_text("import time\ntime.sleep(60)\n")
    child = subprocess.Popen([str(python), "-m", "data_search", "mcp", "--config", str(config)],
        cwd=module.parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        found = []
        while time.monotonic() < deadline and child.poll() is None:
            found = [item for item in inspect_runtime_users(install, data) if item["pid"] == child.pid]
            if found:
                break
            time.sleep(.05)
        assert found and found[0]["blocking"] and found[0]["role"] == "mcp"
        with pytest.raises(RuntimeInUseError):
            assert_runtime_available(install, data)
        assert child.poll() is None
    finally:
        child.terminate()
        child.communicate(timeout=10)
    assert not [item for item in inspect_runtime_users(install, data) if item["pid"] == child.pid]


@pytest.mark.skipif(os.name != "nt", reason="Windows executable locking")
def test_live_native_runtime_executable_lock_is_reported(paths):
    install, data, _, exe = paths
    source = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/ping.exe"
    shutil.copy2(source, exe)
    child = subprocess.Popen([str(exe), "-t", "127.0.0.1"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert child.poll() is None
        found = [item for item in inspect_runtime_users(install, data) if item["pid"] == child.pid]
        assert found and found[0]["blocking"] and found[0]["executable"] == "runtime/data-search.exe"
        with pytest.raises(PermissionError):
            with exe.open("wb"):
                pass
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not [item for item in inspect_runtime_users(install, data) if item["pid"] == child.pid]
