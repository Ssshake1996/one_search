"""Real local daemon acceptance using only temporary synthetic directories."""
from copy import deepcopy
import time

import pytest

from data_search import service, setup_ui, web_management as web
from data_search.config import atomic_json, defaults, load_config
from data_search.runtime_policy import RuntimePolicy


def request(path, action, params=None):
    return web.dispatch(path, {'action': action, 'params': params or {}})


def edit(path):
    result = request(path, 'settings_get')
    assert result['ok'], result
    return {key: result['result'][key] for key in ('revision', 'values')}


def wait_for_ready(path, *, minimum=1):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = service.rpc(load_config(path), 'index_status')
        if result['progress']['content']['counts'].get('ready', 0) >= minimum:
            return result
        time.sleep(.1)
    pytest.fail('Synthetic content did not become ready within the bounded deadline')


@pytest.fixture
def live(tmp_path):
    first, second = tmp_path / 'first documents', tmp_path / 'second 资料'
    first.mkdir()
    second.mkdir()
    (first / 'original.txt').write_text('originaluniqueneedle', encoding='utf-8')
    (second / 'replacement.txt').write_text('replacementuniqueneedle', encoding='utf-8')
    path = tmp_path / 'isolated data' / 'config.json'
    config = defaults(str(path.parent), [str(first)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    config['runtime_policy'] = {'enabled': False}
    atomic_json(path, config)
    service.start_service(load_config(path))
    try:
        yield path, first, second
    finally:
        service.stop_service(load_config(path))
        assert service.stop_service(load_config(path))['status'] == 'not_running'


def test_real_settings_save_changes_scope_and_preset_then_preserves_pause(live):
    path, first, second = live
    wait_for_ready(path)
    original_config = load_config(path)
    old_id = service.service_status(original_config)['service_id']
    assert service.rpc(original_config, 'search', {'query': 'originaluniqueneedle', 'mode': 'keyword'})['results']
    params = edit(path)
    params['values']['roots'] = [str(second)]
    params['values']['preset'] = 'low'
    preview = request(path, 'settings_preview', params)
    assert preview['ok'] and preview['result']['can_apply']
    assert service.service_status(original_config)['service_id'] == old_id
    saved = request(path, 'settings_save', params)
    assert saved['ok'] and saved['result']['applied'], saved
    current = load_config(path)
    assert current['roots'] == [str(second)]
    assert current['resource']['memory_mb'] == 768
    assert service.service_status(current)['service_id'] != old_id
    status = wait_for_ready(path)
    assert status['progress']['known_unique_files'] == 1
    assert not service.rpc(current, 'search', {'query': 'originaluniqueneedle', 'mode': 'keyword'})['results']
    assert service.rpc(current, 'search', {'query': 'replacementuniqueneedle', 'mode': 'keyword'})['results']
    paused = service.rpc(current, 'pause', {'seconds': 1800})
    paused_id = service.service_status(current)['service_id']
    params = edit(path)
    params['values']['exclude_names'].append('synthetic-unused-folder')
    saved = request(path, 'settings_save', params)
    assert saved['ok'], saved
    current = load_config(path)
    assert service.service_status(current)['service_id'] != paused_id
    progress = service.rpc(current, 'index_status')['progress']
    assert progress['overall']['state'] == 'paused'
    assert progress['runtime_policy']['pause_until'] == paused['pause_until']
    assert service.rpc(current, 'search', {'query': 'replacementuniqueneedle', 'mode': 'keyword'})['results']


def test_invalid_and_stale_forms_leave_actual_running_service_untouched(live):
    path, _, second = live
    old_id = service.service_status(load_config(path))['service_id']
    original_bytes = path.read_bytes()
    params = edit(path)
    invalid = deepcopy(params)
    invalid['values']['roots'] = [str(second / 'does-not-exist')]
    failed = request(path, 'settings_save', invalid)
    assert not failed['ok']
    assert service.service_status(load_config(path))['service_id'] == old_id
    assert path.read_bytes() == original_bytes
    accepted = deepcopy(params)
    accepted['values']['exclude_names'].append('newer-editor-value')
    assert request(path, 'settings_save', accepted)['ok']
    current_id = service.service_status(load_config(path))['service_id']
    current_bytes = path.read_bytes()
    params['values']['roots'] = [str(second)]
    stale = request(path, 'settings_save', params)
    assert stale['error']['code'] == 'revision_conflict'
    assert service.service_status(load_config(path))['service_id'] == current_id
    assert path.read_bytes() == current_bytes


def test_failed_config_write_restarts_old_service_even_when_all_writes_fail(live, monkeypatch):
    path, _, _ = live
    wait_for_ready(path)
    before, old_id = path.read_bytes(), service.service_status(load_config(path))['service_id']
    params = edit(path)
    params['values']['exclude_names'].append('rejected-write')
    original_preflight = web._preflight
    writes = []

    def fail_write(*args, **kwargs):
        writes.append(args[0])
        raise PermissionError('synthetic read-only configuration filesystem')

    def arm_write_failure(current, candidate):
        result = original_preflight(current, candidate)
        # Validation has already used its temporary file. Every write in the
        # actual activation/rollback transaction now fails, including retries.
        monkeypatch.setattr(setup_ui, 'atomic_json', fail_write)
        return result

    monkeypatch.setattr(web, '_preflight', arm_write_failure)
    result = request(path, 'settings_save', params)
    assert not result['ok']
    assert writes and path.read_bytes() == before
    current = load_config(path)
    assert service.service_status(current)['service_id'] != old_id
    assert service.rpc(current, 'search', {'query': 'originaluniqueneedle', 'mode': 'keyword'})['results']


def test_new_runtime_start_failure_rolls_back_real_service(live, monkeypatch):
    path, _, _ = live
    wait_for_ready(path)
    before, old_id = path.read_bytes(), service.service_status(load_config(path))['service_id']
    params = edit(path)
    params['values']['exclude_names'].append('reject-new-runtime')
    original_start = service.start_service

    def fail_candidate(config, **kwargs):
        if 'reject-new-runtime' in config['exclude_names']:
            raise service.ServiceError('synthetic new daemon start failure')
        return original_start(config, **kwargs)

    monkeypatch.setattr(service, 'start_service', fail_candidate)
    result = request(path, 'settings_save', params)
    assert result['error']['code'] == 'apply_failed'
    assert path.read_bytes() == before
    current = load_config(path)
    assert service.service_status(current)['service_id'] != old_id
    assert service.rpc(current, 'search', {'query': 'originaluniqueneedle', 'mode': 'keyword'})['results']


def test_real_progress_before_discovery_and_with_an_empty_selected_scope(tmp_path):
    root = tmp_path / 'only synthetic files'
    root.mkdir()
    (root / 'waiting.txt').write_text('pending fixture', encoding='utf-8')
    path = tmp_path / 'data' / 'config.json'
    config = defaults(str(path.parent), [str(root)])
    config['semantic']['enabled'] = False
    config['runtime_policy'] = {'enabled': False}
    atomic_json(path, config)
    RuntimePolicy(config).pause()
    service.start_service(load_config(path))
    try:
        report = service.rpc(load_config(path), 'index_status')['progress']
        assert report['overall']['state'] == 'paused'
        assert report['discovery']['active'] and not report['discovery']['complete']
        assert report['discovery']['roots'] == [] and report['known_unique_files'] == 0
        assert report['discovery']['total'] is None
    finally:
        service.stop_service(load_config(path))
    config['roots'] = []  # Explicit directories scope, never machine scope.
    atomic_json(path, config)
    RuntimePolicy(config).resume()
    service.start_service(load_config(path))
    try:
        report = service.rpc(load_config(path), 'index_status')['progress']
        assert report['overall'] == {'state': 'up_to_date', 'reason': None, 'scope_complete': False}
        assert report['discovery']['complete'] and not report['discovery']['active']
        assert report['known_unique_files'] == 0 and report['discovery']['total'] is None
    finally:
        service.stop_service(load_config(path))
