"""Bounded, local management commands used by the authenticated DSH host bridge.

The browser never receives raw configuration, service state or credentials.
Configuration application lives outside the daemon it needs to restart.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

from .config import load_config
from .credentials import CredentialError, store_credential
from .databases import DatabaseError, DatabaseSource
from .preflight import check_database, check_databases
from .runtime_policy import apply_preset, validate_policy
from .service import ServiceError, rpc, service_status
from .setup_ui import SettingsConflict, activate_settings, settings_config, settings_revision
from .source_setup import discover_source, propose_source


MAX_REQUEST_BYTES = 128 * 1024
SOURCE_KEYS = frozenset(('id', 'kind', 'path', 'host', 'port', 'database', 'user',
    'password_env', 'credential_ref', 'ssl', 'allowed_tables', 'allowed_columns',
    'index', 'index_max_rows', 'max_rows', 'max_result_chars', 'query_timeout_seconds',
    'sync', 'business_metadata'))
TLS_KEYS = frozenset(('ca', 'cert', 'key', 'check_hostname', 'verify_mode',
                     'sslmode', 'sslrootcert', 'sslcert', 'sslkey'))
VALUE_KEYS = frozenset(('scope', 'roots', 'exclude_paths', 'exclude_names',
    'semantic_enabled', 'indexing', 'runtime_policy', 'databases', 'preset'))
ACTION_FIELDS = {
    'settings_get': set(), 'settings_preview': {'revision', 'values'},
    'settings_save': {'revision', 'values'}, 'db_discover': {'source'},
    'db_propose': {'source', 'selections', 'business_metadata'},
    'db_preflight': {'source'}, 'credential_store': {'secret', 'reference'},
    'model_start': set(), 'model_import': {'source'}, 'model_cancel': set(),
}


class ManagementError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details


def _public_source(source):
    value = {key: deepcopy(item) for key, item in source.items() if key in SOURCE_KEYS}
    if 'ssl' in value:
        value['ssl'] = {key: item for key, item in (value['ssl'] or {}).items() if key in TLS_KEYS}
    return value


def _source(source, current):
    if not isinstance(source, dict) or set(source) - SOURCE_KEYS:
        raise ManagementError('invalid_source', '数据库来源字段无效；密码请保存到系统凭据库。')
    if 'ssl' in source and (not isinstance(source['ssl'], dict) or set(source['ssl']) - TLS_KEYS):
        raise ManagementError('invalid_source', 'TLS 配置只接受证书路径及验证选项。')
    existing = next((item for item in current['databases']
                     if item['id'] == source.get('id') and item['kind'] == source.get('kind')), {})
    # Retain unexposed advanced options on existing sources. An omitted public
    # field is an explicit removal, e.g. switching vault to environment auth.
    candidate = {key: deepcopy(value) for key, value in existing.items() if key not in SOURCE_KEYS}
    candidate.update(deepcopy(source))
    hidden_tls = {key: value for key, value in (existing.get('ssl') or {}).items() if key not in TLS_KEYS}
    if hidden_tls:
        candidate['ssl'] = {**hidden_tls, **candidate.get('ssl', {})}
    DatabaseSource(candidate)
    return candidate


def snapshot(config_path):
    path = Path(config_path).resolve()
    # Read a stable pair so a concurrent save cannot label old values with a new
    # revision. A later change is caught again by the apply transaction.
    for _ in range(3):
        revision = settings_revision(path)
        current = load_config(path)
        if revision == settings_revision(path):
            break
    else:
        raise ManagementError('settings_busy', '设置正在更新，请稍后重新读取。')
    values = {key: deepcopy(current[key]) for key in ('scope', 'roots', 'exclude_paths',
                                                     'exclude_names', 'indexing', 'runtime_policy')}
    values.update(semantic_enabled=current['semantic']['enabled'],
                  databases=[_public_source(source) for source in current['databases']],
                  preset=current['runtime_policy']['preset'])
    try:
        health = service_status(current)
        state = {'status': health['status']}
    except ServiceError:
        state = {'status': 'unavailable'}
    return {'revision': revision, 'values': values, 'resource': current['resource'],
            'node_id': current['node_id'], 'service': state}


def _candidate(path, params):
    revision = params.get('revision')
    if not isinstance(revision, str) or revision != settings_revision(path):
        raise ManagementError('revision_conflict', '设置已被其他页面修改，请重新读取后再保存。')
    current = load_config(path)
    if revision != settings_revision(path):
        raise ManagementError('revision_conflict', '设置已更新，请重新读取。')
    values = params.get('values')
    if not isinstance(values, dict) or set(values) != VALUE_KEYS:
        raise ManagementError('invalid_settings', '请提交完整的设置表单，不支持修改内部路径或运行命令。')
    if not isinstance(values['semantic_enabled'], bool):
        raise ManagementError('invalid_settings', '语义索引开关必须为布尔值。')
    if not isinstance(values['databases'], list) or len(values['databases']) > 64:
        raise ManagementError('invalid_settings', '最多配置 64 个数据库来源。')
    if not isinstance(values['indexing'], dict) or set(values['indexing']) - set(current['indexing']):
        raise ManagementError('invalid_settings', '正文和语义范围设置无效。')
    clean = deepcopy(values)
    clean['databases'] = [_source(source, current) for source in values['databases']]
    clean['runtime_policy'] = validate_policy({**values['runtime_policy'], 'preset': values['preset']})
    base = (apply_preset(current, values['preset'])
            if values['preset'] != current['runtime_policy']['preset'] else current)
    candidate = settings_config(base, clean)
    return revision, current, candidate


def _preflight(current, candidate):
    changed = current['databases'] != candidate['databases']
    report = check_databases(candidate['databases'], 30) if changed else None
    return changed, report


def dispatch(config_path, request):
    """Return only an allowlisted application envelope; never return driver errors."""
    try:
        if not isinstance(request, dict) or set(request) - {'action', 'params'}:
            raise ManagementError('invalid_request', '管理请求格式无效。')
        action, params = request.get('action'), request.get('params', {})
        if not isinstance(action, str) or action not in ACTION_FIELDS:
            raise ManagementError('unsupported_action', '不支持此管理操作。')
        if not isinstance(params, dict) or set(params) - ACTION_FIELDS[action]:
            raise ManagementError('invalid_request', '管理操作参数无效。')
        path = Path(config_path).expanduser().resolve()
        if action == 'settings_get':
            result = snapshot(path)
        elif action in {'settings_preview', 'settings_save'}:
            revision, current, candidate = _candidate(path, params)
            changed, checked = _preflight(current, candidate)
            if action == 'settings_preview':
                try:
                    impact = rpc(current, 'scope_preview', {'changes': {key: candidate[key]
                        for key in ('scope', 'roots', 'exclude_paths', 'exclude_names', 'indexing')}}, timeout=10)
                except ServiceError:
                    impact = {'available': False, 'note': '服务不可用，暂时无法估算已知资料的范围变化。'}
                result = {'valid': True, 'scope': impact, 'databases_changed': changed,
                    'preflight': checked, 'can_apply': checked is None or checked['ok'],
                    'preset': candidate['runtime_policy']['preset'], 'resource': candidate['resource']}
            else:
                if checked and not checked['ok']:
                    raise ManagementError('preflight_failed', '数据库只读预检未通过，设置尚未保存。', checked)
                try:
                    activate_settings(path, current, candidate,
                        checked['fingerprint'] if checked else None, expected_revision=revision)
                except SettingsConflict:
                    raise ManagementError('revision_conflict', '设置已被其他页面修改，请重新读取后再保存。') from None
                except ServiceError:
                    raise ManagementError('apply_failed', '设置应用失败或正在被其他操作更新；请重新读取设置并检查服务状态。') from None
                result = {**snapshot(path), 'applied': True}
        else:
            current = load_config(path)
            if action in {'db_discover', 'db_propose', 'db_preflight'}:
                source = _source(params.get('source'), current)
                if action == 'db_discover':
                    result = discover_source(source)
                elif action == 'db_propose':
                    result = propose_source(source, params.get('selections'), params.get('business_metadata'))
                    if result.get('source'):
                        result['source'] = _public_source(result['source'])
                else:
                    result = check_database(source)
            elif action == 'credential_store':
                reference = store_credential(params.get('secret'), params.get('reference'))
                result = {'credential_ref': reference, 'configured': True}
            else:
                from .model_manager import start_model_job, cancel_model_job
                if action == 'model_cancel':
                    result = cancel_model_job(current)
                else:
                    source = params.get('source') if action == 'model_import' else None
                    if action == 'model_import' and (not isinstance(source, str) or not source.strip()):
                        raise ManagementError('invalid_request', '请填写服务所在机器上的离线模型目录。')
                    result = start_model_job(current, source)
        return {'ok': True, 'result': result}
    except ManagementError as error:
        detail = {'code': error.code, 'message': str(error)}
        if error.details is not None:
            detail['details'] = error.details
        return {'ok': False, 'error': detail}
    except (DatabaseError, CredentialError) as error:
        return {'ok': False, 'error': {'code': error.code, 'message': str(error)}}
    except (ValueError, TypeError, KeyError):
        return {'ok': False, 'error': {'code': 'invalid_settings',
            'message': '设置无效，请核对目录、字段类型、资源档位和数据库参数。'}}
    except Exception:
        return {'ok': False, 'error': {'code': 'management_failed',
            'message': '管理操作失败，请检查服务、安装和当前账号的权限。'}}


def main(config_path):
    try:
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.read(MAX_REQUEST_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError()
        request = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        result = {'ok': False, 'error': {'code': 'invalid_request', 'message': '管理请求不是有效的有限长度 JSON。'}}
    else:
        result = dispatch(config_path, request)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0
