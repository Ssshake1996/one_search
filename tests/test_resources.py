import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from data_search import cli, resource_control
from data_search.config import defaults, load_config
from data_search.resources import Budget, ResourceLimit
from data_search.scope import FileScope
from data_search.service import InstanceLock
from data_search.store import Store


def config_file(tmp_path):
    config = defaults(str(tmp_path / 'data'), [])
    config['semantic']['enabled'] = False
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    return config, path


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object integration')
def test_windows_worker_commit_limit_and_cpu_settings(tmp_path):
    config, _ = config_file(tmp_path)
    config['resource']['worker_memory_mb'] = 128
    script = "import sys; sys.stdin.readline();\ntry:\n b=bytearray(256*1024*1024); print('unexpected',flush=True)\nexcept MemoryError:\n print('limited',flush=True)"
    process = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    control = None
    try:
        control = resource_control.attach_worker(process.pid, config)
        assert control.status['hard_memory_limit_mb'] == 128, control.status
        assert control.status['hard_cpu_percent'] == 25, control.status
        applied = control.job.query()
        assert applied['memory_bytes'] == 128 * 1048576
        assert applied['memory_flags'] & 0x100
        assert applied['cpu_flags'] & 0x5 == 0x5
        assert applied['cpu_rate'] == 2500
        output, errors = process.communicate('\n', timeout=15)
        assert process.returncode == 0, errors
        assert output.strip() == 'limited'
        assert len(control.status['affinity_cpus']) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if control:
            control.close()
            control.close()
            assert control.status['active'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows kill-on-job-close integration')
def test_windows_controller_close_terminates_attached_worker(tmp_path):
    config, _ = config_file(tmp_path)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    control = None
    try:
        control = resource_control.attach_worker(process.pid, config)
        assert control.job is not None, control.status
        assert process.poll() is None
        control.close()
        # Windows may report exit code 0 for kill-on-job-close; prompt exit of
        # this 20-second sleeper is the property we need to verify.
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if control:
            control.close()


def test_permission_failures_are_observable_not_fatal(tmp_path, monkeypatch):
    config, _ = config_file(tmp_path)
    class DeniedProcess:
        def __init__(self, pid):
            pass
        def nice(self, value):
            raise resource_control.psutil.AccessDenied(123)
        def ionice(self, value):
            raise resource_control.psutil.AccessDenied(123)
        def cpu_affinity(self, value=None):
            raise resource_control.psutil.AccessDenied(123)
    monkeypatch.setattr(resource_control.psutil, 'Process', DeniedProcess)
    def denied_job(*args):
        raise OSError('job denied')
    monkeypatch.setattr(resource_control, '_WindowsJob', denied_job)
    control = resource_control.attach_worker(123, config)
    assert control.status['hard_memory_limit_mb'] is None
    assert control.status['hard_cpu_percent'] is None
    assert len(control.status['fallback_errors']) >= 3
    control.close()


def test_linux_style_controls_do_not_claim_hard_limits(tmp_path, monkeypatch):
    config, _ = config_file(tmp_path)
    calls = []
    class Process:
        def __init__(self, pid):
            pass
        def nice(self, value):
            calls.append(('nice', value))
        def ionice(self, value):
            calls.append(('io', value))
        def cpu_affinity(self, value=None):
            if value is None:
                return [2, 3, 4]
            calls.append(('affinity', value))
    monkeypatch.setattr(resource_control, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(resource_control.psutil, 'Process', Process)
    monkeypatch.setattr(resource_control.psutil, 'IOPRIO_CLASS_IDLE', 3, raising=False)
    control = resource_control.attach_worker(123, config)
    assert ('nice', 10) in calls and ('affinity', [2]) in calls
    assert control.status['hard_memory_limit_mb'] is None
    assert control.status['hard_cpu_percent'] is None


def test_offline_compact_refuses_active_instance(tmp_path, capsys):
    config, path = config_file(tmp_path)
    store = Store(config['data_dir'])
    store.close()
    with InstanceLock(Path(config['data_dir']) / 'service.lock'):
        assert cli.main(['compact', '--config', str(path)]) == 1
    assert 'already owns' in capsys.readouterr().err


def test_compact_holds_lock_and_checks_space_before_mutation(tmp_path, monkeypatch, capsys):
    config, path = config_file(tmp_path)
    store = Store(config['data_dir'])
    store.close()
    checked = []
    original = Store.compact
    def compact(self):
        with pytest.raises(Exception, match='already owns'):
            with InstanceLock(Path(config['data_dir']) / 'service.lock'):
                pass
        assert checked and checked[0] > 16
        return original(self)
    def budget_check(self, disk=False, reserve_mb=0):
        assert disk
        checked.append(reserve_mb)
    monkeypatch.setattr(Store, 'compact', compact)
    monkeypatch.setattr(Budget, 'check', budget_check)
    assert cli.main(['compact', '--config', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'compacted'


def test_compact_space_failure_preserves_database(tmp_path, monkeypatch, capsys):
    config, path = config_file(tmp_path)
    store = Store(config['data_dir'])
    store.set_setting('preserved', 'value')
    store.close()
    def no_space(*args, **kwargs):
        raise ResourceLimit('disk_free_space_low')
    monkeypatch.setattr(Budget, 'check', no_space)
    assert cli.main(['compact', '--config', str(path)]) == 1
    capsys.readouterr()
    store = Store(config['data_dir'])
    try:
        assert store.setting('preserved') == 'value'
    finally:
        store.close()


def test_model_directory_outside_data_is_excluded(tmp_path):
    root = tmp_path / 'files'
    model = root / 'model'
    model.mkdir(parents=True)
    config = defaults(str(tmp_path / 'data'), [str(root)])
    config['semantic']['model_dir'] = str(model)
    files = FileScope(config)
    assert not files.allowed(model / 'weights.onnx')
    assert files.allowed(root / 'document.txt')


@pytest.mark.parametrize('key,value', [('worker_memory_mb',0), ('worker_memory_mb',32),
                                     ('worker_cpu_percent',0), ('worker_cpu_percent',101),
                                     ('worker_cpu_percent',True)])
def test_worker_limits_validated(tmp_path, key, value):
    config, path = config_file(tmp_path)
    config['resource'][key] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=key):
        load_config(path)
