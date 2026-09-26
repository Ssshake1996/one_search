"""Recovery regressions found by reviewing interrupted installer writes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_search import upgrade
from data_search.config import atomic_json, defaults
from data_search.runtime_use import RuntimeInUseError
from data_search.upgrade_hosts import UpgradeCoordinationError, UpgradeSession
from test_upgrade_lifecycle import fixture


@pytest.fixture(autouse=True)
def no_unrelated_process_inventory(monkeypatch):
    # These regressions exercise retained files and admission state. Real
    # ownership/locking is covered by test_runtime_use and installer smoke tests.
    monkeypatch.setattr(upgrade, "assert_runtime_available", lambda *args, **kwargs: [])


def success(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def interrupted_activation(tmp_path):
    config, request = fixture(tmp_path)

    def terminate(args, **kwargs):
        if "-NativeTransactionChild" in args:
            raise KeyboardInterrupt("updater terminated during activation")
        return success()

    with pytest.raises(KeyboardInterrupt):
        upgrade.upgrade_native(request, runner=terminate)
    install, data = Path(request["InstallDir"]), Path(request["DataDir"])
    journal = next(install.glob(".upgrade-*/transaction.json"))
    assert json.loads(journal.read_text())["phase"] == "activating"
    assert (data / "upgrade-state.json").exists()
    return config, request, journal


@pytest.mark.parametrize("phase", ["activating", "rolling_back"])
@pytest.mark.parametrize("identity", ["install", "data"])
@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_retry_recovers_instance_identity_from_validated_snapshots(tmp_path, phase, identity, damage):
    config, request, journal = interrupted_activation(tmp_path)
    install, data = Path(request["InstallDir"]), Path(request["DataDir"])
    record = json.loads(journal.read_text())
    record["phase"] = phase
    atomic_json(journal, record)
    marker = json.loads((data / "upgrade-state.json").read_text())
    marker["phase"] = phase
    atomic_json(data / "upgrade-state.json", marker)
    identity_path = install / "install-manifest.json" if identity == "install" else data / ".data-search-data.json"
    if damage == "missing":
        identity_path.unlink()
    else:
        identity_path.write_text('{"product":')
    # Native PowerShell's Write-Json can truncate an identity file; an updater
    # terminated again during old remove/copy restoration can leave it absent.
    restarted = []

    def retry(args, **kwargs):
        if args[1:2] == ["start"]:
            restarted.append(True)
            assert (install / "runtime/data-search.exe").read_bytes() == b"old executable"
            assert json.loads((data / "config.json").read_text())["data_dir"] == str(data)
        return success()

    result = upgrade.upgrade_native(request, runner=retry)
    assert result["ok"] and restarted
    assert not (data / "upgrade-state.json").exists()
    assert json.loads(journal.read_text())["phase"] == "rolled_back"


@pytest.mark.parametrize("identity", ["install", "data"])
def test_missing_identity_never_authorizes_recovery_from_mismatched_snapshot(tmp_path, identity):
    _, request, journal = interrupted_activation(tmp_path)
    install, data = Path(request["InstallDir"]), Path(request["DataDir"])
    current = install / "install-manifest.json" if identity == "install" else data / ".data-search-data.json"
    snapshot = journal.parent / ("app-snapshot/install-manifest.json" if identity == "install" else "data-snapshot/.data-search-data.json")
    current.unlink()
    saved = json.loads(snapshot.read_text())
    saved["data_dir"] = str(tmp_path / "another-instance")
    atomic_json(snapshot, saved)
    marker_before = (data / "upgrade-state.json").read_bytes()
    runtime_before = (install / "runtime/data-search.exe").read_bytes()
    with pytest.raises((ValueError, UpgradeCoordinationError)):
        upgrade.upgrade_native(request, runner=lambda *a, **k: pytest.fail("Unverified identity must not run a runtime command"))
    assert (data / "upgrade-state.json").read_bytes() == marker_before
    assert (install / "runtime/data-search.exe").read_bytes() == runtime_before


@pytest.mark.parametrize("invalid", [{"schema_version": 99}, {"transaction_id": None}, {"transaction_id": ""}])
def test_structurally_invalid_maintenance_marker_is_preserved(tmp_path, invalid):
    install, data = tmp_path / "app", tmp_path / "data"
    marker = data / "upgrade-state.json"
    atomic_json(marker, {"schema_version": 1, "transaction_id": "valid-fixture", "install_dir": str(install),
        "data_dir": str(data), "phase": "preparing", **invalid})
    before = marker.read_bytes()
    with pytest.raises(UpgradeCoordinationError) as caught:
        with UpgradeSession(install, data, recover=upgrade._recover_interrupted):
            pytest.fail("Invalid maintenance records cannot grant runtime admission")
    assert caught.value.code == "upgrade_recovery_required"
    assert marker.read_bytes() == before


@pytest.mark.parametrize("crash", [False, True])
def test_first_install_failure_quiesces_its_new_model_job_before_restore(tmp_path, monkeypatch, crash):
    _, old_request = fixture(tmp_path)
    install, data = tmp_path / "fresh-app", tmp_path / "fresh-data"
    request = old_request | {"InstallDir": str(install), "DataDir": str(data)}
    model_running = False
    cancelled = []

    def model_idle(config, **kwargs):
        nonlocal model_running
        assert config["data_dir"] == str(data)
        cancelled.append(True)
        model_running = False

    def strict_check(*args, **kwargs):
        if model_running:
            raise RuntimeInUseError([{"pid": 123, "role": "model_worker", "blocking": True}])

    monkeypatch.setattr("data_search.model_manager.wait_for_model_idle", model_idle)
    monkeypatch.setattr(upgrade, "_wait_runtime_free", strict_check)

    def activation_failure(args, **kwargs):
        nonlocal model_running
        if "-NativeTransactionChild" in args:
            atomic_json(data / "config.json", defaults(str(data), []))
            model_running = True
            if crash:
                raise KeyboardInterrupt("first installer stopped after starting its model task")
            return SimpleNamespace(returncode=1, stdout="", stderr="acceptance failed")
        return success()

    if crash:
        with pytest.raises(KeyboardInterrupt):
            upgrade.upgrade_native(request, runner=activation_failure)
        assert (data / "upgrade-state.json").exists()
        assert upgrade.upgrade_native(request, runner=success)["ok"]
    else:
        with pytest.raises(RuntimeError, match="Installation command failed"):
            upgrade.upgrade_native(request, runner=activation_failure)
        assert not (install / "runtime").exists()
        assert not (data / "config.json").exists()
    assert cancelled and not model_running
    assert not (data / "upgrade-state.json").exists()
