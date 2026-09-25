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
        return {"rss_mb": round(memory / 1048576, 2),
                "available_mb": round(available / 1048576, 2),
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
        return memory, psutil.virtual_memory().available, free

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
        if disk:
            if state["disk_mb"] + reserve_mb >= limits["max_disk_mb"]:
                raise ResourceLimit("disk_budget_exceeded")
            if state["free_disk_mb"] - reserve_mb < limits["min_free_disk_mb"]:
                raise ResourceLimit("disk_free_space_low")
        return state
