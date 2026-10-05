"""Bounded admission under headroom/pressure, and real parser-child lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
import json
import sys
import threading
import time

import pytest

from data_search.config import defaults, load_config
from data_search.resources import Budget, ResourceLimit
from data_search.runtime_policy import apply_preset
from data_search.workers import ParserPool


def measured_budget(tmp_path, monkeypatch, *, total=32768, available=24000, rss=200, cpu=5):
    config = defaults(str(tmp_path / 'index'), [])
    config['semantic']['enabled'] = False
    budget = Budget(config)
    sample = dict(total=total, available=available, rss=rss, cpu=cpu)

    def measure():
        budget._total_memory = sample['total'] * 1048576
        budget._cpu_count = 16
        budget._cpu_percent = sample['cpu']
        return sample['rss'] * 1048576, sample['available'] * 1048576, 100000 * 1048576

    monkeypatch.setattr(budget, '_measure_memory', measure)
    return budget, sample


def refresh(budget):
    budget._last_memory = -float('inf')
    return budget.work_capacity(backlog=100)


def test_headroom_increases_useful_capacity_without_allocating(tmp_path, monkeypatch):
    budget, sample = measured_budget(tmp_path, monkeypatch, total=8192, available=4096)
    small = refresh(budget)
    sample.update(total=32768, available=24000)
    large = refresh(budget)
    assert small['memory_limit_mb'] == pytest.approx(8192 * .2, abs=.01)
    assert large['memory_limit_mb'] == 4096
    assert large['parser_workers'] > small['parser_workers']
    assert large['batch_files'] > small['batch_files']
    assert large['sqlite_cache_mb'] > small['sqlite_cache_mb']
    assert large['batch_bytes'] <= 256 * 1048576
    # Reporting/admission never starts a parser or fills RAM to a target.
    pool = ParserPool(budget)
    assert pool.proc is None and pool.control_status['workers'] == []
    assert budget.snapshot()['rss_mb'] == 200


def test_pressure_shrinks_then_recovers_capacity(tmp_path, monkeypatch):
    budget, sample = measured_budget(tmp_path, monkeypatch)
    assert refresh(budget)['parser_workers'] == 4
    sample['available'] = 4100
    low = refresh(budget)
    assert low['parser_workers'] == 1 and low['batch_bytes'] == 1048576
    assert low['memory_limit_mb'] < 300
    sample.update(available=24000, cpu=95)
    busy = refresh(budget)
    assert busy['parser_workers'] == 1 and busy['reason'] == 'system_cpu_pressure'
    sample['cpu'] = 5
    assert refresh(budget)['parser_workers'] == 4
    sample['available'] = 500
    refresh(budget)
    with pytest.raises(ResourceLimit, match='system_memory_pressure'):
        budget.check()


def test_cpu_sampling_uses_shared_deltas_across_disposable_threads(tmp_path, monkeypatch):
    from data_search import resources
    budget, _ = measured_budget(tmp_path, monkeypatch)
    CpuTimes = namedtuple('CpuTimes', 'user system idle')
    samples = iter([CpuTimes(10, 0, 90), CpuTimes(19, 0, 91)])
    monkeypatch.setattr(resources.psutil, 'cpu_times', lambda: next(samples))
    for _ in range(2):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(budget._sample_cpu).result(timeout=2)
    assert budget._cpu_percent == 90


def test_limits_remain_hard_and_backlog_does_not_start_extra_children(tmp_path, monkeypatch):
    budget, _ = measured_budget(tmp_path, monkeypatch)
    budget.config['resource'].update(memory_mb=1024, workers=1)
    capacity = refresh(budget)
    assert capacity['parser_workers'] == 1 and capacity['memory_limit_mb'] <= 1024
    assert budget.work_capacity(0)['batch_files'] == 0
    assert budget.work_capacity(0)['reason'] == 'idle'
    budget.config['resource'].update(memory_mb=1)
    with pytest.raises(ResourceLimit, match='memory_budget_exceeded'):
        budget.check()


def test_legacy_load_preserves_explicit_fixed_limits(tmp_path):
    config = defaults(str(tmp_path / 'index'), [])
    for key in ('budget_mode', 'memory_fraction', 'reserve_fraction'):
        del config['resource'][key]
    config['resource'].update(memory_mb=913, workers=1, batch_sleep_ms=77)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    old = load_config(path)
    assert old['resource']['budget_mode'] == 'fixed'
    assert old['resource']['memory_mb'] == 913 and old['resource']['workers'] == 1
    assert old['resource']['batch_sleep_ms'] == 77
    assert apply_preset(old, 'balanced')['resource']['workers'] == 4
    assert json.loads(path.read_text(encoding='utf-8')) == config


@pytest.mark.parametrize('key,value', [('workers', True), ('workers', 9), ('workers', 0),
    ('memory_fraction', 0), ('memory_fraction', .51), ('reserve_fraction', -1),
    ('reserve_fraction', float('nan')), ('budget_mode', 'unlimited')])
def test_invalid_adaptive_config_is_rejected(tmp_path, key, value):
    config = defaults(str(tmp_path / 'index'), [])
    config['resource'][key] = value
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match=key):
        load_config(path)


def real_pool(tmp_path, monkeypatch):
    from data_search import workers
    config = defaults(str(tmp_path / 'index'), [])
    config['semantic']['enabled'] = False
    config['resource'].update(budget_mode='fixed', workers=2, min_available_mb=0, worker_cpu_percent=100)
    # Each response includes the actual child PID, so two concurrent requests
    # cannot pass by serializing through a single pipe/process.
    script = ("import json,os,sys,time\nfor line in sys.stdin:\n"
              " request=json.loads(line); time.sleep(request.get('delay',0)); "
              "print(json.dumps({'ok':True,'result':{'pid':os.getpid(),'id':request['id']}}),flush=True)\n")
    monkeypatch.setattr(workers, 'process_command', lambda *args: [sys.executable, '-u', '-c', script])
    return ParserPool(Budget(config))


def test_real_parallel_parsers_have_distinct_pids_and_cpu_slots(tmp_path, monkeypatch):
    pool = real_pool(tmp_path, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(pool.request, {'id': index, 'delay': .3}) for index in range(2)]
            values = [future.result(timeout=10) for future in futures]
        assert len({value['pid'] for value in values}) == 2
        assert {value['id'] for value in values} == {0, 1}
        controls = pool.control_status['workers']
        assert {control['slot'] for control in controls} == {0, 1}
        assert all(control['role'] == 'parser' for control in controls)
        import psutil
        try:
            eligible = psutil.Process().cpu_affinity()
        except (AttributeError, psutil.Error):
            eligible = []
        if len(eligible) >= 2:
            assert controls[0]['affinity_cpus'] != controls[1]['affinity_cpus']
        processes = [worker.proc for worker in pool._workers.values()]
        pool.idle_close(0)
        assert all(process.poll() is not None for process in processes)
        assert pool.proc is None
    finally:
        pool.cancel()
        pool.close()


def test_cancel_reaps_real_children_and_wakes_waiters(tmp_path, monkeypatch):
    pool = real_pool(tmp_path, monkeypatch)
    executor = ThreadPoolExecutor(max_workers=3)
    futures = [executor.submit(pool.request, {'id': index, 'delay': 30}) for index in range(3)]
    processes = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with pool._condition:
                processes = [worker.proc for worker in pool._workers.values() if worker.proc is not None]
            if len(processes) == 2:
                break
            time.sleep(.02)
        assert len(processes) == 2
        pool.cancel()
        for future in futures:
            with pytest.raises(ResourceLimit, match='service_stopping'):
                future.result(timeout=3)
        pool.close()
        assert all(process.poll() is not None for process in processes)
        assert pool.control_status['active_requests'] == 0
        with pytest.raises(ResourceLimit, match='service_stopping'):
            pool.request({'id': 9})
    finally:
        pool.cancel()
        pool.close()
        executor.shutdown(wait=True)


def test_pool_retires_excess_idle_children_on_shrink(tmp_path, monkeypatch):
    pool = real_pool(tmp_path, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(pool.request, {'id': index, 'delay': .2}) for index in range(2)]
            for future in futures:
                future.result(timeout=10)
        retired = pool._workers[1].proc
        pool.budget.config['resource']['workers'] = 1
        assert pool.request({'id': 3})['id'] == 3
        assert retired.poll() is not None
        assert list(pool._workers) == [0]
    finally:
        pool.cancel()
        pool.close()


def test_shrink_does_not_admit_new_request_until_active_jobs_finish(tmp_path, monkeypatch):
    budget, sample = measured_budget(tmp_path, monkeypatch)
    budget.config['resource'].update(budget_mode='fixed', workers=2)
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    class ControlledWorker:
        def __init__(self, budget, **identity):
            self.proc = None
            self.control_status = identity
        def request(self, request, **kwargs):
            index = request['id']
            entered[index].set()
            assert release[index].wait(5)
            return index
        def close(self):
            pass
        def cancel(self):
            for event in release:
                event.set()
    pool = ParserPool(budget, worker_factory=ControlledWorker)
    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = [executor.submit(pool.request, {'id': index}) for index in range(2)]
        assert all(event.wait(2) for event in entered[:2])
        budget.config['resource']['workers'] = 1
        futures.append(executor.submit(pool.request, {'id': 2}))
        release[0].set()
        assert futures[0].result(timeout=2) == 0
        assert not entered[2].wait(.2), 'One active old slot must consume the reduced capacity'
        release[1].set()
        assert entered[2].wait(2)
        release[2].set()
        assert [future.result(timeout=2) for future in futures] == [0, 1, 2]
    finally:
        pool.cancel()
        pool.close()
        executor.shutdown(wait=True)
