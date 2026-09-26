"""Run the actual Windows installer preflight without installing any packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap installer")
INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install.ps1"


@pytest.fixture
def installation(tmp_path):
    install, data = tmp_path / "app with spaces", tmp_path / "data"
    (install / "venv/Scripts").mkdir(parents=True)
    (install / "runtime").mkdir()
    data.mkdir()
    (install / "install-manifest.json").write_text(json.dumps({"product": "data-search", "schema_version": 1,
        "install_dir": str(install), "data_dir": str(data), "version": "old-version"}))
    (data / ".data-search-data.json").write_text(json.dumps({"product": "data-search", "schema_version": 1, "data_dir": str(data)}))
    (data / "config.json").write_text('{"preserve":"original configuration"}')
    return install, data


def snapshot(install, data):
    return {str(path): path.read_bytes() for directory in (install, data) for path in directory.rglob("*") if path.is_file()}


def run_installer(tmp_path, install, data, processes=None):
    request = {"installer": str(INSTALLER), "install": str(install), "data": str(data), "python": sys.executable,
        # The guard runs before root validation. Reaching this deliberately absent
        # root proves unrelated processes were excluded, without starting an install.
        "root": str(tmp_path / "deliberately-missing-root")}
    if processes is not None:
        request["processes"] = processes
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    wrapper = tmp_path / "invoke.ps1"
    wrapper.write_text("""param([string]$Request)
$r = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
if ($r.PSObject.Properties['processes']) {
    $global:TestProcesses = @($r.processes)
    function Get-CimInstance {
        [CmdletBinding()] param([string]$ClassName, [string[]]$Property)
        if ($ClassName -ne 'Win32_Process') { throw 'Unexpected process query' }
        return $global:TestProcesses
    }
}
& $r.installer -InstallDir $r.install -DataDir $r.data -Python $r.python -Root @($r.root) -SkipModel -NoAutostart
exit $LASTEXITCODE
""", encoding="utf-8")
    shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert shell
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(wrapper), "-Request", str(request_path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"')]
    assert result.returncode != 0 and records, (result.stdout, result.stderr)
    return records[-1], result


def process(pid, name, exe, command):
    return {"ProcessId": pid, "Name": name, "ExecutablePath": str(exe) if exe else None, "CommandLine": command}


def test_bootstrap_maintenance_marker_rejects_before_any_installation_change(tmp_path, installation):
    install, data = installation
    (data / "upgrade-state.json").write_text('{"transaction_id":"fixture"}')
    before = snapshot(install, data)
    report, _ = run_installer(tmp_path, install, data, [])
    assert report["stage"] == "preflight" and report["error"]["code"] == "upgrade_in_progress"
    assert snapshot(install, data) == before


@pytest.mark.parametrize("base_image", [False, True])
def test_bootstrap_active_runtime_rejects_before_stop_manifest_or_venv_write(tmp_path, installation, base_image):
    install, data = installation
    launcher = install / "venv/Scripts/python.exe"
    exe = Path(sys.executable) if base_image else launcher
    before = snapshot(install, data)
    report, result = run_installer(tmp_path, install, data, [process(821, "python.exe", exe,
        f'"{launcher}" -m data_search mcp --config "{data / "config.json"}" --private SECRET-NOT-OUTPUT')])
    assert report["stage"] == "preflight" and report["error"]["code"] == "runtime_in_use"
    assert report["error"]["processes"] == [{"pid": 821, "name": "python.exe"}]
    assert "Stop the MCP host" in report["error"]["message"]
    assert "SECRET-NOT-OUTPUT" not in result.stdout + result.stderr
    assert snapshot(install, data) == before


def test_bootstrap_excludes_other_installations_and_independent_incoming_python(tmp_path, installation):
    install, data = installation
    incoming = Path(sys.executable)
    neighbor = Path(str(install) + "-other") / "venv/Scripts/python.exe"
    foreign = tmp_path / "another/runtime/data-search.exe"
    before = snapshot(install, data)
    report, _ = run_installer(tmp_path, install, data, [
        process(821, "python.exe", incoming, f'"{incoming}" "installer.py" --config "{data / "config.json"}"'),
        process(822, "python.exe", incoming, f'"{neighbor}" -m data_search mcp'),
        process(823, "data-search.exe", foreign, f'"{foreign}" mcp --config "{data / "config.json"}"')])
    assert report["error"]["code"] == "installation_failed", report
    assert snapshot(install, data) == before


def test_bootstrap_unknown_possible_runtime_identity_fails_closed(tmp_path, installation):
    install, data = installation
    before = snapshot(install, data)
    report, _ = run_installer(tmp_path, install, data, [process(821, "python.exe", None, None)])
    assert report["error"]["code"] == "runtime_in_use"
    assert report["error"]["processes"] == [{"pid": 821, "name": "python.exe"}]
    assert snapshot(install, data) == before


def test_bootstrap_real_executable_remains_alive_and_installation_unchanged(tmp_path, installation):
    install, data = installation
    exe = install / "runtime/data-search.exe"
    shutil.copy2(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/ping.exe", exe)
    before = snapshot(install, data)
    child = subprocess.Popen([str(exe), "-t", "127.0.0.1"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        report, _ = run_installer(tmp_path, install, data)
        assert report["error"]["code"] == "runtime_in_use"
        assert child.pid in {item["pid"] for item in report["error"]["processes"]}
        assert child.poll() is None
        assert snapshot(install, data) == before
    finally:
        child.terminate()
        child.wait(timeout=10)
