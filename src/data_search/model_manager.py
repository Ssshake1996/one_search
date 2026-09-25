"""Recoverable detached model installation, separate from daemon readiness."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from .config import load_config
from .model import ASSETS, MODEL_ID, ModelCancelled, download_model, model_ready
from .runtime import process_command
from .service import InstanceLock, ServiceError

ACTIVE = {'queued', 'running', 'cancelling'}


def _directory(config):
    return Path(config['data_dir']) / 'model-job'


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _worker_running(directory):
    try:
        with InstanceLock(directory / 'worker.lock'):
            return False
    except ServiceError:
        return True


def _asset_signatures(directory):
    result = {}
    for name in [*ASSETS, 'manifest.json']:
        try:
            stat = (Path(directory) / name).stat()
            result[name] = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            result[name] = None
    return result


def model_status(config):
    directory = _directory(config)
    state = _read(directory / 'status.json')
    # Never report an old job for a newly selected model location.
    model_dir = str(Path(config['semantic']['model_dir']).resolve())
    if state.get('model_dir') != model_dir:
        state = {}
    if state.get('state') == 'ready' and state.get('asset_signatures') != _asset_signatures(model_dir):
        state = {**state, 'state': 'failed', 'error': {'code': 'assets_changed', 'message': 'Model assets changed after verification; start a new model job to verify or repair them'}}
    if state.get('state') in ACTIVE and not _worker_running(directory):
        if state['state'] != 'queued' or time.time() - state.get('updated_at', 0) > 30:
            state = {**state, 'state': 'interrupted', 'error': {'code': 'interrupted', 'message': 'Model job stopped before completion'}}
    ready = model_ready(model_dir)
    default_state = 'ready' if ready else ('missing' if config['semantic']['enabled'] else 'disabled')
    result = {'schema_version': 1, 'model_id': MODEL_ID, 'model_dir': model_dir, 'state': default_state,
              'basic_search_available_without_model': True, 'progress': {}, **state, 'ready': ready,
              'enabled': bool(config['semantic']['enabled'])}
    if result['state'] == 'failed':
        result['ready'] = False
    elif ready and result['state'] not in ACTIVE:
        result['state'] = 'ready'
    result['verification'] = 'verified_by_job' if state.get('state') == 'ready' else 'verified_when_model_loads'
    result['recommended_action'] = ({'missing': 'model-start or model-import', 'failed': 'model-start to retry, or model-import',
                                    'interrupted': 'model-start to retry', 'cancelled': 'model-start to retry',
                                    'disabled': 'Enable semantic indexing in settings, then model-start'}.get(result['state']))
    return result


def start_model_job(config, source_directory=None):
    config_path = config.get('config_path')
    if not config_path:
        raise ValueError('Model jobs require a saved configuration path')
    source = str(Path(source_directory).expanduser().resolve()) if source_directory else None
    if source and not Path(source).is_dir():
        raise ValueError('Offline model directory must exist')
    directory = _directory(config)
    with InstanceLock(directory / 'manager.lock'):
        state = model_status(config)
        if state['state'] in ACTIVE or (state['ready'] and state.get('job_id')):
            return {**state, 'reused': True}
        if _worker_running(directory):
            raise ValueError('Another model location is being installed; cancel it before changing model location')
        job_id = secrets.token_hex(12)
        state = {'schema_version': 1, 'job_id': job_id, 'state': 'queued', 'model_id': MODEL_ID,
                 'model_dir': str(Path(config['semantic']['model_dir']).resolve()),
                 'started_at': time.time(), 'updated_at': time.time(), 'progress': {}, 'error': None,
                 'operation': 'import' if source else 'download'}
        _write(directory / 'request.json', {'job_id': job_id, 'source_directory': source, 'model_dir': state['model_dir']})
        _write(directory / 'status.json', state)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0
        try:
            child = subprocess.Popen(process_command('data_search.model_manager', 'worker', '--config', str(config_path), '--job', job_id),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, start_new_session=os.name != 'nt', close_fds=True)
        except OSError:
            _write(directory / 'status.json', {**state, 'state': 'failed', 'error': {'code': 'launch_failed', 'message': 'Unable to launch model worker'}})
            raise
        return {**state, 'ready': False, 'pid': child.pid, 'reused': False}


def cancel_model_job(config):
    directory = _directory(config)
    state = model_status(config)
    if state['state'] in ACTIVE:
        _write(directory / 'cancel.json', {'job_id': state['job_id']})
        return {**state, 'state': 'cancelling', 'cancellation_max_wait_seconds': 20}
    return state


def wait_for_model_idle(config, timeout=30):
    """Cancel an owned job before moving its runtime or data; never kill another PID."""
    state = cancel_model_job(config)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = model_status(config)
        if state['state'] not in ACTIVE and not _worker_running(_directory(config)):
            return state
        time.sleep(.1)
    raise ValueError('Model preparation is still stopping; retry maintenance after model-status reports cancelled')


def download_model_blocking(config):
    """Compatibility command shares admission with async jobs and maintenance."""
    directory = _directory(config)
    with InstanceLock(directory / 'manager.lock'):
        wait_for_model_idle(config)
        with InstanceLock(directory / 'worker.lock'):
            model_dir = str(Path(config['semantic']['model_dir']).resolve())
            result = download_model(model_dir)
            _write(directory / 'status.json', {'schema_version': 1, 'model_id': MODEL_ID, 'model_dir': model_dir,
                'job_id': secrets.token_hex(12), 'state': 'ready', 'updated_at': time.time(), 'completed_at': time.time(),
                'error': None, 'progress': {'phase': 'complete'}, 'asset_signatures': _asset_signatures(model_dir)})
            return result


def run_model_job(config, job_id):
    directory = _directory(config)
    with InstanceLock(directory / 'worker.lock'):
        request = _read(directory / 'request.json')
        state = _read(directory / 'status.json')
        if request.get('job_id') != job_id or state.get('job_id') != job_id:
            return
        last_write = [0.0]
        def save(force=False):
            now = time.time()
            if force or now - last_write[0] >= .25:
                state['updated_at'] = now
                _write(directory / 'status.json', state)
                last_write[0] = now
        def progress(values):
            state['progress'].update(values)
            save()
        def cancelled():
            return _read(directory / 'cancel.json').get('job_id') == job_id
        state.update(state='running', pid=os.getpid())
        save(True)
        try:
            download_model(request['model_dir'], progress=progress, cancelled=cancelled,
                           source_directory=request.get('source_directory'))
            state.update(state='ready', completed_at=time.time(), error=None)
            state['asset_signatures'] = _asset_signatures(request['model_dir'])
            state['progress']['phase'] = 'complete'
        except ModelCancelled:
            state.update(state='cancelled', error=None)
        except Exception as error:
            # Paths, signed download URLs, environment and credentials stay out of diagnostics.
            code = 'checksum_or_model_invalid' if isinstance(error, ValueError) else 'network_or_file_error'
            state.update(state='failed', error={'code': code, 'message': 'Model preparation failed; retry or import verified offline assets',
                                                'type': type(error).__name__})
        save(True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['start', 'status', 'cancel', 'worker', 'quiesce'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--source')
    parser.add_argument('--job')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.operation == 'worker':
        if not args.job:
            parser.error('--job is required for worker')
        run_model_job(config, args.job)
        return 0
    result = (start_model_job(config, args.source) if args.operation == 'start' else
              cancel_model_job(config) if args.operation == 'cancel' else
              wait_for_model_idle(config) if args.operation == 'quiesce' else model_status(config))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
