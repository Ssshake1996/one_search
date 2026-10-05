from __future__ import annotations

import os
import shutil
import time
import threading
from pathlib import Path

import psutil

from .scope import link_directory


class ResourceLimit(RuntimeError):
    pass


class Budget:
    def __init__(self, config: dict):
        self.config = config
        self.path = Path(config["data_dir"])
        self.path.mkdir(parents=True, exist_ok=True)
        self._last_disk = 0.0
        self._disk_bytes = 0
        self._disk_writes = 0
        self._last_memory = 0.0
        self._memory = (0, 0, 0)
        self._total_memory = 0
        self._cpu_percent = 0.0
        self._cpu_times = None
        self._cpu_count = max(1, psutil.cpu_count() or 1)
        try:
            self._cpu_count = max(1, len(psutil.Process().cpu_affinity()))
        except (psutil.Error, AttributeError, OSError):
            pass
        self.lock = threading.RLock()

    def note_write(self, byte_estimate: int):
        """Conservative accounting between full disk calibrations."""
        with self.lock:
            self._disk_writes += max(0, byte_estimate)

    def snapshot(self, force_disk: bool = False) -> dict:
        with self.lock:
            return self._snapshot(force_disk)

    def _snapshot(self, force_disk=False):
        moment = time.monotonic()
        if moment - self._last_memory >= .25:
            self._memory = self._measure_memory()
            self._last_memory = moment
        memory, available, free_disk = self._memory
        if force_disk or moment - self._last_disk > 10:
            self._calibrate_disk()
        capacity = self._capacity()
        return {"rss_mb": round(memory / 1048576, 2),
                "available_mb": round(available / 1048576, 2),
                "total_memory_mb": round(self._total_memory / 1048576, 2),
                "system_cpu_percent": self._cpu_percent,
                "configured_budget": {key: self.config['resource'].get(key) for key in
                    ('budget_mode', 'memory_mb', 'memory_fraction', 'reserve_fraction', 'workers')},
                "effective_budget": capacity,
                "disk_mb": round((self._disk_bytes + self._disk_writes) / 1048576, 2),
                "free_disk_mb": round(free_disk / 1048576, 2),
                "disk_accounting": "reserved_writes_with_10s_calibration",
                "rss_enforcement": "sampled_process_tree",
                "worker_limits_requested": {
                    "memory_mb": self.config['resource'].get('worker_memory_mb',512),
                    "cpu_percent": self.config['resource'].get('worker_cpu_percent',25),
                    "note": "Applied limits and fallbacks are reported for each worker"}}

    def _measure_memory(self):
        proc = psutil.Process()
        procs = [proc] + proc.children(recursive=True)
        memory = 0
        for child in procs:
            try:
                memory += child.memory_info().rss
            except psutil.Error:
                pass
        paths = {self.path,Path(self.config.get('index_dir',self.path))}
        free = min(shutil.disk_usage(path).free for path in paths if path.exists())
        system = psutil.virtual_memory()
        self._total_memory = system.total
        self._sample_cpu()
        return memory, system.available, free

    def _sample_cpu(self):
        # psutil.cpu_percent keeps a previous sample per calling thread. Parser
        # executors are disposable, so use process-owned deltas across callers.
        sample = psutil.cpu_times()
        total = sum(sample) - getattr(sample, 'guest', 0) - getattr(sample, 'guest_nice', 0)
        idle = sample.idle + getattr(sample, 'iowait', 0)
        if self._cpu_times is not None:
            elapsed = total - self._cpu_times[0]
            if elapsed > 0:
                self._cpu_percent = round(max(0., min(100., 100 * (1 - (idle - self._cpu_times[1]) / elapsed))), 2)
        self._cpu_times = total, idle

    def work_capacity(self, backlog: int | None = None) -> dict:
        """Admission limits, not allocations. All *_mb values use MiB.

        Consumers retain their own cancellation/pause checks. An existing
        process may finish after capacity shrinks; no new parallel job should
        be admitted above the new limit. Budget.check enforces hard pressure.
        """
        with self.lock:
            moment = time.monotonic()
            if moment - self._last_memory >= .25:
                self._memory = self._measure_memory()
                self._last_memory = moment
            return self._capacity(backlog)

    def _capacity(self, backlog=None):
        limits, semantic = self.config['resource'], self.config['semantic']
        adaptive = limits.get('budget_mode', 'fixed') == 'adaptive'
        rss, available, _ = (value / 1048576 for value in self._memory)
        total = self._total_memory / 1048576
        hard = float(limits['memory_mb'])
        reserve = float(limits['min_available_mb'])
        ceiling = hard
        if adaptive:
            reserve = max(reserve, total * limits.get('reserve_fraction', .125))
            ceiling = min(hard, total * limits.get('memory_fraction', .20))
        limit = min(ceiling, max(0, rss + available - reserve)) if adaptive else hard
        worker_max = max(1, int(limits.get('workers', 1)))
        # Reserve a modest daemon/cache allowance and a resident model before
        # granting parser slots. Per-worker Job memory is committed memory,
        # so this is conservative admission, not a promised RSS allocation.
        base = 256 + (512 if semantic.get('enabled', False) else 0)
        memory_workers = max(1, int(max(0, limit - base) // limits.get('worker_memory_mb', 512)))
        cpu_workers = max(1, int(self._cpu_count * limits.get('worker_cpu_percent', 25) / 100))
        workers = min(worker_max, memory_workers, cpu_workers) if adaptive else min(worker_max, memory_workers)
        reason = ('memory_capacity' if memory_workers < worker_max else 'configured_limit') if not adaptive else (
            'memory_capacity' if memory_workers < worker_max and memory_workers <= cpu_workers else
            'cpu_capacity' if cpu_workers < worker_max else 'configured_limit')
        delay = limits.get('batch_sleep_ms', 0) / 1000
        if adaptive and (available < reserve or rss >= max(1, limit) * .90):
            workers, delay, reason = 1, max(delay, .2), 'system_memory_pressure'
        elif adaptive and self._cpu_percent >= self.config.get('runtime_policy', {}).get('busy_cpu_percent', 85):
            workers, delay, reason = 1, max(delay, .1), 'system_cpu_pressure'
        if backlog is not None:
            backlog = max(0, int(backlog))
            if backlog < workers:
                workers, reason = max(1, backlog), 'backlog' if backlog else 'idle'
        batch_files = min(self.config.get('scheduler', {}).get('files_per_tick', 32), workers * 8)
        if backlog is not None:
            batch_files = min(batch_files, backlog)
        headroom = max(0, limit - rss)
        return {'budget_mode': 'adaptive' if adaptive else 'fixed',
                'memory_limit_mb': round(limit, 2), 'memory_ceiling_mb': round(ceiling, 2),
                'system_reserve_mb': round(reserve, 2), 'parser_workers': workers,
                'batch_files': batch_files,
                'batch_bytes': int(min(256, max(1, headroom / 8)) * 1048576),
                'sqlite_cache_mb': int(max(4, min(256, limit / 16))),
                'embedding_batch_size': min(semantic.get('batch_size', 8),
                    max(1, int(headroom // 32))) if adaptive else semantic.get('batch_size', 8),
                'delay_seconds': delay, 'reason': reason, 'memory_unit': 'MiB'}

    def _calibrate_disk(self):
        size = 0
        candidates = {self.path.resolve(), Path(self.config['semantic']['model_dir']).resolve(),
                      Path(self.config.get('index_dir',self.path)).resolve()}
        paths = [p for p in candidates if not any(p!=other and p.is_relative_to(other) for other in candidates)]
        for directory in paths:
            for base, dirs, files in os.walk(directory, followlinks=False):
                dirs[:] = [d for d in dirs if not link_directory(Path(base, d))]
                for name in files:
                    try:
                        p = Path(base, name)
                        if not p.is_symlink():
                            size += p.stat().st_size
                    except OSError:
                        pass
        self._disk_bytes, self._disk_writes, self._last_disk = size, 0, time.monotonic()

    def check(self, disk: bool = False, reserve_mb: float = 0) -> dict:
        state, limits = self.snapshot(), self.config["resource"]
        if disk and (state['disk_mb'] + reserve_mb >= limits['max_disk_mb'] or
                     state['free_disk_mb'] - reserve_mb < limits['min_free_disk_mb']):
            state = self.snapshot(force_disk=True)
        if state["rss_mb"] > limits["memory_mb"]:
            raise ResourceLimit("memory_budget_exceeded")
        if state["available_mb"] < limits["min_available_mb"]:
            raise ResourceLimit("system_memory_pressure")
        if (limits.get('budget_mode') == 'adaptive' and
                state['rss_mb'] > state['effective_budget']['memory_limit_mb']):
            raise ResourceLimit('system_memory_pressure' if
                state['available_mb'] < state['effective_budget']['system_reserve_mb'] else 'memory_budget_exceeded')
        if disk:
            if state["disk_mb"] + reserve_mb >= limits["max_disk_mb"]:
                raise ResourceLimit("disk_budget_exceeded")
            if state["free_disk_mb"] - reserve_mb < limits["min_free_disk_mb"]:
                raise ResourceLimit("disk_free_space_low")
        return state
