from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

import pytest

from data_search.config import defaults, atomic_json, load_config
from data_search.maintenance import (IndexDirectoryLease, cleanup_backup, compatibility_info,
    export_config, registered_clients, register_client, relocate_index, remove_client,
    restore_config, retained_backups, space_report)
from data_search.service import InstanceLock, ServiceError


def configuration(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    config = defaults(str(data), [str(source)])
    path = data / 'config.json'
    atomic_json(path, config)
    return load_config(path)


def index_data(config):
    path = Path(config['data_dir']) / 'index.sqlite3'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE evidence(value TEXT)')
    connection.execute("INSERT INTO evidence VALUES('source proof')")
    connection.commit()
    connection.close()
    return path


def test_index_lease_rejects_simultaneous_and_other_instance(tmp_path):
    config = configuration(tmp_path)
    target = tmp_path / 'index'
    config['index_dir'] = str(target)
    with IndexDirectoryLease(config):
        with pytest.raises(ServiceError):
            with IndexDirectoryLease(config):
                pass
    other = {**config, 'data_dir': str(tmp_path / 'other')}
    with pytest.raises(ValueError, match='another instance'):
        with IndexDirectoryLease(other):
            pass


def test_relocate_keeps_sources_config_and_model_then_cleans_only_backup(tmp_path):
    config = configuration(tmp_path)
    original_index = index_data(config)
    proof = Path(config['roots'][0]) / 'report.txt'
    proof.write_text('untouched')
    model = Path(config['semantic']['model_dir'])
    model.mkdir(parents=True)
    (model / 'model.onnx').write_bytes(b'model')
    target = tmp_path / 'moved index'
    result = relocate_index(config, target)
    updated = load_config(config['config_path'])
    assert updated['index_dir'] == str(target)
    assert updated['data_dir'] == config['data_dir']
    assert updated['semantic']['model_dir'] == str(model)
    assert original_index.exists() and (target / 'index.sqlite3').read_bytes() == original_index.read_bytes()
    assert sqlite3.connect(target / 'index.sqlite3').execute('SELECT value FROM evidence').fetchone()[0] == 'source proof'
    backups = retained_backups(updated)
    assert len(backups) == 1 and backups[0]['cleanup_eligible']
    cleanup_backup(updated, backups[0]['id'])
    assert not original_index.exists()
    assert (target / 'index.sqlite3').exists()
    assert proof.read_text() == 'untouched' and (model / 'model.onnx').read_bytes() == b'model'
    assert result['old_index_retained']


def test_relocation_partial_copy_failure_rolls_back_and_can_retry(tmp_path):
    config = configuration(tmp_path)
    original = index_data(config).read_bytes()
    saved = Path(config['config_path']).read_bytes()
    target = tmp_path / 'newindex'
    def fail_copy(source, destination):
        Path(destination).write_bytes(b'partial')
        raise OSError('simulated full disk')
    with pytest.raises(OSError):
        relocate_index(config, target, copy=fail_copy)
    assert Path(config['config_path']).read_bytes() == saved
    assert (Path(config['data_dir']) / 'index.sqlite3').read_bytes() == original
    assert not (target / 'index.sqlite3').exists()
    assert relocate_index(config, target)['changed']


def test_relocation_requires_stopped_service_and_empty_target(tmp_path):
    config = configuration(tmp_path)
    index_data(config)
    with InstanceLock(Path(config['data_dir']) / 'service.lock'):
        with pytest.raises(ServiceError):
            relocate_index(config, tmp_path / 'newindex')
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'file').write_text('user data')
    with pytest.raises(ValueError, match='empty'):
        relocate_index(config, unrelated)
    assert (unrelated / 'file').read_text() == 'user data'


def test_interrupted_copy_retries_owned_destination_without_losing_source(tmp_path):
    config = configuration(tmp_path)
    source_index = index_data(config)
    target = tmp_path / 'interrupted-index'
    with IndexDirectoryLease(config, directory=target):
        (target / 'index.sqlite3').write_bytes(b'partial interrupted copy')
    journal = Path(config['data_dir']) / 'maintenance' / ('relocation-' + uuid.uuid4().hex + '.json')
    atomic_json(journal, {'product': 'one-search-relocation', 'schema_version': 1, 'phase': 'copying',
        'source': config['data_dir'], 'destination': str(target), 'data_dir': config['data_dir'], 'files': {}})
    assert relocate_index(config, target)['changed']
    assert source_index.read_bytes() == (target / 'index.sqlite3').read_bytes()
    assert json.loads(journal.read_text())['phase'] == 'retried'


def test_crash_after_config_switch_completes_verified_journal(tmp_path):
    config = configuration(tmp_path)
    index_data(config)
    target = tmp_path / 'newindex'
    result = relocate_index(config, target)
    journal = Path(result['transaction'])
    record = json.loads(journal.read_text())
    record['phase'] = 'verified'
    atomic_json(journal, record)
    recovered = relocate_index(load_config(config['config_path']), target)
    assert recovered['completed_transactions'] == [journal.stem]
    assert json.loads(journal.read_text())['phase'] == 'complete'


def test_cleanup_refuses_old_index_modified_after_migration(tmp_path):
    config = configuration(tmp_path)
    index_data(config)
    relocate_index(config, tmp_path / 'newindex')
    updated = load_config(config['config_path'])
    backup = retained_backups(updated)[0]
    old = Path(config['data_dir']) / 'index.sqlite3'
    old.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        cleanup_backup(updated, backup['id'])
    assert old.read_bytes() == b'changed'


def test_export_strips_credentials_and_internal_identity(tmp_path):
    config = configuration(tmp_path)
    config['token'] = 'do-not-export'
    config['databases'] = [{'id': 'orders', 'kind': 'mysql', 'password': 'hidden', 'password_env': 'ORDERS_PASSWORD',
                            'credential_id': 'secret-vault-id', 'connection': {'api_key': 'hidden'}, 'allowed_tables': []}]
    exported = export_config(config)
    serialized = json.dumps(exported)
    assert 'hidden' not in serialized and 'do-not-export' not in serialized and 'secret-vault-id' not in serialized
    assert 'ORDERS_PASSWORD' in serialized
    assert 'data_dir' not in exported['settings'] and 'model_dir' not in exported['settings']['semantic']
    assert exported['requires_reconnect'] == ['orders']


def test_restore_preview_mapping_and_lock_preserve_identity(tmp_path):
    config = configuration(tmp_path)
    exported = export_config(config)
    old = str(tmp_path / 'oldmachine')
    exported['settings']['roots'] = [old]
    preview = restore_config(config, exported)
    assert preview['issues'][0]['reason'] == 'directory_missing'
    with pytest.raises(ValueError, match='missing local paths'):
        restore_config(config, exported, apply=True)
    restored = restore_config(config, exported, path_mappings={old: config['roots'][0]}, apply=True)
    assert restored['applied'] and not restored['credentials_restored']
    assert load_config(config['config_path'])['data_dir'] == config['data_dir']
    with InstanceLock(Path(config['data_dir']) / 'service.lock'):
        with pytest.raises(ServiceError):
            restore_config(config, export_config(config), apply=True)


def test_client_registry_is_idempotent_and_explains_shared_impact(tmp_path):
    config = configuration(tmp_path)
    register_client(config, 'dsh-web', label='DSH web', kind='dsh')
    register_client(config, 'dsh-web', label='DSH web', kind='dsh')
    register_client(config, 'editor', label='Editor')
    assert len(registered_clients(config)) == 2
    assert len(compatibility_info(config)['clients']) == 2
    result = remove_client(config, 'editor')
    assert result['removed'] and not result['daemon_stopped']


def test_space_report_does_not_double_count_nested_model(tmp_path):
    config = configuration(tmp_path)
    index_data(config)
    model = Path(config['semantic']['model_dir'])
    model.mkdir(parents=True)
    (model / 'model.onnx').write_bytes(b'12345678')
    report = space_report(config)
    assert report['categories_bytes']['model'] == 8
    assert report['total_bytes'] == sum(report['categories_bytes'].values())
