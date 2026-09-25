from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

from .resources import Budget, ResourceLimit
from .runtime import process_command


class Worker:
    """One serial disposable process. The parent monitors total process-tree RSS."""
    def __init__(self, budget: Budget):
        self.budget = budget
        self.proc = None
        self.lock = threading.RLock()
        self.last_used = 0.0
        self.responses = queue.Queue()
        self.cancelled = threading.Event()
        self.control = None
        self.control_status = {}

    def _start(self):
        if self.control:
            self.control.close()
            self.control = None
        self.responses = queue.Queue()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        child_env = os.environ.copy()
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            child_env[name] = str(self.budget.config['semantic']['threads'])
        self.proc = subprocess.Popen(process_command('data_search.worker'),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     creationflags=flags, env=child_env)
        from .resource_control import attach_worker
        self.control = attach_worker(self.proc.pid, self.budget.config)
        self.control_status = self.control.status
        q, proc = self.responses, self.proc
        def read():
            try:
                for line in proc.stdout:
                    try:
                        q.put(json.loads(line))
                    except ValueError:
                        q.put({"ok": False, "error": "invalid worker output"})
            except (OSError, ValueError):
                pass
            finally:
                q.put({"ok": False, "error": "worker exited"})
        threading.Thread(target=read, daemon=True).start()

    def request(self, request: dict, timeout: float = 60, cancelled=None):
        with self.lock:
            if self.cancelled.is_set():
                raise ResourceLimit('service_stopping')
            self.budget.check()
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self.proc.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                self.proc.stdin.flush()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if cancelled is not None and cancelled():
                        raise ResourceLimit('indexing_paused_or_stopping')
                    if self.cancelled.is_set():
                        raise ResourceLimit('service_stopping')
                    self.budget.check()
                    try:
                        result = self.responses.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if not result["ok"]:
                        if result['error'] == 'worker exited':
                            raise RuntimeError(f'worker exited (exit_code={self.proc.poll()}); check worker_memory_mb and resource controls')
                        raise RuntimeError(result["error"])
                    self.last_used = time.monotonic()
                    return result["result"]
                raise ResourceLimit("worker_timeout")
            except Exception:
                self.close()
                raise

    def cancel(self):
        self.cancelled.set()

    def close(self):
        with self.lock:
            if self.proc:
                proc, self.proc = self.proc, None
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                for stream in (proc.stdin, proc.stdout):
                    if stream:
                        stream.close()
            if self.control:
                self.control.close()
                self.control = None

    def idle_close(self, seconds: float):
        if self.lock.acquire(blocking=False):
            try:
                if self.proc and time.monotonic() - self.last_used > seconds:
                    self.close()
            finally:
                self.lock.release()
