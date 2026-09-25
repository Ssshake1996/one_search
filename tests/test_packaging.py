import importlib.util
import gc
import json
import os
from pathlib import Path
import sys
import time

import pytest

from data_search import runtime
from data_search.config import defaults
from data_search.setup_ui import settings_config


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_and_python_subprocess_commands(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime.process_command("data_search.worker") == [sys.executable, "-m", "data_search.worker"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert runtime.process_command("data_search", "daemon", "--config", "a b.json") == [
        sys.executable, "--internal-module", "data_search", "daemon", "--config", "a b.json"]


def test_native_pool_limits_are_bounded(monkeypatch):
    variables = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    for variable in variables:
        monkeypatch.setenv(variable, "99")
    runtime.configure_native_threads(20)
    assert all(os.environ[variable] == "2" for variable in variables)
    runtime.configure_native_threads(0)
    assert all(os.environ[variable] == "1" for variable in variables)


def test_bundle_rejects_wrong_python_before_install(tmp_path):
    builder, checker = script("build_release"), script("check_runtime")
    manifest = {"kind": "python-bootstrap", "runtime": builder.runtime_metadata()}
    (tmp_path / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest))
    checker.check(tmp_path)
    manifest["runtime"]["python"] = "0.0"
    (tmp_path / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="requires python=0.0"):
        checker.check(tmp_path)


def test_release_checksum_manifest_matches_content(tmp_path):
    builder = script("build_release")
    bundle = tmp_path / "release"
    bundle.mkdir()
    payload = bundle / "payload.txt"
    payload.write_text("portable installation")
    archive, manifest = builder.finalize(bundle, tmp_path, {"version": "test"})
    assert archive.is_file() and manifest.is_file()
    checksums = json.loads((bundle / "SHA256SUMS.json").read_text())
    assert checksums["payload.txt"] == builder.digest(payload)
    assert checksums["RELEASE_MANIFEST.json"] == builder.digest(manifest)
    builder.finalize(bundle, tmp_path, {"version": "test", "docs_refreshed": True})
    refreshed = json.loads((bundle / "SHA256SUMS.json").read_text())
    assert "SHA256SUMS.json" not in refreshed
    assert refreshed["RELEASE_MANIFEST.json"] == builder.digest(manifest)


def test_settings_change_scope_retains_unedited_budgets_and_database_tls(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    config = defaults(str(tmp_path / "data"), [str(root)])
    config["resource"]["memory_mb"] = 555
    db = {"id": "pg", "kind": "postgres", "ssl": {"sslmode": "verify-full"},
          "allowed_tables": ["public.notes"], "allowed_columns": {"public.notes": ["id", "body"]}}
    values = {"scope": "machine", "roots": [str(root)], "databases": [db],
              "semantic_enabled": False, "indexing": dict(config["indexing"])}
    result = settings_config(config, values)
    assert result["scope"] == "machine" and result["roots"] == []
    assert result["resource"]["memory_mb"] == 555
    assert result["databases"][0]["ssl"]["sslmode"] == "verify-full"
    assert config["roots"] == [str(root)]
    values["scope"] = "directories"
    values["roots"] = []
    with pytest.raises(ValueError, match="Choose at least"):
        settings_config(config, values)


def test_settings_window_save_restarts_with_selected_scope(tmp_path, monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from data_search import service, setup_ui
    root = tmp_path / "docs"
    root.mkdir()
    path = tmp_path / "config.json"
    original = defaults(str(tmp_path / "data"), [str(root)])
    original["semantic"]["enabled"] = False
    original["resource"]["memory_mb"] = 640
    path.write_text(json.dumps(original), encoding="utf-8")
    try:
        window = tkinter.Tk()
    except tkinter.TclError as error:
        pytest.skip(f"No GUI display is available: {error}")
    window.withdraw()
    calls, errors = [], []
    monkeypatch.setattr(service, "stop_service", lambda config: calls.append(("stop", config["scope"])))
    monkeypatch.setattr(service, "start_service", lambda config: calls.append(("start", config["scope"])) or {"started": True})
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *args, **kwargs: errors.append(args))
    monkeypatch.setattr(tkinter, "Tk", lambda: window)
    def exercise():
        assert not calls, "Opening settings must not start indexing"
        assert json.loads(path.read_text(encoding="utf-8")) == original
        pending, buttons = [window], {}
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if "text" in widget.keys():
                buttons[widget.cget("text")] = widget
        assert "保存并启动" in buttons and "暂停索引" in buttons and "添加数据库…" in buttons
        machine = next(widget for label, widget in buttons.items() if label.startswith("整个电脑"))
        machine.invoke()
        buttons["保存并启动"].invoke()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(calls) < 2:
            window.update()
            time.sleep(.01)
        assert not errors
        assert calls == [("stop", "directories"), ("start", "machine")]
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["scope"] == "machine" and reloaded["roots"] == []
        assert reloaded["resource"]["memory_mb"] == 640
    monkeypatch.setattr(window, "mainloop", exercise)
    try:
        assert setup_ui.main(["--config", str(path)]) == 0
    finally:
        window.destroy()
        # Tk variables captured by callbacks must be released on this thread,
        # before another service-test worker happens to trigger cyclic GC.
        gc.collect()
