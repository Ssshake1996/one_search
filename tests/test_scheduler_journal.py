"""Engine/journal integration on isolated synthetic roots, never real drives."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_search import change_journal, scope
from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import ResourceLimit


class Feed:
    """Durable synthetic volume history; handles themselves are ephemeral."""

    def __init__(self):
        self.journal_id = 'synthetic-1'
        self.next_usn = 100
        self.first_usn = 0
        self.events = []
        self.available = True
        self.calls = []
        self.handles = []

    def append(self, path, action='upsert', directory=False):
        self.events.append({'action': action, 'path': str(path), 'is_directory': directory,
                            'file_id': '1', 'parent_id': '2', 'usn': self.next_usn})
        self.next_usn += 1

    def handle(self):
        feed = self
        class Journal:
            closed = False

            def poll(self, cursor, *, max_events, allowed):
                assert not self.closed
                feed.calls.append(json.loads(json.dumps(cursor)))
                result = {'available': feed.available, 'backend': 'ntfs_usn', 'events': [],
                          'next_cursor': cursor, 'reconciliation_required': True,
                          'reason': 'access_denied', 'has_more': False, 'diagnostics': []}
                if not feed.available:
                    return result
                next_cursor = {'journal_id': feed.journal_id, 'next_usn': feed.next_usn}
                if cursor is None or cursor['journal_id'] != feed.journal_id or cursor['next_usn'] < feed.first_usn:
                    result.update(next_cursor=next_cursor, reason='baseline_required')
                    return result
                selected = [event for event in feed.events if event['usn'] >= cursor['next_usn']]
                consumed = selected[:max_events]
                if len(selected) > max_events:
                    next_cursor['next_usn'] = selected[max_events]['usn']
                events = [event for event in consumed if allowed(Path(event['path']))]
                result.update(events=events, next_cursor=next_cursor,
                              reconciliation_required=any(e['action'] == 'reconcile' for e in events),
                              reason=None, has_more=len(selected) > max_events)
                return result

            def close(self):
                self.closed = True
        handle = Journal()
        self.handles.append(handle)
        return handle


@pytest.fixture
def machine(tmp_path, monkeypatch):
    roots = [str(tmp_path / 'volume-a'), str(tmp_path / 'volume-b')]
    for root in roots:
        Path(root).mkdir()
    feeds = {root: Feed() for root in roots}
    monkeypatch.setattr(scope, 'discover_volumes', lambda: (roots.copy(), []))
    # The platform branch is exercised while all discovered volumes and journal
    # transports are synthetic; no native disk API can be reached.
    monkeypatch.setattr('data_search.engine.platform', SimpleNamespace(system=lambda: 'Windows'))
    monkeypatch.setattr(change_journal, 'VolumeJournal', lambda root: feeds[root].handle())
    config = defaults(str(tmp_path / 'index'))
    config['semantic']['enabled'] = False
    config['resource'].update(batch_sleep_ms=0, min_available_mb=0, min_free_disk_mb=0)
    config['scheduler'].update(metadata_items_per_tick=2, metadata_batch_size=2,
                               directories_per_tick=2, files_per_tick=2,
                               phase_seconds=5, journal_events_per_tick=2)
    engines = []

    def open_engine():
        engine = Engine(config)
        def parse(request, *args, **kwargs):
            return {'status': 'ready', 'chunks': [
                {'text': Path(request['path']).read_text(encoding='utf-8'), 'locator': {}}]}
        engine.parser.request = parse
        engines.append(engine)
        return engine

    result = SimpleNamespace(roots=roots, feeds=feeds, config=config, open=open_engine)
    yield result
    for engine in engines:
        if not engine.stop_event.is_set():
            engine.close()


def tick(engine, full=False):
    status = engine.scan_once(full=full)
    assert status['last_error'] is None, status['last_error']
    return status


def drain(engine, limit=100):
    for _ in range(limit):
        tick(engine)
        if not engine._has_work() and not any(r.get('has_more') for r in engine.journal_reports.values()):
            return
    pytest.fail('synthetic journal/scheduler queues did not drain')


def cursor(engine, root):
    return json.loads(engine.store.setting('journal:' + root, 'null'))


def test_first_anchor_is_durable_before_baseline_walk(machine, monkeypatch):
    root = Path(machine.roots[0])
    (root / 'initial.txt').write_text('baselineanchorword', encoding='utf-8')
    engine = machine.open()
    begin = engine.catalog.begin
    anchors = []
    def checked_begin():
        anchors.append([cursor(engine, value) for value in machine.roots])
        assert all(value == {'journal_id': 'synthetic-1', 'next_usn': 100} for value in anchors[-1])
        assert engine.store.setting('file_reconcile_requested') == 'true'
        return begin()
    monkeypatch.setattr(engine.catalog, 'begin', checked_begin)
    status = tick(engine, full=True)
    monkeypatch.setattr(engine.catalog, 'begin', begin)
    drain(engine)
    assert anchors
    assert engine.search('baselineanchorword', 'keyword')['results']
    assert status['file_scope']['monitoring'] == 'journal_and_reconciliation'
    assert machine.feeds[str(root)].calls[0] is None


def test_rename_delete_queue_and_cursor_replay_after_restart(machine):
    root = Path(machine.roots[0])
    old, gone = root / 'old.txt', root / 'gone.txt'
    old.write_text('renamedcontentword')
    gone.write_text('deletedcontentword')
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    new = root / 'new.txt'
    old.rename(new)
    gone.unlink()
    feed = machine.feeds[str(root)]
    feed.append(old, 'delete')
    feed.append(new, 'upsert')
    feed.append(gone, 'delete')
    engine._poll_journals()  # Crash boundary: durable events, not yet applied.
    assert cursor(engine, str(root))['next_usn'] == 102
    assert {row['path'] for row in engine.store.rows('SELECT path FROM file_events')} == {str(old), str(new)}
    assert engine.journal_reports[str(root)]['has_more']
    engine.close()
    reopened = machine.open()
    tick(reopened)
    assert feed.calls[-1]['next_usn'] == 102
    drain(reopened)
    assert cursor(reopened, str(root))['next_usn'] == 103
    results = reopened.search('renamedcontentword', 'keyword')['results']
    assert len(results) == 1 and results[0]['path'] == str(new)
    assert not reopened.search('deletedcontentword', 'keyword')['results']
    assert not reopened.store.rows('SELECT id FROM documents WHERE path IN (?,?)', (str(old), str(gone)))


def test_permission_denied_uses_full_scan_and_preserves_previous_cursor(machine):
    root = Path(machine.roots[0])
    source = root / 'fallback.txt'
    source.write_text('fallbackinitial')
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    saved = cursor(engine, str(root))
    feed = machine.feeds[str(root)]
    feed.available = False
    source.write_text('fallbackchangedlonger')
    status = tick(engine, full=True)
    drain_one_baseline = 0
    while engine.catalog.active or engine.store.rows('SELECT 1 FROM file_work'):
        tick(engine)
        drain_one_baseline += 1
        assert drain_one_baseline < 20
    assert engine.search('fallbackchangedlonger', 'keyword')['results']
    assert cursor(engine, str(root)) == saved
    assert status['file_scope']['monitoring'] == 'journal_with_periodic_fallback'
    assert status['journal'][str(root)]['reason'] == 'access_denied'
    for value in machine.feeds.values():
        value.available = False
    engine._poll_journals()
    assert engine.monitoring == 'periodic'


def test_removed_volume_closes_handle_and_revokes_cached_results(machine):
    root = machine.roots[1]
    source = Path(root) / 'removed.txt'
    source.write_text('removedvolumeword')
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    assert engine.search('removedvolumeword', 'keyword')['results']
    handle = machine.feeds[root].handles[-1]
    machine.roots.remove(root)
    tick(engine, full=True)
    drain(engine)
    assert handle.closed
    assert root not in engine.journals and root not in engine.journal_reports
    assert not engine.store.rows('SELECT id FROM documents WHERE path=?', (str(source),))
    assert not engine.search('removedvolumeword', 'keyword')['results']


def test_queue_overflow_commits_recovery_flag_with_cursor_and_reconciles_after_restart(machine):
    root = machine.roots[0]
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    # Saturate the queue with names only; no 10,000 real files are created.
    with engine.store.lock, engine.store.db:
        engine.store.db.executemany('INSERT INTO file_events(path) VALUES(?)',
                                    [(str(Path(root) / f'old-event-{i}.txt'),) for i in range(10000)])
    source = Path(root) / 'overflow-new.txt'
    source.write_text('overflowrecoveryword')
    machine.feeds[root].append(source)
    engine._poll_journals()
    assert cursor(engine, root)['next_usn'] == 101
    assert engine.store.setting('file_reconcile_requested') == 'true'
    assert engine.store.rows('SELECT count(*) n FROM file_events')[0]['n'] == 10000
    assert not engine.store.rows('SELECT path FROM file_events WHERE path=?', (str(source),))
    engine.close()
    reopened = machine.open()
    assert reopened.store.setting('file_reconcile_requested') == 'true'
    tick(reopened)
    assert reopened.search('overflowrecoveryword', 'keyword')['results']
    assert cursor(reopened, root)['next_usn'] == 101


def test_change_in_already_visited_directory_survives_baseline_cleanup(machine):
    root = Path(machine.roots[0])
    earlier, later = root / 'earlier', root / 'later'
    earlier.mkdir()
    later.mkdir()
    (earlier / 'original.txt').write_text('firstdirectoryword')
    for i in range(8):
        (later / f'{i}.txt').write_text(f'laterscanword{i}')
    engine = machine.open()
    tick(engine, full=True)
    for _ in range(20):
        completed = engine.store.rows('SELECT 1 FROM documents WHERE path=?', (str(earlier / 'original.txt'),))
        queued = engine.store.rows('SELECT 1 FROM file_scan_dirs WHERE path=?', (str(earlier),))
        if completed and not queued:
            break
        tick(engine)
    else:
        pytest.fail('earlier synthetic directory was not visited')
    assert engine.catalog.active
    source = earlier / 'late-created.txt'
    source.write_text('duringbaselineword')
    machine.feeds[str(root)].append(source)
    engine._poll_journals()
    engine.catalog.process_events()
    assert engine.store.rows('SELECT id FROM documents WHERE path=?', (str(source),))
    drain(engine)
    assert engine.search('duringbaselineword', 'keyword')['results']


def test_reconciliation_requested_midscan_survives_current_generation_and_restart(machine):
    root = Path(machine.roots[0])
    for i in range(7):
        (root / f'{i}.txt').write_text(f'midscanword{i}')
    engine = machine.open()
    tick(engine, full=True)
    assert engine.catalog.active
    generation = engine.store.rows('SELECT generation FROM file_scan_roots WHERE path=?', (str(root),))[0]['generation']
    machine.feeds[str(root)].append(root, 'reconcile', directory=True)
    engine._poll_journals()
    assert engine.store.setting('file_reconcile_requested') == 'true'
    assert not engine.catalog.begin()  # Must not clear the pending second scan.
    engine.close()
    reopened = machine.open()
    assert reopened.store.setting('file_reconcile_requested') == 'true'
    drain(reopened)
    new_generation = reopened.store.rows('SELECT generation FROM file_scan_roots WHERE path=?', (str(root),))[0]['generation']
    assert generation != new_generation
    assert reopened.store.setting('file_reconcile_requested') != 'true'
    assert reopened.store.rows('SELECT count(*) n FROM documents')[0]['n'] == 7


def test_budget_failure_does_not_advance_journal_cursor_or_drop_events(machine, monkeypatch):
    root = machine.roots[0]
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    source = Path(root) / 'budget.txt'
    source.write_text('budgetreplayword')
    machine.feeds[root].append(source)
    original = engine.budget.check
    def exhausted(*args, **kwargs):
        raise ResourceLimit('synthetic_disk_limit')
    monkeypatch.setattr(engine.budget, 'check', exhausted)
    with pytest.raises(ResourceLimit):
        engine._poll_journals()
    assert cursor(engine, root)['next_usn'] == 100
    assert not engine.store.rows('SELECT 1 FROM file_events WHERE path=?', (str(source),))
    monkeypatch.setattr(engine.budget, 'check', original)
    drain(engine)
    assert engine.search('budgetreplayword', 'keyword')['results']
    assert cursor(engine, root)['next_usn'] == 101


def test_unavailable_journal_does_not_restart_completed_baseline_forever(machine):
    root = Path(machine.roots[0])
    for i in range(7):
        (root / f'{i}.txt').write_text(f'fallbackloopword{i}')
    for feed in machine.feeds.values():
        feed.available = False
    engine = machine.open()
    tick(engine, full=True)
    assert engine.catalog.active
    # A full-machine baseline normally spans many ticks. Persistent permission
    # denial must return the scheduler to its periodic idle interval afterward.
    drain(engine, limit=25)
    assert engine.monitoring == 'periodic'
    assert engine.store.rows('SELECT count(*) n FROM documents')[0]['n'] == 7


@pytest.mark.parametrize('cause', ['reset', 'overflow'])
def test_lost_journal_history_reanchors_then_recovers_from_baseline(machine, cause):
    root = Path(machine.roots[0])
    source = root / 'lost-old.txt'
    source.write_text('journalhistoryold')
    engine = machine.open()
    tick(engine, full=True)
    drain(engine)
    source.unlink()
    replacement = root / 'lost-new.txt'
    replacement.write_text('journalhistoryrecovered')
    feed = machine.feeds[str(root)]
    feed.next_usn = 500
    if cause == 'reset':
        feed.journal_id = 'synthetic-recreated'
    else:
        feed.first_usn = 400
    engine._poll_journals()
    assert cursor(engine, str(root)) == {'journal_id': feed.journal_id, 'next_usn': 500}
    assert engine.store.setting('file_reconcile_requested') == 'true'
    assert not engine.store.rows('SELECT 1 FROM file_events')
    engine.close()
    reopened = machine.open()
    drain(reopened)
    assert reopened.search('journalhistoryrecovered', 'keyword')['results']
    assert not reopened.store.rows('SELECT id FROM documents WHERE path=?', (str(source),))
