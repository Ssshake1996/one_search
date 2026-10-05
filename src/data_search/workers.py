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
    def __init__(self, budget: Budget, *, role='worker', slot=0):
        self.budget = budget
        self.role, self.slot = role, slot
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
            child_env[name] = str(self.budget.config['semantic']['threads'] if self.role in ('worker', 'model') else 1)
        self.proc = subprocess.Popen(process_command('data_search.worker'),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     creationflags=flags, env=child_env)
        from .resource_control import attach_worker
        self.control = attach_worker(self.proc.pid, self.budget.config, role=self.role, slot=self.slot)
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
            if cancelled is not None and cancelled():
                raise ResourceLimit('indexing_paused_or_stopping')
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
                    if cancelled is not None and cancelled():
                        raise ResourceLimit('indexing_paused_or_stopping')
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
                if self.proc and time.monotonic() - self.last_used >= seconds:
                    self.close()
            finally:
                self.lock.release()


class ParserPool:
    """Lazily leased parser children; each pipe still has exactly one owner.

    Callers provide their own bounded executor and commit results on the owner
    thread. Waiting callers hold no process or response buffers. A shrinking
    budget retires excess idle children without interrupting admitted work.
    """
    def __init__(self, budget: Budget, *, worker_factory=None):
        self.budget = budget
        self._factory = worker_factory or Worker
        self._workers = {}
        self._leased = set()
        self._condition = threading.Condition(threading.RLock())
        self.cancelled = threading.Event()

    def capacity(self, backlog=None):
        return self.budget.work_capacity(backlog)['parser_workers']

    @property
    def proc(self):
        # Compatibility for process-presence diagnostics; control_status lists
        # every child so callers need not mistake this for the entire pool.
        with self._condition:
            return next((worker.proc for worker in self._workers.values() if worker.proc is not None), None)

    @property
    def control_status(self):
        with self._condition:
            return {'role': 'parser_pool', 'capacity': self.capacity(), 'active_requests': len(self._leased),
                    'workers': [dict(worker.control_status) for _, worker in sorted(self._workers.items())
                                if worker.control_status]}

    def _retire(self, capacity):
        for slot in list(self._workers):
            if slot >= capacity and slot not in self._leased:
                self._workers.pop(slot).close()

    def request(self, request: dict, timeout: float = 60, cancelled=None):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self.cancelled.is_set():
                    raise ResourceLimit('service_stopping')
                if cancelled is not None and cancelled():
                    raise ResourceLimit('indexing_paused_or_stopping')
                self.budget.check()
                capacity = self.capacity()
                self._retire(capacity)
                # Include requests admitted before pressure reduced capacity.
                slot = next((index for index in range(capacity) if index not in self._leased), None)
                if len(self._leased) < capacity and slot is not None:
                    if slot not in self._workers:
                        self._workers[slot] = self._factory(self.budget, role='parser', slot=slot)
                    worker = self._workers[slot]
                    self._leased.add(slot)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResourceLimit('worker_timeout')
                self._condition.wait(min(.1, remaining))
        try:
            return worker.request(request, timeout=max(.01, deadline - time.monotonic()), cancelled=cancelled)
        finally:
            with self._condition:
                self._leased.discard(slot)
                self._retire(self.capacity())
                self._condition.notify_all()

    def cancel(self):
        with self._condition:
            self.cancelled.set()
            for worker in self._workers.values():
                worker.cancel()
            self._condition.notify_all()

    def close(self):
        # Do not hold the pool condition while waiting for worker request locks:
        # a completing request needs that condition to release its lease.
        with self._condition:
            workers = list(self._workers.values())
        for worker in workers:
            worker.close()

    def idle_close(self, seconds: float):
        with self._condition:
            self._retire(self.capacity())
            workers = [worker for slot, worker in self._workers.items() if slot not in self._leased]
        for worker in workers:
            worker.idle_close(seconds)
