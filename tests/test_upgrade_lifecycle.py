import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from data_search.config import atomic_json, defaults, load_config
from data_search.maintenance import MaintenanceGuard, cleanup_backup, relocate_index, retained_backups
from data_search.service import InstanceLock, ServiceError
from data_search.upgrade import upgrade_native
from data_search import upgrade


def fixture(tmp_path):
    install, data, release = (tmp_path / name for name in ('app', 'data', 'release'))
    runtime = release / 'runtime'
    (install / 'runtime').mkdir(parents=True)
    runtime.mkdir(parents=True)
    (install / 'runtime/data-search.exe').write_bytes(b'old executable')
    (runtime / 'data-search.exe').write_bytes(b'new executable')
    (release / 'SHA256SUMS.json').write_text(json.dumps({'runtime/data-search.exe': hashlib.sha256(b'new executable').hexdigest()}))
    conf = defaults(str(data), [])
    conf['semantic']['enabled'] = False
    atomic_json(data / 'config.json', conf)
    config = load_config(data / 'config.json')
    atomic_json(install / 'install-manifest.json', {'product': 'data-search', 'schema_version': 1,
        'install_dir': str(install), 'data_dir': str(data), 'config': config['config_path'],
        'cli': str(install / 'runtime/data-search.exe'), 'autostart': 'none'})
    atomic_json(data / '.data-search-data.json', {'product': 'data-search', 'schema_version': 1, 'data_dir': str(data)})
    db = sqlite3.connect(data / 'index.sqlite3')
    db.execute('CREATE TABLE evidence(value TEXT)')
    db.execute("INSERT INTO evidence VALUES('previous schema')")
    db.commit()
    db.close()
    relocate_index(config, tmp_path / 'external-index')
    config = load_config(config['config_path'])
    return config, {'InstallDir': str(install), 'DataDir': str(data), 'RuntimeDir': str(runtime), 'Installer': str(release / 'install.ps1')}


def test_upgrade_failed_activation_restores_external_index_and_runtime(tmp_path):
    config, request = fixture(tmp_path)
    install, data, index = Path(request['InstallDir']), Path(config['data_dir']), Path(config['index_dir'])
    config_before = Path(config['config_path']).read_bytes()
    lock_checks = []
    def copy(source, destination):
        if Path(destination).parent.name in ('data-snapshot', 'index-snapshot'):
            with pytest.raises(ServiceError):
                with InstanceLock(data / 'model-job/manager.lock'):
                    pass
            lock_checks.append(True)
        return shutil.copy2(source, destination)
    def runner(args, **kwargs):
        if '-NativeTransactionChild' in args:
            db = sqlite3.connect(index / 'index.sqlite3')
            db.execute('DROP TABLE evidence')
            db.commit()
            db.close()
            atomic_json(Path(config['config_path']), {**config, 'scope': 'machine'})
            return SimpleNamespace(returncode=1, stdout='', stderr='activation failed')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    with pytest.raises(RuntimeError, match='Installation command failed'):
        upgrade_native(request, runner=runner, copy=copy)
    assert lock_checks
    assert (install / 'runtime/data-search.exe').read_bytes() == b'old executable'
    assert Path(config['config_path']).read_bytes() == config_before
    db = sqlite3.connect(index / 'index.sqlite3')
    assert db.execute('SELECT value FROM evidence').fetchone()[0] == 'previous schema'
    db.close()
    record = json.loads(next(install.glob('.upgrade-*/transaction.json')).read_text())
    assert record['phase'] == 'rolled_back' and record['index_dir'] == str(index)


def test_completed_upgrade_backup_cleanup_never_deletes_active_index(tmp_path):
    config, request = fixture(tmp_path)
    result = upgrade_native(request, runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert result['ok']
    backups = retained_backups(config, install_dir=request['InstallDir'])
    backup = next(item for item in backups if item['kind'] == 'upgrade')
    cleanup_backup(config, backup['id'], install_dir=request['InstallDir'])
    assert not Path(backup['path']).exists()
    assert (Path(config['index_dir']) / 'index.sqlite3').exists()
    assert (Path(request['InstallDir']) / 'runtime/data-search.exe').read_bytes() == b'new executable'


def test_maintenance_guard_blocks_model_admission_and_daemon(tmp_path):
    config, _ = fixture(tmp_path)
    with MaintenanceGuard(config):
        for relative in ('service.lock', 'model-job/manager.lock', 'model-job/worker.lock'):
            with pytest.raises(ServiceError):
                with InstanceLock(Path(config['data_dir']) / relative):
                    pass


def test_transient_runtime_locks_during_swap_and_rollback_are_retried(tmp_path, monkeypatch):
    config, request = fixture(tmp_path)
    before = Path(config['config_path']).read_bytes()
    original = Path.rename
    attempts = {}

    def transient(source, destination):
        pair = source, Path(destination)
        attempts[pair] = attempts.get(pair, 0) + 1
        if attempts[pair] == 1:
            error = PermissionError('native image is still mapped')
            error.winerror = 5
            raise error
        return original(source, destination)

    def runner(args, **kwargs):
        return SimpleNamespace(returncode=int('-NativeTransactionChild' in args), stdout='', stderr='')

    monkeypatch.setattr(Path, 'rename', transient)
    with pytest.raises(RuntimeError, match='Installation command failed'):
        upgrade_native(request, runner=runner)
    install = Path(request['InstallDir'])
    assert (install / 'runtime/data-search.exe').read_bytes() == b'old executable'
    assert Path(config['config_path']).read_bytes() == before
    assert len(attempts) == 4 and set(attempts.values()) == {2}
    assert json.loads(next(install.glob('.upgrade-*/transaction.json')).read_text())['phase'] == 'rolled_back'


def test_persistent_runtime_lock_keeps_old_installation_and_snapshot(tmp_path, monkeypatch):
    config, request = fixture(tmp_path)
    before = Path(config['config_path']).read_bytes()
    moment = [0.0]
    monkeypatch.setattr(upgrade, 'time', SimpleNamespace(
        monotonic=lambda: moment[0], sleep=lambda seconds: moment.__setitem__(0, moment[0] + seconds)))

    def locked(source, destination):
        error = PermissionError('persistent lock')
        error.winerror = 32
        raise error

    monkeypatch.setattr(Path, 'rename', locked)
    with pytest.raises(PermissionError, match='persistent lock'):
        upgrade_native(request, runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    install = Path(request['InstallDir'])
    assert 5 <= moment[0] < 5.1
    assert (install / 'runtime/data-search.exe').read_bytes() == b'old executable'
    assert Path(config['config_path']).read_bytes() == before
    transaction = next(install.glob('.upgrade-*'))
    assert json.loads((transaction / 'transaction.json').read_text())['phase'] == 'failed_before_activation'
    assert (transaction / 'index-snapshot/index.sqlite3').is_file()
    assert (transaction / 'staged-runtime/data-search.exe').read_bytes() == b'new executable'


def test_non_windows_permission_error_does_not_wait(tmp_path, monkeypatch):
    def denied(source, destination):
        raise PermissionError('ordinary permission failure')
    monkeypatch.setattr(Path, 'rename', denied)
    monkeypatch.setattr(upgrade, 'time', SimpleNamespace(monotonic=lambda: 0,
        sleep=lambda _: pytest.fail('Non-sharing errors must not be retried')))
    with pytest.raises(PermissionError, match='ordinary permission failure'):
        upgrade._rename_runtime(tmp_path / 'source', tmp_path / 'destination')


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory rename sharing rules')
def test_real_windows_runtime_file_handle_is_released_before_rename(tmp_path):
    source, destination = tmp_path / 'runtime', tmp_path / 'previous-runtime'
    source.mkdir()
    (source / 'mapped.dll').write_bytes(b'synthetic library bytes')
    handle = (source / 'mapped.dll').open('rb')
    release = None
    try:
        # Reproduce the same OS error before testing the bounded recovery.
        with pytest.raises(PermissionError) as error:
            source.rename(destination)
        assert error.value.winerror in {5, 32, 33}
        release = threading.Timer(0.2, handle.close)
        release.start()
        started = time.monotonic()
        upgrade._rename_runtime(source, destination)
        assert time.monotonic() - started < 4
        assert (destination / 'mapped.dll').read_bytes() == b'synthetic library bytes'
    finally:
        handle.close()
        if release:
            release.join(1)
