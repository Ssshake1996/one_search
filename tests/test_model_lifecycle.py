import hashlib
import io
import json
import time
from pathlib import Path

import pytest

from data_search import model, model_manager
from data_search.config import defaults, load_config


@pytest.fixture
def assets(tmp_path, monkeypatch):
    source = tmp_path / 'offline model'
    source.mkdir()
    values = {name: (name + ' synthetic pinned asset').encode() for name in model.ASSETS}
    monkeypatch.setattr(model, 'SHA256', {name: hashlib.sha256(value).hexdigest() for name, value in values.items()})
    for name, value in values.items():
        (source / name).write_bytes(value)
    (source / 'manifest.json').write_text(json.dumps({'model_id': model.MODEL_ID}))
    return source, values


def test_offline_import_verifies_and_publishes_last(tmp_path, assets):
    source, values = assets
    dest = tmp_path / 'managed'
    progress = []
    model.download_model(str(dest), source_directory=str(source), progress=progress.append)
    assert model.model_ready(str(dest))
    assert all((dest / name).read_bytes() == value for name, value in values.items())
    assert any(item.get('phase') == 'importing' for item in progress)


def test_network_failure_retry_reuses_verified_assets_without_manifest(tmp_path, assets, monkeypatch):
    _source, values = assets
    dest = tmp_path / 'managed'
    dest.mkdir()
    (dest / 'model.onnx').write_bytes(values['model.onnx'])
    called = []
    def open_url(url, **_):
        name = url.rsplit('/', 1)[-1]
        called.append(name)
        if len(called) == 1:
            raise OSError('network disconnected')
        response = io.BytesIO(values[name])
        response.headers = {'Content-Length': str(len(values[name]))}
        return response
    monkeypatch.setattr(model.urllib.request, 'urlopen', open_url)
    with pytest.raises(OSError):
        model.download_model(str(dest))
    assert not model.model_ready(str(dest))
    model.download_model(str(dest))
    assert 'model_quantized.onnx' not in called
    assert model.model_ready(str(dest))


def test_checksum_failure_never_publishes_ready_manifest(tmp_path, assets):
    source, _values = assets
    (source / 'config.json').write_text('tampered')
    dest = tmp_path / 'managed'
    with pytest.raises(ValueError, match='checksum'):
        model.download_model(str(dest), source_directory=str(source))
    assert not model.model_ready(str(dest))
    assert not (dest / 'config.download').exists()


def test_cancel_keeps_completed_assets_and_retry_succeeds(tmp_path, assets):
    source, _values = assets
    dest = tmp_path / 'managed'
    stop = [False]
    def progress(item):
        if item.get('asset') == 'tokenizer.json':
            stop[0] = True
    with pytest.raises(model.ModelCancelled):
        model.download_model(str(dest), source_directory=str(source), progress=progress, cancelled=lambda: stop[0])
    assert (dest / 'model.onnx').exists()
    assert not (dest / 'manifest.json').exists()
    model.download_model(str(dest), source_directory=str(source))
    assert model.model_ready(str(dest))


def saved_config(tmp_path):
    config = defaults(str(tmp_path / 'data'), [])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    return load_config(str(path))


def test_model_job_is_idempotent_and_runs_import(tmp_path, assets, monkeypatch):
    source, _values = assets
    config = saved_config(tmp_path)
    children = []
    class Child:
        pid = 12345
        def __init__(self, command, **kwargs):
            children.append((command, kwargs))
        def wait(self):
            return 0
    monkeypatch.setattr(model_manager.subprocess, 'Popen', Child)
    first = model_manager.start_model_job(config, str(source))
    again = model_manager.start_model_job(config, str(source))
    assert len(children) == 1 and again['reused']
    assert first['job_id'] == again['job_id']
    assert children[0][1]['stdout'] == model_manager.subprocess.DEVNULL
    model_manager.run_model_job(config, first['job_id'])
    state = model_manager.model_status(config)
    assert state['state'] == 'ready' and state['ready']
    assert state['verification'] == 'verified_by_job'
    (Path(config['semantic']['model_dir']) / 'config.json').write_text('corrupted after installation')
    changed = model_manager.model_status(config)
    assert changed['state'] == 'failed' and not changed['ready']
    assert changed['error']['code'] == 'assets_changed'
    repaired = model_manager.start_model_job(config, str(source))
    assert not repaired['reused']
    model_manager.run_model_job(config, repaired['job_id'])
    assert model_manager.model_status(config)['ready']


def test_job_failure_is_sanitized_retryable_and_basic_search_unaffected(tmp_path, monkeypatch):
    config = saved_config(tmp_path)
    directory = model_manager._directory(config)
    state = {'job_id': 'test', 'model_dir': config['semantic']['model_dir'], 'state': 'queued', 'progress': {}, 'updated_at': time.time()}
    model_manager._write(directory / 'status.json', state)
    model_manager._write(directory / 'request.json', {'job_id': 'test', 'model_dir': config['semantic']['model_dir']})
    def fail(*args, **kwargs):
        raise OSError('secret download token must not escape')
    monkeypatch.setattr(model_manager, 'download_model', fail)
    model_manager.run_model_job(config, 'test')
    result = model_manager.model_status(config)
    assert result['state'] == 'failed'
    assert result['basic_search_available_without_model']
    assert 'secret download token' not in json.dumps(result)
    assert 'retry' in result['recommended_action']


def test_abandoned_job_and_cancellation_are_explicit(tmp_path):
    config = saved_config(tmp_path)
    directory = model_manager._directory(config)
    model_manager._write(directory / 'status.json', {'job_id': 'test', 'model_dir': config['semantic']['model_dir'],
        'state': 'running', 'updated_at': time.time() - 60, 'progress': {}})
    assert model_manager.model_status(config)['state'] == 'interrupted'
    with model_manager.InstanceLock(directory / 'worker.lock'):
        assert model_manager.cancel_model_job(config)['state'] == 'cancelling'
    assert model_manager._read(directory / 'cancel.json')['job_id'] == 'test'


def test_synchronous_compatibility_download_holds_model_admission(tmp_path, assets, monkeypatch):
    source, _values = assets
    config = saved_config(tmp_path)
    original = model_manager.download_model
    def verify_lock(directory):
        with pytest.raises(model_manager.ServiceError):
            with model_manager.InstanceLock(model_manager._directory(config) / 'manager.lock'):
                pass
        return original(directory, source_directory=str(source))
    monkeypatch.setattr(model_manager, 'download_model', verify_lock)
    model_manager.download_model_blocking(config)
    assert model_manager.model_status(config)['ready']


def test_installation_status_does_not_claim_dsh_or_complete_discovery(tmp_path, monkeypatch):
    from data_search import installation
    config = saved_config(tmp_path)
    monkeypatch.setattr(installation, 'service_status', lambda config: {'running': True})
    monkeypatch.setattr(installation, 'rpc', lambda config, method, *args, **kwargs: {'results': []} if method == 'search' else {'coverage': {'files': 0}})
    result = installation.installation_status(config)
    assert result['ok'] and result['basic_search_ready']
    assert result['dsh_connection'] == 'not_checked'
    assert result['indexing_complete'] is None
