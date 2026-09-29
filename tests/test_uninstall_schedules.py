import json
import os
from pathlib import Path
import subprocess

import pytest

from data_search.service import InstanceLock


@pytest.mark.skipif(os.name != "nt", reason="Windows uninstaller admission")
def test_uninstall_keeps_files_when_a_scheduled_launcher_holds_upgrade_lock(tmp_path):
    install = tmp_path / "app"
    data = tmp_path / "data"
    install.mkdir()
    data.mkdir()
    proof = install / "runtime-proof.txt"
    proof.write_text("must survive rejected uninstall", encoding="utf-8")
    manifest = {"product": "data-search", "schema_version": 1, "install_dir": str(install),
                "data_dir": str(data), "config": str(data / "config.json"), "startup_name": "one-search-test-do-not-touch"}
    (install / "install-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    uninstaller = Path(__file__).resolve().parents[1] / "scripts/uninstall.ps1"
    with InstanceLock(data / "upgrade.lock"):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                 "-ExecutionPolicy", "Bypass", "-File", str(uninstaller), "-InstallDir", str(install)],
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode != 0
    assert b"uninstall_busy" in result.stderr
    assert proof.read_text(encoding="utf-8") == "must survive rejected uninstall"
    assert data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows uninstaller admission")
def test_uninstall_keeps_files_if_schedule_cleanup_cannot_load_runtime(tmp_path):
    install = tmp_path / "app"
    data = tmp_path / "data"
    install.mkdir()
    data.mkdir()
    proof = install / "runtime-proof.txt"
    proof.write_text("retain", encoding="utf-8")
    manifest = {"product": "data-search", "schema_version": 1, "install_dir": str(install),
                "data_dir": str(data), "config": str(data / "config.json"), "startup_name": "one-search-test-do-not-touch"}
    (install / "install-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (data / "service-schedules.json").write_text('{"schema_version":1,"revision":0,"tasks":[]}', encoding="utf-8")
    uninstaller = Path(__file__).resolve().parents[1] / "scripts/uninstall.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                             "-ExecutionPolicy", "Bypass", "-File", str(uninstaller), "-InstallDir", str(install)],
                            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode != 0 and b"schedule_cleanup_unavailable" in result.stderr
    assert proof.read_text(encoding="utf-8") == "retain"
    assert data.exists()
