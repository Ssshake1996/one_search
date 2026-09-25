"""Small, persistent scheduling decisions; never changes the search scope.

The policy gates background ticks. It does not kill workers or interrupt a read
request, and Budget remains the final memory/disk enforcement layer.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

import psutil


PRESETS = {
    "low": {
        "resource": {"memory_mb": 768, "min_available_mb": 768, "worker_memory_mb": 512,
                     "worker_cpu_percent": 15, "batch_sleep_ms": 100},
        "scheduler": {"metadata_items_per_tick": 1000, "directories_per_tick": 32,
                      "files_per_tick": 8, "embedding_batches_per_tick": 1, "phase_seconds": .5,
                      "tick_seconds": 2},
        "semantic": {"threads": 1, "batch_size": 4, "idle_seconds": 60},
    },
    "balanced": {
        "resource": {"memory_mb": 1024, "min_available_mb": 768, "worker_memory_mb": 512,
                     "worker_cpu_percent": 25, "batch_sleep_ms": 50},
        "scheduler": {"metadata_items_per_tick": 2000, "directories_per_tick": 64,
                      "files_per_tick": 32, "embedding_batches_per_tick": 4, "phase_seconds": 2,
                      "tick_seconds": 1},
        "semantic": {"threads": 1, "batch_size": 8, "idle_seconds": 120},
    },
    "fast": {
        "resource": {"memory_mb": 2048, "min_available_mb": 1024, "worker_memory_mb": 768,
                     "worker_cpu_percent": 50, "batch_sleep_ms": 10},
        "scheduler": {"metadata_items_per_tick": 4000, "directories_per_tick": 128,
                      "files_per_tick": 64, "embedding_batches_per_tick": 8, "phase_seconds": 3,
                      "tick_seconds": .5},
        "semantic": {"threads": 2, "batch_size": 16, "idle_seconds": 180},
    },
}

POLICY_DEFAULTS = {"preset": "balanced", "enabled": True, "busy_cpu_percent": 85,
                   "busy_seconds": 3, "resume_cpu_percent": 65, "battery_percent": 20,
                   "battery_tick_multiplier": 4, "foreground_grace_seconds": 2,
                   "idle_only": False, "on_ac_only": False, "idle_seconds": 120}


def apply_preset(config: dict, name: str) -> dict:
    if name not in PRESETS:
        raise ValueError("Resource preset must be low, balanced or fast")
    result = deepcopy(config)
    for group, values in PRESETS[name].items():
        result.setdefault(group, {}).update(values)
    result.setdefault("runtime_policy", {}).update(preset=name)
    return result


def validate_policy(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("runtime_policy must be an object")
    policy = {**POLICY_DEFAULTS, **value}
    if set(policy) - set(POLICY_DEFAULTS):
        raise ValueError("Unknown runtime policy setting")
    if policy["preset"] not in PRESETS or any(not isinstance(policy[k], bool) for k in ('enabled', 'idle_only', 'on_ac_only')):
        raise ValueError("Invalid runtime policy preset or enabled flag")
    ranges = {"busy_cpu_percent": (1, 100), "resume_cpu_percent": (0, 100),
              "busy_seconds": (0, 300), "battery_percent": (0, 100),
              "battery_tick_multiplier": (1, 20), "foreground_grace_seconds": (0, 30),
              "idle_seconds": (15, 86400)}
    for key, (low, high) in ranges.items():
        v = policy[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"runtime_policy.{key} must be between {low} and {high}")
    if policy["resume_cpu_percent"] >= policy["busy_cpu_percent"]:
        raise ValueError("resume_cpu_percent must be lower than busy_cpu_percent")
    return policy


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as output:
            os.chmod(temp, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class RuntimePolicy:
    def __init__(self, config: dict, *, clock=time.time, monotonic=time.monotonic, sample=None):
        self.settings = validate_policy(config.get("runtime_policy", {}))
        self._tick_seconds = config.get("scheduler", {}).get("tick_seconds", 1)
        self.path = Path(config["data_dir"]) / "runtime-policy.json"
        self._clock, self._monotonic = clock, monotonic
        self._sample_provider = sample or self._sample
        self._lock = threading.RLock()
        self._paused, self._until = False, None
        self._foreground_until, self._busy_since = 0.0, None
        self._foreground_since = None
        self._busy = False
        self._sample_at, self._sample_value = -float("inf"), {}
        self._last_tick, self._last_reason = -float("inf"), None
        self._state_warning = None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("schema_version") != 1 or not isinstance(value.get("paused"), bool):
                raise ValueError()
            until = value.get("until")
            if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until)):
                raise ValueError()
            self._paused, self._until = value["paused"], until
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self._state_warning = "pause_state_unreadable"

    @staticmethod
    def _sample():
        cpu = psutil.cpu_percent(interval=None)
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, OSError, NotImplementedError):
            battery = None
        idle_seconds = None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class LastInput(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
            last = LastInput()
            last.cbSize = ctypes.sizeof(last)
            try:
                if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last)):
                    idle_seconds = ((ctypes.windll.kernel32.GetTickCount() - last.dwTime) & 0xffffffff) / 1000
            except (AttributeError, OSError):
                pass
        return {"cpu_percent": cpu, "on_battery": bool(battery and not battery.power_plugged),
                "battery_percent": battery.percent if battery else None,
                "idle_seconds": idle_seconds, 'idle_detection_available': idle_seconds is not None}

    def _persist(self):
        _write_json(self.path, {"schema_version": 1, "paused": self._paused, "until": self._until})
        self._state_warning = None

    def pause(self, seconds: float | None = None):
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 7 * 86400):
            raise ValueError("Pause duration must be between 1 and 604800 seconds, or omitted")
        with self._lock:
            previous = self._paused, self._until
            self._paused, self._until = True, self._clock() + seconds if seconds is not None else None
            try:
                self._persist()
            except OSError:
                self._paused, self._until = previous
                raise
            return self.status()

    def resume(self):
        with self._lock:
            previous = self._paused, self._until
            self._paused, self._until = False, None
            if self._last_reason == "user_pause":
                self._last_reason = None
            try:
                self._persist()
            except OSError:
                self._paused, self._until = previous
                raise
            return self.status()

    def foreground(self, seconds: float | None = None):
        duration = self.settings["foreground_grace_seconds"] if seconds is None else seconds
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 30:
            raise ValueError("Foreground grace must be between 0 and 30 seconds")
        with self._lock:
            moment = self._monotonic()
            if self._foreground_until <= moment:
                self._foreground_since = moment
            self._foreground_until = max(self._foreground_until, moment + duration)

    def _expire_pause(self):
        if self._paused and self._until is not None and self._clock() >= self._until:
            self._paused, self._until = False, None
            if self._last_reason == "user_pause":
                self._last_reason = None
            try:
                self._persist()
            except OSError:
                # Expiry is already absolute; a restart will reach the same decision.
                self._state_warning = "pause_expiry_not_saved"

    def status(self):
        with self._lock:
            self._expire_pause()
            return {"preset": self.settings["preset"], "user_paused": self._paused,
                    "pause_until": self._until, "automatic_wait": bool(not self._paused and self._last_reason and self._last_reason != "user_pause"),
                    "reason": "user_pause" if self._paused else self._last_reason,
                    "state_warning": self._state_warning, "sample": dict(self._sample_value)}

    def decision(self, snapshot: dict | None = None) -> dict:
        """Call once when deciding whether to start a background tick.

        Battery throttling reserves its allowed tick here. Status reads do not
        consume a tick; foreground queries remain allowed during every wait.
        """
        with self._lock:
            self._expire_pause()
            moment, reason = self._monotonic(), None
            if snapshot is not None:
                self._sample_value, self._sample_at = dict(snapshot), moment
            elif moment - self._sample_at >= 1:
                try:
                    self._sample_value = self._sample_provider()
                    self._state_warning = None if self._state_warning == "resource_sample_unavailable" else self._state_warning
                except (OSError, psutil.Error):
                    self._sample_value = {}
                    self._state_warning = "resource_sample_unavailable"
                self._sample_at = moment
            state = self._sample_value
            cpu = state.get("cpu_percent", 0)
            if self._paused:
                reason = "user_pause"
            elif self._foreground_until > moment and self._foreground_since is not None and moment - self._foreground_since < 3:
                reason = "foreground_query"
            elif self.settings["enabled"]:
                if cpu >= self.settings["busy_cpu_percent"]:
                    if self._busy_since is None:
                        self._busy_since = moment
                    if moment - self._busy_since >= self.settings["busy_seconds"]:
                        self._busy = True
                elif cpu <= self.settings["resume_cpu_percent"]:
                    self._busy, self._busy_since = False, None
                elif not self._busy:
                    self._busy_since = None
                if self._busy:
                    reason = "system_busy"
                elif self.settings['on_ac_only'] and state.get('on_battery'):
                    reason = 'waiting_for_ac_power'
                elif self.settings['idle_only'] and state.get('idle_seconds') is not None and state['idle_seconds'] < self.settings['idle_seconds']:
                    reason = 'waiting_for_idle'
                elif state.get("on_battery"):
                    level = state.get("battery_percent")
                    if level is not None and level <= self.settings["battery_percent"]:
                        reason = "battery_low"
                    elif moment - self._last_tick < self._tick_seconds * self.settings["battery_tick_multiplier"]:
                        reason = "battery_saving"
                if self.settings['idle_only'] and state.get('idle_seconds') is None:
                    self._state_warning = 'idle_detection_unavailable_policy_not_enforced'
                elif self._state_warning == 'idle_detection_unavailable_policy_not_enforced':
                    self._state_warning = None
            self._last_reason = reason
            if reason is None:
                self._last_tick = moment
                if self._foreground_until > moment:
                    # Continuous result polling cannot starve the catalog forever.
                    self._foreground_since = moment
            return {**self.status(), "background_allowed": reason is None,
                    "reason": reason, "query_allowed": True,
                    "next_check_seconds": 1 if reason else 0}
