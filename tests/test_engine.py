import time
from pathlib import Path

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import ResourceLimit


@pytest.fixture
def local(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    config = defaults(str(tmp_path / 'index'), [str(root)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    engine = Engine(config)
    yield engine, root, config
    engine.close()


def test_update_delete_and_stale_evidence(local):
    engine, root, _ = local
    source = root / 'receipt.txt'
    source.write_text('alphaunique receipt', encoding='utf-8')
    engine.scan_once()
    hit = engine.search('alphaunique', 'keyword')['results'][0]
    assert hit['stale'] is False
    source.write_text('betaunique changed receipt', encoding='utf-8')
    assert engine.fetch(hit['id'])['document']['stale'] is True
    engine.scan_once()
    assert not engine.search('alphaunique', 'keyword')['results']
    assert engine.search('betaunique', 'keyword')['results']
    source.unlink()
    assert not engine.search('betaunique', 'keyword')['results']
    engine.scan_once()
    assert engine.coverage()['chunks'] == 0


def test_fetch_chunk_starts_at_matching_fragment(local):
    engine, root, _ = local
    (root / 'long.txt').write_text('opening ' * 90 + '\n' + 'targetneedle ' * 15, encoding='utf-8')
    engine.scan_once()
    result = engine.search('targetneedle', 'keyword')['results'][0]
    fetched = engine.fetch(result['id'], limit=1)
    assert 'targetneedle' in fetched['chunks'][0]['text']
    assert fetched['chunks'][0]['chunk_id'] == int(result['id'].split(':')[1])


def test_metadata_only_and_file_size_budget(local):
    engine, root, conf = local
    (root / 'record.bin').write_bytes(b'unknown binary')
    (root / 'oversized.txt').write_text('x' * 5000)
    conf['extraction']['max_file_mb'] = .001
    engine.scan_once()
    assert engine.search('record.bin', 'files')['results'][0]['status'] == 'unsupported'
    assert engine.search('oversized.txt', 'files')['results'][0]['status'] == 'budget'
    assert engine.coverage()['chunks'] == 0


def test_budget_interruption_is_visible_and_resumable(local):
    engine, root, conf = local
    (root / 'resume.txt').write_text('recoverable token')
    conf['resource']['memory_mb'] = 1
    status = engine.scan_once()
    assert status['last_error'] == 'memory_budget_exceeded'
    conf['resource']['memory_mb'] = 1024
    assert engine.scan_once()['last_error'] is None
    assert engine.search('recoverable', 'keyword')['results']


def test_pause_and_reconciliation_after_restart(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    source = root / 'offline.txt'
    source.write_text('originaltoken')
    conf = defaults(str(tmp_path / 'index'), [str(root)])
    conf['semantic']['enabled'] = False
    with_engine = Engine(conf)
    with_engine.scan_once()
    with_engine.dispatch('pause', {})
    with_engine.close()
    source.write_text('offlinechangetoken')
    engine = Engine(conf)
    try:
        assert engine.paused
        assert engine.scan_once()['reason'] == 'paused'
        engine.dispatch('resume', {})
        engine.scan_once()
        assert engine.search('offlinechangetoken', 'keyword')['results']
        assert not engine.search('originaltoken', 'keyword')['results']
    finally:
        engine.close()


def test_scope_change_purges_cached_text(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'secret.txt').write_text('privatecachedtext')
    conf = defaults(str(tmp_path / 'index'), [str(root)])
    conf['semantic']['enabled'] = False
    engine = Engine(conf)
    engine.scan_once()
    engine.close()
    conf['roots'] = []
    engine = Engine(conf)
    try:
        assert not engine.search('privatecachedtext', 'keyword')['results']
        assert engine.store.rows('SELECT * FROM chunks') == []
    finally:
        engine.close()


def test_watcher_ignores_own_index_and_exclusions(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'initial.txt').write_text('initialtoken')
    ignored = root / '.git'
    ignored.mkdir()
    (ignored / 'hidden.txt').write_text('hiddenneedle')
    conf = defaults(str(root / 'data'), [str(root)])
    conf['semantic']['enabled'] = False
    conf['resource']['batch_sleep_ms'] = 0
    conf['scan_interval_seconds'] = 1
    engine = Engine(conf)
    try:
        engine.start_background()
        deadline = time.monotonic() + 10
        while not engine.last_scan and time.monotonic() < deadline:
            time.sleep(.05)
        assert engine.last_scan
        assert not engine.search('hiddenneedle', 'keyword')['results']
        (root / 'delta.txt').write_text('watcherneedle')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if engine.search('watcherneedle', 'keyword')['results']:
                break
            time.sleep(.05)
        else:
            pytest.fail('watcher did not index new file')
        time.sleep(.2)
        with engine.dirty_lock:
            assert all(engine.allowed(Path(p)) for p in engine.dirty)
        assert not engine.scan_event.is_set()
    finally:
        engine.close()


def test_semantic_missing_is_explicit_and_keyword_survives(local):
    engine, root, conf = local
    (root / 'fallback.txt').write_text('fallbackneedle')
    conf['semantic']['enabled'] = True
    engine.scan_once()
    result = engine.search('fallbackneedle', 'hybrid')
    assert result['results'] and result['warnings']
    assert result['coverage']['embedded_chunks'] == 0


def test_remote_node_is_reserved(local):
    engine, _, _ = local
    with pytest.raises(ValueError, match='remote_node'):
        engine.dispatch('search', {'query': 'x', 'node_id': 'server-two'})


def test_stopping_cancels_worker_requests(local):
    engine, _, _ = local
    engine.begin_shutdown()
    with pytest.raises(ResourceLimit, match='service_stopping'):
        engine.parser.request({'method': 'extract', 'path': 'irrelevant'})


def test_entire_filename_catalog_exists_before_first_parser(local, monkeypatch):
    engine, root, _ = local
    for i in range(3):
        (root / f'catalog-{i}.txt').write_text('catalog content')
    request = engine.parser.request
    checks = []
    def checked_request(*args, **kwargs):
        checks.append(len(engine.store.rows('SELECT id FROM documents')))
        return request(*args, **kwargs)
    monkeypatch.setattr(engine.parser, 'request', checked_request)
    engine.scan_once()
    assert checks == [3,3,3]


def test_deleted_document_id_cannot_refer_to_new_file(local):
    engine, root, _ = local
    first = root / 'first.txt'
    first.write_text('original document')
    engine.scan_once()
    old_id = engine.search('first.txt', 'files')['results'][0]['id']
    first.unlink()
    engine.scan_once()
    (root / 'second.txt').write_text('new document')
    engine.scan_once()
    with pytest.raises(ValueError, match='unavailable'):
        engine.fetch(old_id)
