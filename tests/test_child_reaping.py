"""Detached workers remain our children until the creating process reaps them."""
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from data_search import model_manager, runtime, service
from data_search.config import defaults


@pytest.mark.parametrize("worker", ["daemon", "model"])
def test_long_lived_launcher_reaps_child_without_another_subprocess(tmp_path, monkeypatch, worker):
    config = defaults(str(tmp_path / "data"), [])
    config["config_path"] = str(tmp_path / "config.json")
    config["semantic"]["model_dir"] = str(tmp_path / "model")
    Path(config["config_path"]).write_text(json.dumps(config), encoding="utf-8")
    release = tmp_path / "release-child"
    command = [sys.executable, "-c",
               "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile not p.exists(): time.sleep(.01)",
               str(release)]
    children = []
    original_popen = subprocess.Popen

    def launch(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def status(_config):
        if not children:
            raise service.ServiceError("not running")
        return {"pid": children[0].pid, "service_id": "synthetic-test"}

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "process_command", lambda *args: command)
    monkeypatch.setattr(model_manager, "process_command", lambda *args: command)
    monkeypatch.setattr(service, "service_status", status)
    try:
        if worker == "daemon":
            result = service.start_service(config)
            assert result["started"]
        else:
            result = model_manager.start_model_job(config)
            assert not result["reused"]
        child = children[0]
        assert result["pid"] == child.pid and child.returncode is None
        # Simulate an external stop or independent completion. Do not poll/wait
        # the Popen or launch any more children: either can hide missing reaping.
        release.touch()
        deadline = time.monotonic() + 5
        while child.returncode is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert child.returncode == 0
        assert not psutil.pid_exists(child.pid)
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
            child.wait(timeout=5)
