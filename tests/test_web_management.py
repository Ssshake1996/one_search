import copy
import io
import json
import sqlite3
from pathlib import Path

import pytest

from data_search import web_management as web
from data_search.config import atomic_json, defaults, load_config
from data_search.service import ServiceError
from data_search.setup_ui import activate_settings, settings_revision


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / '资料'
    root.mkdir()
    path = tmp_path / 'data' / 'config.json'
    config = defaults(str(path.parent), [str(root)])
    config['semantic']['enabled'] = False
    config['resource']['memory_mb'] = 913
    config['custom_internal_value'] = 'not-for-browser'
    atomic_json(path, config)
    monkeypatch.setattr(web, 'service_status', lambda _: {'status': 'running', 'token': 'hidden-token'})
    return path


def request(path, action, params=None):
    return web.dispatch(path, {'action': action, 'params': params or {}})


def edit(path):
    result = request(path, 'settings_get')['result']
    return {'revision': result['revision'], 'values': result['values']}


def test_snapshot_is_allowlisted_and_revision_matches(configured):
    result = request(configured, 'settings_get')['result']
    assert result['revision'] == settings_revision(configured)
    assert result['service'] == {'status': 'running'}
    serialized = json.dumps(result)
    assert 'not-for-browser' not in serialized
    assert 'hidden-token' not in serialized
    assert result['values']['scope'] == 'directories'


def test_save_rejects_stale_revision_without_stopping(configured, monkeypatch):
    params = edit(configured)
    existing = load_config(configured)
    existing['exclude_names'].append('changed')
    atomic_json(configured, existing)
    monkeypatch.setattr(web, 'activate_settings', lambda *a, **k: pytest.fail('must not stop'))
    assert request(configured, 'settings_save', params)['error']['code'] == 'revision_conflict'


def test_save_rechecks_revision_inside_apply_transaction(configured, monkeypatch):
    import data_search.service as service
    params = edit(configured)
    candidate = load_config(configured)
    candidate['exclude_names'].append('concurrent')
    def race(current, proposed):
        atomic_json(configured, candidate)
        return False, None
    monkeypatch.setattr(web, '_preflight', race)
    monkeypatch.setattr(service, 'stop_service', lambda _: pytest.fail('must not stop stale config'))
    assert request(configured, 'settings_save', params)['error']['code'] == 'revision_conflict'


def test_scope_save_preserves_custom_budget_and_hidden_fields(configured, monkeypatch):
    params = edit(configured)
    params['values']['exclude_names'].append('skip-me')
    observed = {}
    def apply(path, current, candidate, tested, **kwargs):
        observed.update(candidate)
        assert kwargs['expected_revision'] == params['revision']
        atomic_json(path, candidate)
    monkeypatch.setattr(web, 'activate_settings', apply)
    result = request(configured, 'settings_save', params)
    assert result['ok'] and result['result']['applied']
    assert observed['resource']['memory_mb'] == 913
    assert observed['custom_internal_value'] == 'not-for-browser'
    assert 'skip-me' in result['result']['values']['exclude_names']


def test_preset_changes_budget_without_scope_change(configured, monkeypatch):
    params = edit(configured)
    params['values']['preset'] = 'low'
    monkeypatch.setattr(web, 'rpc', lambda *a, **k: {'preview': True})
    result = request(configured, 'settings_preview', params)['result']
    assert result['resource']['memory_mb'] == 768
    assert result['preset'] == 'low' and result['can_apply']
    assert load_config(configured)['resource']['memory_mb'] == 913


def test_resource_form_roundtrips_only_editable_budget_fields(configured, monkeypatch):
    params = edit(configured)
    assert set(params['values']['resource']) == {
        'budget_mode', 'memory_mb', 'workers', 'memory_fraction', 'reserve_fraction'}
    resource = {'budget_mode': 'adaptive', 'memory_mb': 6144, 'workers': 6,
                'memory_fraction': .3, 'reserve_fraction': .15}
    params['values']['resource'] = resource
    before = load_config(configured)
    def apply(path, current, candidate, tested, **kwargs):
        atomic_json(path, candidate)
    monkeypatch.setattr(web, 'activate_settings', apply)
    result = request(configured, 'settings_save', params)
    assert result['ok'], result
    assert result['result']['values']['resource'] == resource
    after = load_config(configured)
    assert after['resource']['worker_memory_mb'] == before['resource']['worker_memory_mb']
    assert after['resource']['max_disk_mb'] == before['resource']['max_disk_mb']
    assert after['custom_internal_value'] == before['custom_internal_value']


@pytest.mark.parametrize('key,value', [
    ('budget_mode', 'fill_all_memory'), ('memory_mb', 0), ('memory_mb', True),
    ('workers', 0), ('workers', 9), ('workers', True), ('workers', 1.5),
    ('memory_fraction', 0), ('memory_fraction', .51), ('memory_fraction', True),
    ('reserve_fraction', -.1), ('reserve_fraction', .51),
    ('worker_memory_mb', 99999), ('command', 'private-untrusted-command'),
])
def test_invalid_resource_form_never_activates_or_changes_config(configured, monkeypatch, key, value):
    params = edit(configured)
    params['values']['resource'][key] = value
    before = configured.read_bytes()
    monkeypatch.setattr(web, 'activate_settings', lambda *a, **k: pytest.fail('invalid resources must not activate'))
    result = request(configured, 'settings_save', params)
    assert result['ok'] is False
    assert configured.read_bytes() == before
    assert 'private-untrusted-command' not in json.dumps(result)


def test_old_resource_form_preserves_limits_until_preset_explicitly_changes(configured):
    params = edit(configured)
    params['values'].pop('resource')
    _, _, unchanged = web._candidate(configured, params)
    assert unchanged['resource']['memory_mb'] == 913
    params['values']['preset'] = 'fast'
    _, _, changed = web._candidate(configured, params)
    assert changed['resource']['budget_mode'] == 'adaptive'
    assert changed['resource']['memory_mb'] == 8192
    assert changed['resource']['workers'] == 8


def test_preset_keeps_explicitly_edited_budget_override(configured):
    params = edit(configured)
    params['values']['preset'] = 'fast'
    params['values']['resource'].update(memory_mb=3072, workers=3)
    _, _, candidate = web._candidate(configured, params)
    assert candidate['resource']['memory_mb'] == 3072
    assert candidate['resource']['workers'] == 3
    assert candidate['resource']['worker_cpu_percent'] == 50


def test_changed_database_preflight_failure_prevents_save(configured, monkeypatch):
    params = edit(configured)
    params['values']['databases'] = [{'id': 'missing', 'kind': 'sqlite', 'path': 'absent.sqlite', 'allowed_tables': []}]
    monkeypatch.setattr(web, 'check_databases', lambda *a: {'ok': False, 'databases': []})
    monkeypatch.setattr(web, 'activate_settings', lambda *a, **k: pytest.fail('must not save failed preflight'))
    assert request(configured, 'settings_save', params)['error']['code'] == 'preflight_failed'


def test_sqlite_discover_select_preflight_and_save(configured, tmp_path, monkeypatch):
    db = tmp_path / 'example.db'
    with sqlite3.connect(db) as connection:
        connection.executescript("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL); INSERT INTO notes VALUES (1, 'private row body');")
    source = {'id': 'notes', 'kind': 'sqlite', 'path': str(db), 'allowed_tables': [], 'allowed_columns': {}}
    discovery = request(configured, 'db_discover', {'source': source})
    assert discovery['result']['ok']
    assert discovery['result']['tables'][0]['table'] == 'notes'
    assert 'private row body' not in json.dumps(discovery)
    proposal = request(configured, 'db_propose', {'source': source, 'selections': [
        {'table': 'notes', 'columns': ['id', 'body'], 'index_text_columns': ['body'], 'id_column': 'id'}]})
    assert proposal['result']['ok']
    selected = proposal['result']['source']
    assert request(configured, 'db_preflight', {'source': selected})['result']['ok']
    params = edit(configured)
    params['values']['databases'] = [selected]
    def apply(path, current, candidate, fingerprint, **kwargs):
        from data_search.preflight import require_preflight
        require_preflight(current, candidate, fingerprint)
        atomic_json(path, candidate)
    monkeypatch.setattr(web, 'activate_settings', apply)
    saved = request(configured, 'settings_save', params)
    assert saved['ok'] and saved['result']['values']['databases'][0]['allowed_columns']['notes'] == ['id', 'body']


def test_browser_source_never_returns_nested_tls_secret(configured):
    current = load_config(configured)
    current['databases'] = [{'id': 'example', 'kind': 'mysql', 'host': 'localhost', 'user': 'reader',
        'database': 'example', 'ssl': {'ca': '/ca.pem', 'password': 'secret-private-key'},
        'internal_password': 'another-secret', 'allowed_tables': []}]
    atomic_json(configured, current)
    params = edit(configured)
    assert 'secret-private-key' not in json.dumps(params)
    assert 'another-secret' not in json.dumps(params)
    _, _, candidate = web._candidate(configured, params)
    assert candidate['databases'][0]['ssl']['password'] == 'secret-private-key'
    assert candidate['databases'][0]['internal_password'] == 'another-secret'


@pytest.mark.parametrize('action,params', [
    ('settings_save', {'revision': 'wrong', 'values': {}}),
    ('db_discover', {'source': {'id': 'x', 'kind': 'mysql', 'password': 'do-not-echo'}}),
    ('credential_store', {'secret': 15}),
    ('model_import', {'source': None}),
    ('__dict__', {}), ('settings_get', {'command': 'whoami'}),
])
def test_invalid_actions_are_bounded_and_do_not_echo_secrets(configured, action, params):
    result = request(configured, action, params)
    assert result['ok'] is False
    assert 'do-not-echo' not in json.dumps(result)


def test_credential_is_stdin_only_and_returned_as_reference(configured, monkeypatch):
    seen = []
    monkeypatch.setattr(web, 'store_credential', lambda secret, reference: seen.append(secret) or 'opaque-reference')
    result = request(configured, 'credential_store', {'secret': 'do-not-echo'})
    assert seen == ['do-not-echo']
    assert result == {'ok': True, 'result': {'credential_ref': 'opaque-reference', 'configured': True}}


def test_failed_activation_restores_old_config(configured, monkeypatch):
    import data_search.service as service
    current = load_config(configured)
    before = json.loads(configured.read_text(encoding='utf-8'))
    candidate = copy.deepcopy(current)
    candidate['exclude_names'].append('new')
    calls = []
    monkeypatch.setattr(service, 'stop_service', lambda _: calls.append('stop'))
    def start(config):
        calls.append('start')
        if 'new' in config['exclude_names']:
            raise ServiceError('synthetic failed start')
        return {'status': 'running'}
    monkeypatch.setattr(service, 'start_service', start)
    with pytest.raises(ServiceError):
        activate_settings(configured, current, candidate, expected_revision=settings_revision(configured))
    assert json.loads(configured.read_text(encoding='utf-8')) == before
    assert calls == ['stop', 'start', 'stop', 'start']


def test_cli_bounds_input_and_reports_machine_envelope(configured, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('x' * (web.MAX_REQUEST_BYTES + 1)))
    assert web.main(configured) == 0
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'invalid_request'
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'action': 'settings_get'})))
    assert web.main(configured) == 0
    assert json.loads(capsys.readouterr().out)['ok']


def test_service_actions_are_explicit_and_force_is_separate(configured, monkeypatch):
    from data_search import service_control
    calls = []
    monkeypatch.setattr(service_control, 'start', lambda config, **kw: calls.append(('start', kw)) or {'status': 'running'})
    monkeypatch.setattr(service_control, 'stop', lambda config, **kw: calls.append(('stop', kw)) or {'status': 'stopped'})
    for action in ['service_start', 'service_stop', 'service_force_stop']:
        assert request(configured, action)['ok']
    assert calls == [('start', {'reason': 'manual'}), ('stop', {'force': False}), ('stop', {'force': True})]
    assert request(configured, 'service_start', {'command': 'untrusted'})['error']['code'] == 'invalid_request'


def test_service_control_errors_retain_actionable_code(configured, monkeypatch):
    from data_search import service_control
    def failed(*args, **kwargs):
        raise service_control.ServiceControlError('service_process_mismatch', 'Process identity changed; no process was terminated')
    monkeypatch.setattr(service_control, 'stop', failed)
    result = request(configured, 'service_force_stop')
    assert not result['ok'] and result['error']['code'] == 'service_process_mismatch'


def test_schedule_crud_works_while_daemon_is_stopped(configured, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from data_search import service_control, service_schedules
    from test_service_schedules import FakeScheduler
    adapter = FakeScheduler()
    monkeypatch.setattr(service_schedules, 'SystemScheduler', lambda _: adapter)
    service_control.stop(load_config(configured))
    initial = request(configured, 'schedules_get')['result']
    assert initial['tasks'] == []
    task = {'name': 'Schedule fixture', 'enabled': True,
            'schedule': {'kind': 'once', 'at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}
    saved = request(configured, 'schedule_save', {'revision': initial['revision'], 'task': task})
    assert saved['ok'] and saved['result']['tasks'][0]['name'] == 'Schedule fixture'
    assert service_control.read_control(load_config(configured))['desired_state'] == 'stopped'
    stale = request(configured, 'schedule_save', {'revision': initial['revision'], 'task': task})
    assert stale['error']['code'] == 'schedule_conflict'
    deleted = request(configured, 'schedule_delete', {'revision': saved['result']['revision'],
                                                     'id': saved['result']['tasks'][0]['id']})
    assert deleted['ok'] and deleted['result']['tasks'] == [] and not adapter.tasks


@pytest.mark.parametrize('revision', [True, -1, '1', None])
def test_schedule_mutation_requires_integer_revision(configured, revision):
    assert request(configured, 'schedule_delete', {'revision': revision, 'id': 'fake'})['error']['code'] == 'invalid_request'


def test_settings_save_while_stopped_preserves_stop_and_applies_candidate(configured, monkeypatch):
    from data_search import service, service_control
    service_control.stop(load_config(configured))
    before = service_control.read_control(load_config(configured))
    params = edit(configured)
    params['values']['exclude_names'].append('offline-save')
    monkeypatch.setattr(service, 'start_service', lambda *_: pytest.fail('Saving settings must not clear explicit stop'))
    result = request(configured, 'settings_save', params)
    assert result['ok'] and 'offline-save' in load_config(configured)['exclude_names']
    assert service_control.read_control(load_config(configured)) == before


def test_stop_during_settings_activation_wins_without_rollback(configured, monkeypatch):
    from data_search import service, service_control
    current = load_config(configured)
    candidate = copy.deepcopy(current)
    candidate['exclude_names'].append('accepted-before-stop')
    monkeypatch.setattr(service, 'stop_service', lambda _, **kwargs: {'status': 'stopped'})
    def start(config):
        service_control.stop(config)
        raise service_control.ServiceControlError('service_stopped', 'Stopped concurrently')
    monkeypatch.setattr(service, 'start_service', start)
    result = activate_settings(configured, current, candidate)
    assert result['status'] == 'stopped'
    assert 'accepted-before-stop' in load_config(configured)['exclude_names']
    assert service_control.read_control(current)['desired_state'] == 'stopped'


def test_control_failure_restores_config_without_restarting_invalid_state(configured, monkeypatch):
    from data_search import service, service_control
    current = load_config(configured)
    before = json.loads(configured.read_text(encoding='utf-8'))
    candidate = copy.deepcopy(current)
    candidate['exclude_names'].append('invalid-control-race')
    calls = []
    monkeypatch.setattr(service, 'stop_service', lambda _: calls.append('stop'))
    def start(config):
        calls.append('start')
        (Path(config['data_dir']) / 'service-control.json').write_text('{corrupt')
        raise service_control.ServiceControlError('service_control_invalid', 'Malformed control state')
    monkeypatch.setattr(service, 'start_service', start)
    with pytest.raises(service_control.ServiceControlError):
        activate_settings(configured, current, candidate)
    assert json.loads(configured.read_text(encoding='utf-8')) == before


def test_invalid_control_prevents_settings_mutation(configured, monkeypatch):
    from data_search import service, service_control
    current = load_config(configured)
    before = configured.read_bytes()
    (Path(current['data_dir']) / 'service-control.json').write_text('{corrupt')
    monkeypatch.setattr(service, 'stop_service', lambda _: pytest.fail('Invalid intent must fail before stopping'))
    candidate = copy.deepcopy(current)
    candidate['exclude_names'].append('not-saved')
    with pytest.raises(service_control.ServiceControlError):
        activate_settings(configured, current, candidate)
    assert configured.read_bytes() == before
