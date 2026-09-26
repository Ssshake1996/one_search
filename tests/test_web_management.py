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
