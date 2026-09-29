"""Durable user intent, separate from a daemon's transient connection state.

Only explicit starts clear a stop. All automatic launch paths and the daemon
itself check the same revision, so a delayed reconnect cannot undo a newer stop.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import time
import uuid

import psutil

from .runtime_policy import _write_json
from .service import InstanceLock, ServiceError


class ServiceControlError(ServiceError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@contextmanager
def control_lock(config, name='service-control.lock', *, timeout=10):
    lock = InstanceLock(Path(config['data_dir']) / name)
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.__enter__()
            break
        except ServiceError:
            if time.monotonic() >= deadline:
                raise ServiceControlError('service_control_busy', 'Another service control operation is in progress') from None
            time.sleep(.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def read_control(config):
    path = Path(config['data_dir']) / 'service-control.json'
    try:
        if path.stat().st_size > 8192:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or
                value.get('desired_state') not in {'running', 'stopped'} or
                value.get('reason') not in {'manual', 'forced', 'scheduled', 'automatic'} or
                not isinstance(value.get('revision'), str) or not 1 <= len(value['revision']) <= 160 or
                type(value.get('updated_at')) not in (int, float) or
                not math.isfinite(value['updated_at'])):
            raise ValueError()
        return {key: value[key] for key in ('schema_version', 'desired_state', 'revision', 'reason', 'updated_at')}
    except FileNotFoundError:
        return {'schema_version': 1, 'desired_state': 'running', 'revision': 'initial',
                'reason': 'automatic', 'updated_at': 0}
    except (OSError, ValueError, TypeError, OverflowError):
        raise ServiceControlError('service_control_invalid', 'Service control state cannot be read safely; automatic startup is disabled') from None


def _write_control(config, state, reason):
    value = {'schema_version': 1, 'desired_state': state, 'revision': uuid.uuid4().hex,
             'reason': reason, 'updated_at': time.time()}
    _write_json(Path(config['data_dir']) / 'service-control.json', value)
    return value


def require_running(config, revision=None):
    value = read_control(config)
    if value['desired_state'] != 'running':
        raise ServiceControlError('service_stopped', 'Service was stopped by the user; use Start or an enabled scheduled task to run it again')
    if revision is not None and value['revision'] != revision:
        raise ServiceControlError('service_control_changed', 'A newer service control operation superseded this startup')
    return value


def control_status(config):
    from .service import service_status
    value = read_control(config)
    try:
        health = service_status(config)
        return {**value, 'status': health['status'], 'service': health}
    except ServiceError:
        return {**value, 'status': 'stopped' if value['desired_state'] == 'stopped' else 'unavailable', 'service': None}


def start(config, *, reason='manual', timeout=30):
    from .service import start_service
    if reason not in {'manual', 'scheduled', 'automatic'}:
        raise ValueError('Invalid service start reason')
    with control_lock(config):
        value = require_running(config) if reason == 'automatic' else _write_control(config, 'running', reason)
    try:
        result = start_service(config, timeout=timeout, control_revision=value['revision'])
    except ServiceControlError:
        raise
    except ServiceError:
        raise ServiceControlError('service_start_failed', 'Service did not become ready; inspect daemon.log and retry Start') from None
    _notify_hosts(config)
    return {**result, 'service_control': read_control(config)}


def _notify_hosts(config):
    # Registry validation and authenticated loopback requests are shared with
    # upgrade coordination, but this does not enter its maintenance barrier.
    from concurrent.futures import ThreadPoolExecutor
    from .upgrade_hosts import UpgradeSession, _control
    try:
        hosts = UpgradeSession(Path(config['data_dir']).parent, config['data_dir']).hosts_now()
        if hosts:
            with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
                futures = [pool.submit(_control, host, 'service_sync', None, timeout=2) for host in hosts]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        pass  # Durable intent and the host poll remain authoritative.
    except Exception:
        pass


def _process_marker(config):
    path = Path(config['data_dir']) / 'service-process.json'
    try:
        if path.stat().st_size > 16384:
            raise ValueError()
        marker = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ServiceControlError('service_process_invalid', 'Daemon process identity is unavailable; no process was terminated') from None
    if (not isinstance(marker, dict) or marker.get('role') != 'data_search_daemon' or
            marker.get('config_path') != str(Path(config['config_path']).resolve()) or
            type(marker.get('pid')) is not int or marker['pid'] <= 0 or
            type(marker.get('create_time')) not in (int, float) or
            not 0 < marker['create_time'] < 1e12 or
            not isinstance(marker.get('service_id'), str) or not marker['service_id']):
        raise ServiceControlError('service_process_invalid', 'Daemon process identity does not match this instance; no process was terminated')
    return marker


def _owned_process(config, marker):
    """Never trust a PID alone: birth time, role and exact config must match."""
    if marker['pid'] == os.getpid():
        raise ServiceControlError('service_process_mismatch', 'Refusing to terminate the current control process')
    try:
        process = psutil.Process(marker['pid'])
        if abs(process.create_time() - marker['create_time']) > .001:
            raise ValueError()
        args = process.cmdline()
        role = args.index('daemon')
        argument = args.index('--config', role + 1)
        if str(Path(args[argument + 1]).resolve()) != str(Path(config['config_path']).resolve()):
            raise ValueError()
        return process
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, ValueError, IndexError, OSError):
        raise ServiceControlError('service_process_mismatch', 'Daemon process identity changed; no process was terminated') from None


def _force_stop(config, *, timeout=10):
    marker = _process_marker(config)
    if marker is None:
        # Older runtimes cannot be safely killed from PID-only service.json.
        from .service import stop_service
        result = stop_service(config, timeout=timeout)
        try:
            with InstanceLock(Path(config['data_dir']) / 'service.lock'):
                return result
        except ServiceError:
            raise ServiceControlError('service_process_identity_missing', 'The daemon is still running without a verifiable process marker; automatic startup is disabled, but force termination was refused') from None
    process = _owned_process(config, marker)
    if process is None:
        return {'status': 'not_running', 'stopped': False}
    # Freeze only the verified daemon before enumerating workers. This prevents
    # a hung parent spawning a new worker between enumeration and termination.
    suspended = False
    try:
        process.suspend()
        suspended = True
        children = []
        for child in process.children(recursive=True):
            try:
                args = child.cmdline()
                if 'data_search.worker' in args:
                    children.append(child)
            except psutil.NoSuchProcess:
                continue
        for child in reversed(children):
            try:
                child.kill()  # psutil rechecks PID/create_time before destructive calls.
            except psutil.NoSuchProcess:
                pass
        process.kill()
        suspended = False
        _, alive = psutil.wait_procs([*children, process], timeout=timeout)
        if alive:
            raise ServiceControlError('service_stop_timeout', 'Owned processes have not exited yet; automatic startup remains disabled')
        from .service import _remove_owned_state
        _remove_owned_state(Path(config['data_dir']) / 'service.json', marker['service_id'])
        _remove_owned_state(Path(config['data_dir']) / 'service-process.json', marker['service_id'])
        return {'status': 'stopped', 'stopped': True, 'forced': True}
    except psutil.NoSuchProcess:
        return {'status': 'stopped', 'stopped': True, 'forced': True}
    except psutil.Error:
        raise ServiceControlError('service_stop_denied', 'Operating system rejected stopping the verified daemon; automatic startup remains disabled') from None
    finally:
        if suspended:
            try:
                process.resume()
            except psutil.Error:
                pass


def stop(config, *, force=False, timeout=15):
    from .service import stop_service
    # Persist before waiting for a launch or a slow RPC. Reconnects immediately
    # lose permission even when the process being stopped is unresponsive.
    with control_lock(config):
        value = _write_control(config, 'stopped', 'forced' if force else 'manual')
    _notify_hosts(config)
    with control_lock(config, 'service-action.lock', timeout=max(timeout, 35)):
        if read_control(config)['revision'] != value['revision']:
            return {'status': 'superseded', 'stopped': False, 'service_control': read_control(config)}
        try:
            result = _force_stop(config, timeout=timeout) if force else stop_service(config, timeout=timeout)
        except ServiceControlError:
            raise
        except ServiceError:
            raise ServiceControlError('service_stop_timeout', 'Service did not stop cleanly; automatic startup is disabled, and Force stop remains available') from None
        if not force:
            # A daemon can still be constructing Engine before RPC publication.
            # It checks the same intent again before beginning background work.
            deadline = time.monotonic() + timeout
            while True:
                try:
                    with InstanceLock(Path(config['data_dir']) / 'service.lock'):
                        break
                except ServiceError:
                    if time.monotonic() >= deadline:
                        raise ServiceControlError('service_stop_timeout', 'Service is still stopping; use Force stop if it does not exit') from None
                    time.sleep(.05)
        return {**result, 'service_control': read_control(config)}
