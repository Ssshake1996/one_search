"""Real process tests use only isolated synthetic document directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil
import pytest

from data_search import cli, service, service_control as control
from data_search.config import atomic_json, defaults, load_config


@pytest.fixture
def config(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    path = tmp_path / 'instance' / 'config.json'
    value = defaults(str(path.parent), [str(root)])
    value['semantic']['enabled'] = False
    value['resource'].update(min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
    atomic_json(path, value)
    return load_config(path)


def test_missing_state_preserves_legacy_automatic_start_and_corrupt_fails_closed(config, monkeypatch):
    assert control.read_control(config)['revision'] == 'initial'
    assert control.require_running(config)['desired_state'] == 'running'
    path = Path(config['data_dir']) / 'service-control.json'
    path.write_text('{broken')
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *a, **k: pytest.fail('corrupt intent cannot launch'))
    with pytest.raises(control.ServiceControlError) as caught:
        service.start_service(config)
    assert caught.value.code == 'service_control_invalid'


def test_cli_stop_is_durable_automatic_launch_is_noop_manual_start_clears(config, monkeypatch, capsys):
    assert cli.main(['stop', '--config', config['config_path']]) == 0
    stopped = control.read_control(config)
    assert stopped['desired_state'] == 'stopped' and stopped['reason'] == 'manual'
    assert cli.main(['start', '--automatic', '--config', config['config_path']]) == 0
    assert control.read_control(config) == stopped
    monkeypatch.setattr(service, 'start_service', lambda *_a, **_k: {'status': 'running', 'started': True})
    assert cli.main(['start', '--config', config['config_path']]) == 0
    assert control.read_control(config)['desired_state'] == 'running'
    assert 'stopped' in capsys.readouterr().out


def test_lowlevel_and_daemon_entry_cannot_bypass_stop(config, monkeypatch):
    control.stop(config)
    with pytest.raises(control.ServiceControlError, match='stopped by the user'):
        service.start_service(config)
    with pytest.raises(control.ServiceControlError):
        service.run_daemon(config, engine_factory=lambda _: pytest.fail('must not construct engine'))
    assert cli.main(['daemon', '--config', config['config_path']]) == 0


def test_temporary_stop_does_not_change_user_intent(config):
    before = control.read_control(config)
    assert cli.main(['stop', '--temporary', '--config', config['config_path']]) == 0
    assert control.read_control(config) == before


def test_stale_start_revision_cannot_override_new_stop_then_start(config, monkeypatch):
    old = control.require_running(config)['revision']
    control.stop(config)
    monkeypatch.setattr(service, '_start_service_locked', lambda *a, **k: {'status': 'running'})
    control.start(config)
    with pytest.raises(control.ServiceControlError) as caught:
        service.start_service(config, control_revision=old)
    assert caught.value.code == 'service_control_changed'


def test_stop_during_engine_initialization_never_starts_background(config):
    entered, release = threading.Event(), threading.Event()
    started, closed, failures = [], [], []

    class SlowEngine:
        def __init__(self, _):
            entered.set()
            assert release.wait(5)
        def start_background(self):
            started.append(True)
        def close(self):
            closed.append(True)

    def run():
        try:
            service.run_daemon(config, engine_factory=SlowEngine)
        except control.ServiceControlError as error:
            failures.append(error.code)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    # Persist intent without waiting for the deliberately stalled constructor.
    with control.control_lock(config):
        control._write_control(config, 'stopped', 'manual')
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == ['service_stopped'] and not started and closed
    assert not (Path(config['data_dir']) / 'service-process.json').exists()


def test_stop_invalidates_inflight_launcher_and_reaps_its_delayed_child(config, monkeypatch):
    from data_search import runtime
    launched, errors, children = threading.Event(), [], []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        launched.set()
        return process
    monkeypatch.setattr(runtime, 'process_command', lambda *a: [sys.executable, '-c', 'import time; time.sleep(30)'])
    monkeypatch.setattr(subprocess, 'Popen', launch)
    def start():
        try:
            service.start_service(config)
        except control.ServiceControlError as error:
            errors.append(error.code)
    thread = threading.Thread(target=start)
    thread.start()
    assert launched.wait(3)
    try:
        control.stop(config)
        thread.join(5)
        assert not thread.is_alive()
        assert errors == ['service_stopped']
        assert children[0].poll() is not None
    finally:
        if children[0].poll() is None:
            children[0].kill()
        children[0].wait(5)


@pytest.mark.parametrize('damage', ['pid_reuse', 'wrong_config', 'wrong_role'])
def test_force_stop_never_kills_unrelated_process(config, damage):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        marker = {'role': 'data_search_daemon', 'pid': process.pid,
                  'create_time': psutil.Process(process.pid).create_time(),
                  'service_id': 'test', 'config_path': config['config_path']}
        if damage == 'pid_reuse': marker['create_time'] -= 10
        if damage == 'wrong_config': marker['config_path'] += '.other'
        # Even a forged marker with a current PID/birth time fails role argv.
        atomic_json(Path(config['data_dir']) / 'service-process.json', marker)
        with pytest.raises(control.ServiceControlError):
            control.stop(config, force=True)
        assert process.poll() is None
        assert control.read_control(config)['desired_state'] == 'stopped'
    finally:
        process.kill()
        process.wait(5)


def test_real_daemon_stop_survives_reconnect_and_force_stop_preserves_other_instance(config, tmp_path):
    other_path = tmp_path / 'other' / 'config.json'
    other_value = {**config, 'data_dir': str(other_path.parent), 'index_dir': str(other_path.parent)}
    atomic_json(other_path, other_value)
    other = load_config(other_path)
    first, second = None, None
    try:
        first = control.start(config)
        second = control.start(other)
        stopped = control.stop(config)
        assert stopped['service_control']['desired_state'] == 'stopped'
        with pytest.raises(control.ServiceControlError):
            service.start_service(config)
        # A fresh CLI (as a login/startup launcher would use) preserves the stop.
        result = subprocess.run([sys.executable, '-m', 'data_search', 'start', '--automatic', '--config', config['config_path']],
                                capture_output=True, text=True, timeout=8)
        assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'stopped'
        assert service.service_status(other)['pid'] == second['pid']
        first = control.start(config, reason='scheduled')
        assert control.read_control(config)['reason'] == 'scheduled'
        # Leave a real disposable parser alive, then suspend the daemon to
        # emulate an unresponsive RPC/engine. Force stop cannot depend on RPC.
        document = Path(config['roots'][0]) / 'force-stop.txt'
        document.write_text('synthetic force stop evidence')
        service.rpc(config, 'refresh_path', {'path': str(document)})
        owned = psutil.Process(first['pid'])
        workers = owned.children(recursive=True)
        assert workers
        owned.suspend()
        assert control.stop(config, force=True)['forced']
        assert not psutil.pid_exists(first['pid'])
        assert all(not worker.is_running() for worker in workers)
        assert service.service_status(other)['pid'] == second['pid']
        time.sleep(.35)
        assert not (Path(config['data_dir']) / 'service.json').exists()
    finally:
        service.stop_service(config)
        service.stop_service(other)


def test_mcp_remains_connected_without_implicitly_starting_stopped_service(config, monkeypatch):
    from data_search import mcp_server
    control.stop(config)
    calls = []
    class MCP:
        def run(self, **kwargs): calls.append(kwargs)
    monkeypatch.setattr(mcp_server, 'create_mcp', lambda _: MCP())
    mcp_server.run_mcp(config)
    assert calls == [{'transport': 'stdio'}]
    assert control.read_control(config)['desired_state'] == 'stopped'


def test_installation_accepts_preserved_stop_without_claiming_search_ready(config):
    from data_search.installation import installation_status
    control.stop(config)
    result = installation_status(config)
    assert result['ok'] and result['intentionally_stopped']
    assert not result['daemon_running'] and not result['basic_search_ready']


def test_upgrade_uses_temporary_stop_and_rollback_preserves_new_user_stop(tmp_path):
    from types import SimpleNamespace
    from data_search import upgrade
    from test_upgrade_lifecycle import fixture
    config, request = fixture(tmp_path)
    manifest_path = Path(request['InstallDir']) / 'install-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    atomic_json(manifest_path, {**manifest, 'version': '0.6.0'})
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        assert args[1:2] != ['start'], 'Rollback must not clear a stop requested during activation'
        if args[1:2] == ['stop']:
            assert '--temporary' in args
        if '-NativeTransactionChild' in args:
            control.stop(config)
            return SimpleNamespace(returncode=1, stdout='', stderr='synthetic activation failure')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    with pytest.raises(RuntimeError, match='Installation command failed'):
        upgrade.upgrade_native(request, runner=runner)
    assert control.read_control(config)['desired_state'] == 'stopped'
    assert any('--temporary' in args for args in calls)


def test_upgrade_stopped_instance_acceptance_does_not_require_running_daemon(tmp_path):
    from types import SimpleNamespace
    from data_search import upgrade
    from test_upgrade_lifecycle import fixture
    config, request = fixture(tmp_path)
    control.stop(config)
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        assert args[1:2] != ['start']
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert upgrade.upgrade_native(request, runner=runner)['ok']
    assert calls[-1][1] == 'installation-status'
    assert control.read_control(config)['desired_state'] == 'stopped'


def test_new_installer_and_login_launchers_explicitly_preserve_service_intent():
    root = Path(__file__).resolve().parents[1]
    powershell = (root / 'scripts/install.ps1').read_text(encoding='utf-8')
    bash = (root / 'scripts/install.sh').read_text(encoding='utf-8')
    assert "'start' '--automatic'" in powershell
    assert 'start --automatic --config' in powershell
    assert "--temporary'" in powershell
    assert '"$cli" start --automatic' in bash
    assert 'stop_args+=(--temporary)' in bash
    assert 'Restart=no' in bash and 'Restart=on-failure' not in bash
    assert 'Restart=no' in (root / 'src/data_search/maintenance.py').read_text(encoding='utf-8')


@pytest.mark.parametrize('updated', [True, float('nan'), float('inf'), 10 ** 400])
def test_invalid_control_timestamp_fails_closed(config, updated):
    atomic_json(Path(config['data_dir']) / 'service-control.json', {
        'schema_version': 1, 'desired_state': 'running', 'revision': 'test', 'reason': 'automatic', 'updated_at': updated})
    with pytest.raises(control.ServiceControlError) as caught:
        control.read_control(config)
    assert caught.value.code == 'service_control_invalid'


def test_force_stop_rejects_nan_process_birth_time(config):
    atomic_json(Path(config['data_dir']) / 'service-process.json', {'role': 'data_search_daemon',
        'config_path': config['config_path'], 'pid': os.getpid(), 'create_time': float('nan'), 'service_id': 'bad'})
    with pytest.raises(control.ServiceControlError) as caught:
        control.stop(config, force=True)
    assert caught.value.code == 'service_process_invalid'


@pytest.mark.parametrize('lease_held', [False, True])
def test_stop_rpc_lost_is_success_only_after_daemon_lease_released(config, monkeypatch, lease_held):
    def disconnected(*args, **kwargs):
        raise service.ServiceError('Daemon exited between health and stop RPC')
    monkeypatch.setattr(service, 'stop_service', disconnected)
    lock = service.InstanceLock(Path(config['data_dir']) / 'service.lock')
    if lease_held:
        lock.__enter__()
    try:
        if lease_held:
            with pytest.raises(control.ServiceControlError) as caught:
                control.stop(config, timeout=.1)
            assert caught.value.code == 'service_stop_timeout'
        else:
            result = control.stop(config, timeout=.1)
            assert result['status'] == 'stopped' and result['stopped']
        assert control.read_control(config)['desired_state'] == 'stopped'
    finally:
        if lease_held:
            lock.__exit__(None, None, None)
