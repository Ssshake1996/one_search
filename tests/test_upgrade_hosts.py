import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from data_search.config import atomic_json
from data_search.runtime_use import RuntimeInUseError
from data_search.upgrade_hosts import UpgradeCoordinationError, UpgradeSession
from data_search import upgrade
from test_upgrade_lifecycle import fixture


def test_host_drain_is_authenticated_and_marker_spans_transaction(tmp_path):
    data = tmp_path / 'data'
    events = []
    class Host(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == '/control'
            assert self.headers['Authorization'] == 'Bearer ' + 'x' * 64
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            events.append((body['action'], (data / 'upgrade-state.json').exists()))
            payload = {'ok': True, 'result': {'instance_id': 'host-one',
                'maintenance': body['action'] == 'prepare', 'transaction_id': body['transaction_id']}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Host)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    atomic_json(data / 'host-clients/host-one.json', {'schema_version': 1, 'pid': os.getpid(),
        'instance_id': 'host-one', 'port': server.server_port, 'token': 'x' * 64,
        'data_dir': str(data), 'config_path': str(data / 'config.json')})
    try:
        with UpgradeSession(tmp_path / 'app', data) as session:
            assert events == [('prepare', True)]
            with pytest.raises(UpgradeCoordinationError) as error:
                with UpgradeSession(tmp_path / 'app', data):
                    pytest.fail('concurrent installers must not enter')
            assert error.value.code == 'upgrade_in_progress'
            assert (data / 'upgrade-state.json').exists()
            session.update('activating', safe=False)
            session.update('complete', safe=True)
        assert events == [('prepare', True), ('resume', False)]
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()


def test_invalid_or_unrecoverable_marker_never_grants_reconnection(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    marker = data / 'upgrade-state.json'
    marker.write_text('{broken')
    with pytest.raises(UpgradeCoordinationError):
        with UpgradeSession(tmp_path / 'app', data):
            pass
    assert marker.read_text() == '{broken'
    atomic_json(marker, {'schema_version': 1, 'install_dir': str(tmp_path / 'app'), 'data_dir': str(data),
        'transaction_id': 'interrupted', 'phase': 'activating'})
    with pytest.raises(UpgradeCoordinationError) as error:
        with UpgradeSession(tmp_path / 'app', data):
            pass
    assert error.value.code == 'upgrade_recovery_required'
    assert marker.exists()


def test_legacy_runtime_in_use_rejected_before_stopping_or_snapshot(tmp_path, monkeypatch):
    config, request = fixture(tmp_path)
    before = Path(config['config_path']).read_bytes()
    def occupied(*args, **kwargs):
        raise RuntimeInUseError([{'pid': 123, 'role': 'mcp', 'blocking': True}])
    monkeypatch.setattr(upgrade, 'assert_runtime_available', occupied)
    with pytest.raises(RuntimeInUseError) as error:
        upgrade.upgrade_native(request, runner=lambda *a, **k: pytest.fail('No runtime command may run'))
    assert error.value.code == 'runtime_in_use'
    assert Path(config['config_path']).read_bytes() == before
    assert not list(Path(request['InstallDir']).glob('.upgrade-*'))
    assert not (Path(request['DataDir']) / 'upgrade-state.json').exists()


def test_interrupted_activation_recovers_before_retry_and_preserves_host_registrations(tmp_path):
    config, request = fixture(tmp_path)
    data, install = Path(request['DataDir']), Path(request['InstallDir'])
    original_config = Path(config['config_path']).read_bytes()
    def killed(args, **kwargs):
        if '-NativeTransactionChild' in args:
            atomic_json(data / 'config.json', {'data_dir': str(data), 'damaged_by_migration': True})
            raise KeyboardInterrupt('simulate updater termination after runtime swap')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    with pytest.raises(KeyboardInterrupt):
        upgrade.upgrade_native(request, runner=killed)
    assert (data / 'upgrade-state.json').exists()
    assert (install / 'runtime/data-search.exe').read_bytes() == b'new executable'
    # A newly started waiting profile belongs to the live installation, never
    # to the old snapshot. It must survive recovery unchanged.
    registry = data / 'host-clients/stale.json'
    atomic_json(registry, {'fixture': 'survives snapshot restore'})
    checked = []
    def retry(args, **kwargs):
        if args[1:2] == ['start']:
            assert (install / 'runtime/data-search.exe').read_bytes() == b'old executable'
            assert (data / 'config.json').read_bytes() == original_config
            checked.append(True)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert upgrade.upgrade_native(request, runner=retry)['ok']
    assert checked
    assert registry.exists()
    assert not (data / 'upgrade-state.json').exists()
    records = [json.loads(path.read_text()) for path in install.glob('.upgrade-*/transaction.json')]
    assert {record['phase'] for record in records} == {'rolled_back', 'complete'}
    for snapshot in install.glob('.upgrade-*/data-snapshot'):
        assert not (snapshot / 'host-clients').exists()
        assert not (snapshot / 'upgrade-state.json').exists()


def test_interrupted_snapshot_restarts_old_service_without_restoring_partial_snapshot(tmp_path):
    config, request = fixture(tmp_path)
    data, install = Path(request['DataDir']), Path(request['InstallDir'])
    import shutil
    def killed_copy(source, destination):
        if 'data-snapshot' in Path(destination).parts:
            raise KeyboardInterrupt('simulate termination while copying')
        return shutil.copy2(source, destination)
    runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr='')
    with pytest.raises(KeyboardInterrupt):
        upgrade.upgrade_native(request, runner=runner, copy=killed_copy)
    # A killed process skips context teardown. Recreate its on-disk marker
    # because KeyboardInterrupt still executes Python's context manager exit.
    journal = next(install.glob('.upgrade-*/transaction.json'))
    record = json.loads(journal.read_text())
    assert record['was_running'] is True and record['phase'] == 'staging'
    atomic_json(data / 'upgrade-state.json', {'schema_version': 1, 'transaction_id': 'interrupted-snapshot',
        'install_dir': str(install), 'data_dir': str(data), 'phase': 'staging', 'transaction': str(journal.parent)})
    starts = []
    def retry(args, **kwargs):
        if args[1:2] == ['start']:
            starts.append(True)
            assert (install / 'runtime/data-search.exe').read_bytes() == b'old executable'
        return runner()
    assert upgrade.upgrade_native(request, runner=retry)['ok']
    assert starts


def test_new_host_registration_directory_is_accepted_on_fresh_install(tmp_path):
    data = tmp_path / 'data'
    atomic_json(data / 'host-clients/new.json', {'schema_version': 1})
    (data / 'host-clients/new.json.tmp').write_text('{}')
    upgrade._validate_target(data, '.data-search-data.json', {'product': 'data-search', 'data_dir': str(data)})
    (data / 'unrelated.txt').write_text('not an installation')
    with pytest.raises(ValueError, match='unmanaged'):
        upgrade._validate_target(data, '.data-search-data.json', {'product': 'data-search', 'data_dir': str(data)})
