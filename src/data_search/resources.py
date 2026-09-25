from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import psutil


class ResourceLimit(RuntimeError):
    pass


class Budget:
    def __init__(self, config: dict):
        self.config = config
        self.path = Path(config["data_dir"])
        self.path.mkdir(parents=True, exist_ok=True)
        self._last_disk = 0.0
        self._disk_bytes = 0

    def snapshot(self, force_disk: bool = False) -> dict:
        proc = psutil.Process()
        procs = [proc] + proc.children(recursive=True)
        memory = 0
        for child in procs:
            try:
                memory += child.memory_info().rss
            except psutil.Error:
                pass
        if force_disk or time.monotonic() - self._last_disk > 10:
            size = 0
            paths = [self.path]
            model = Path(self.config['semantic']['model_dir']).resolve()
            if not model.is_relative_to(self.path.resolve()):
                paths.append(model)
            for directory in paths:
                for base, dirs, files in os.walk(directory, followlinks=False):
                    dirs[:] = [d for d in dirs if not Path(base, d).is_symlink()]
                    for name in files:
                        try:
                            p = Path(base, name)
                            if not p.is_symlink():
                                size += p.stat().st_size
                        except OSError:
                            pass
            self._disk_bytes, self._last_disk = size, time.monotonic()
        return {"rss_mb": round(memory / 1048576, 2),
                "available_mb": round(psutil.virtual_memory().available / 1048576, 2),
                "disk_mb": round(self._disk_bytes / 1048576, 2),
                "free_disk_mb": round(shutil.disk_usage(self.path).free / 1048576, 2)}

    def check(self, disk: bool = False, reserve_mb: float = 0) -> dict:
        state, limits = self.snapshot(force_disk=disk), self.config["resource"]
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
