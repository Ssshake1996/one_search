"""Exercise the actual event handler and scheduler without an OS watcher."""
import time
from types import SimpleNamespace

import pytest

from data_search import engine as module
from data_search.config import defaults
from data_search.engine import Engine


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail('bounded watcher condition did not complete')


def watcher_engine(tmp_path, monkeypatch):
    import watchdog.observers
    root = tmp_path / 'files'
    root.mkdir(exist_ok=True)
    config = defaults(str(tmp_path / 'index'), [str(root)])
    config['semantic']['enabled'] = False
    config['runtime_policy'] = {'enabled': False, 'foreground_grace_seconds': 0}
    engine = Engine(config)
    class Observer:
        def schedule(self, handler, *args, **kwargs):
            self.handler = handler
        def start(self): pass
        def stop(self): pass
        def join(self, **kwargs): pass
    monkeypatch.setattr(watchdog.observers, 'Observer', Observer)
    monkeypatch.setattr(module.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(engine, '_has_work', lambda: False)
    scans, batches = [], []
    monkeypatch.setattr(engine, 'scan_once', lambda **kwargs: scans.append(kwargs))
    monkeypatch.setattr(engine.catalog, 'enqueue_events', lambda events: batches.append(events))
    engine.start_background()
    return engine, root, scans, batches


def event(path, *, directory=False, kind='modified'):
    return SimpleNamespace(event_type=kind, src_path=str(path), is_directory=directory)


def test_burst_is_one_batch_and_preserves_directory_repair(tmp_path, monkeypatch):
    engine, root, scans, batches = watcher_engine(tmp_path, monkeypatch)
    try:
        wait_until(lambda: scans)
        assert scans[0] == {'full': True, 'semantic': False}
        handler = engine.observer.handler
        for _ in range(100):
            handler.on_any_event(event(root / 'saved.txt'))
        # A directory replaced with a file still requires subtree cleanup.
        handler.on_any_event(event(root / 'replaced', directory=True, kind='deleted'))
        handler.on_any_event(event(root / 'replaced', kind='created'))
        handler.on_any_event(event(root, directory=True))
        assert batches == []
        wait_until(lambda: batches)
        assert len(batches) == 1 and len(batches[0]) == 2
        assert {row['path']: row['is_directory'] for row in batches[0]} == {
            str(root / 'saved.txt'): False, str(root / 'replaced'): True}
    finally:
        engine.close()


def test_overflow_requests_full_reconciliation_and_bounds_pending_map(tmp_path, monkeypatch):
    engine, root, scans, batches = watcher_engine(tmp_path, monkeypatch)
    try:
        wait_until(lambda: scans)
        engine.dispatch('pause', {})
        engine.scan_event.clear()
        monkeypatch.setattr(engine, 'allowed', lambda path: True)
        handler = engine.observer.handler
        # Freeze this engine module's clock so the scheduler cannot age/flush
        # entries during the burst; no test assumption about CPU speed is needed.
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 100.,
                                                          time=time.time, sleep=time.sleep))
        for index in range(10001):
            handler.on_any_event(event(root / str(index)))
        assert engine.scan_event.is_set()
        assert batches == []
        engine.dispatch('resume', {})
        wait_until(lambda: len(scans) >= 2)
        assert scans[-1]['full'] is True
    finally:
        engine.close()


def test_restart_reconciles_even_when_watcher_event_was_not_flushed(tmp_path, monkeypatch):
    engine, root, scans, batches = watcher_engine(tmp_path, monkeypatch)
    try:
        wait_until(lambda: scans)
        engine.observer.handler.on_any_event(event(root / 'unflushed.txt'))
        assert batches == []
    finally:
        engine.close()
    restarted, _, restarted_scans, restarted_batches = watcher_engine(tmp_path, monkeypatch)
    try:
        wait_until(lambda: restarted_scans)
        assert restarted_scans[0]['full'] is True
        assert restarted_batches == []
    finally:
        restarted.close()
