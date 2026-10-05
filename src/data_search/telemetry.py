"""Bounded, process-local measurements. Parallel stage times can overlap."""
from collections import deque
from contextlib import contextmanager
import threading
import time


class Telemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.stages = {name: {'seconds': 0., 'calls': 0, 'active': 0}
                       for name in ('discovery', 'parse', 'write', 'embedding', 'vectors', 'wait')}
        self.counters = dict.fromkeys(('files_indexed', 'files_updated', 'chunks_written', 'chunks_reused', 'chunks_removed'), 0)
        self.samples = deque(maxlen=61)
        self.last_batch = None

    @contextmanager
    def measure(self, name):
        start = time.monotonic()
        with self.lock:
            self.stages[name]['active'] += 1
        try:
            yield
        finally:
            with self.lock:
                stage = self.stages[name]
                stage['active'] -= 1
                stage['calls'] += 1
                stage['seconds'] += time.monotonic() - start

    def count(self, **values):
        with self.lock:
            for key, value in values.items():
                self.counters[key] += value

    def batch(self, files, seconds, workers):
        with self.lock:
            self.last_batch = {'files': files, 'seconds': seconds, 'workers': workers}
            moment = int(time.monotonic())
            if self.samples and self.samples[-1][0] == moment:
                previous, count = self.samples.pop()
                self.samples.append((previous, count + files))
            else:
                self.samples.append((moment, files))

    def snapshot(self, queue):
        moment = time.monotonic()
        with self.lock:
            while self.samples and self.samples[0][0] < int(moment) - 59:
                self.samples.popleft()
            window = min(60., max(.001, moment - self.started))
            return {'schema_version': 1, 'uptime_seconds': moment - self.started,
                    'stages': {key: dict(value) for key, value in self.stages.items()},
                    'counters': dict(self.counters), 'last_batch': self.last_batch,
                    'throughput': {'files_per_second': sum(n for _, n in self.samples) / window,
                                   'window_seconds': window}, 'queue': queue}
