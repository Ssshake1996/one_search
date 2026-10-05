"""Independent content/embedding work and bounded ANN publication decisions."""
import threading
import time

import pytest

from data_search import engine as module
from data_search.config import defaults
from data_search.engine import Engine
from data_search.resources import ResourceLimit
from data_search.runtime_policy import RuntimePolicy
from data_search.store import text_hash


def configuration(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    config = defaults(str(tmp_path / 'index'), [str(root)])
    config['resource'].update(budget_mode='fixed', workers=2, memory_mb=4096,
                              min_available_mb=0, min_free_disk_mb=0, batch_sleep_ms=0)
    config['runtime_policy'] = {'enabled': False, 'foreground_grace_seconds': 0}
    config['semantic'].update(batch_size=8, vector_publish_chunks=256, vector_publish_seconds=10)
    config['scheduler'].update(embedding_batches_per_tick=1, tick_seconds=.2, phase_seconds=2)
    return config, root


def seed(engine, root, count):
    path = root / 'synthetic.txt'
    path.write_text('synthetic file only', encoding='utf-8')
    with engine.vector_lock, engine.store.transaction():
        doc = engine.store.db.execute('INSERT INTO documents(key,source_id,path,name,status) VALUES(?,?,?,?,?)',
            ('semantic-fixture', 'files', str(path), path.name, 'ready')).lastrowid
        for index in range(count):
            text = 'synthetic chunk ' + str(index)
            engine.store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator,semantic) VALUES(?,?,?,?,1)',
                                     (doc, text, text_hash(text), '{}'))
        engine._changed()
    return doc


def fake_embeddings(monkeypatch, engine):
    monkeypatch.setattr(module, 'model_ready', lambda _: True)
    monkeypatch.setattr(engine, '_encode', lambda texts: [[1., *([0.] * 511)] for _ in texts])


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail('bounded condition did not complete')


def test_embedding_batches_coalesce_until_queue_drains(tmp_path, monkeypatch):
    config, root = configuration(tmp_path)
    engine = Engine(config)
    fake_embeddings(monkeypatch, engine)
    builds = []
    monkeypatch.setattr(engine.vectors, 'sync', lambda **kwargs: builds.append(1))
    try:
        seed(engine, root, 32)
        for _ in range(3):
            assert engine._embed_pending() == 8
            assert engine.vector_thread is None
        assert engine._embed_pending() == 8
        engine.vector_thread.join(timeout=3)
        assert builds == [1]
        assert not engine.store.rows('SELECT 1 FROM embedding_queue LIMIT 1')
        assert engine.telemetry.snapshot({})['stages']['embedding']['calls'] == 4
        assert engine.telemetry.snapshot({})['stages']['vectors']['calls'] == 1
    finally:
        engine.close()


def test_chunk_threshold_and_max_wait_each_publish_with_backlog(tmp_path, monkeypatch):
    config, root = configuration(tmp_path)
    config['semantic']['vector_publish_chunks'] = 16
    engine = Engine(config)
    fake_embeddings(monkeypatch, engine)
    builds = []
    monkeypatch.setattr(engine.vectors, 'sync', lambda **kwargs: builds.append(1))
    try:
        seed(engine, root, 100)
        engine._embed_pending()
        assert builds == []
        engine._embed_pending()
        engine.vector_thread.join(timeout=3)
        assert builds == [1]
        assert engine.store.rows('SELECT 1 FROM embedding_queue LIMIT 1')
        engine._vector_last_launch = 0
        engine._vector_pending_since = time.monotonic() - 11
        assert engine._publish_vectors()
        engine.vector_thread.join(timeout=3)
        assert builds == [1, 1]
    finally:
        engine.close()


def test_deletion_during_encode_does_not_recreate_orphan_vector(tmp_path, monkeypatch):
    config, root = configuration(tmp_path)
    engine = Engine(config)
    monkeypatch.setattr(module, 'model_ready', lambda _: True)
    monkeypatch.setattr(engine, '_publish_vectors', lambda **kwargs: None)
    try:
        doc = seed(engine, root, 1)
        def encode(texts):
            engine.store.remove([doc])
            return [[1., *([0.] * 511)] for _ in texts]
        monkeypatch.setattr(engine, '_encode', encode)
        assert engine._embed_pending() == 0
        assert not engine.store.rows('SELECT 1 FROM embeddings')
        assert not engine.store.rows('SELECT 1 FROM embedding_queue')
    finally:
        engine.close()


def test_restart_flushes_already_encoded_generation_and_deletions(tmp_path, monkeypatch):
    config, root = configuration(tmp_path)
    engine = Engine(config)
    fake_embeddings(monkeypatch, engine)
    try:
        doc = seed(engine, root, 4)
        monkeypatch.setattr(engine, '_publish_vectors', lambda **kwargs: False)
        engine._embed_pending()
        assert not engine.store.rows('SELECT 1 FROM embedding_queue')
        assert not engine.vectors.meta.exists()
    finally:
        engine.close()
    restarted = Engine(config)
    try:
        assert restarted._publish_vectors()
        restarted.vector_thread.join(timeout=15)
        assert not restarted.vector_thread.is_alive()
        assert restarted.vector_error is None
        assert restarted.vectors.status()['published_chunks'] == 4
        restarted.store.remove([doc])
        restarted._changed()
        assert restarted._publish_vectors(force=True)
        restarted.vector_thread.join(timeout=15)
        assert restarted.vector_error is None
        assert restarted.vectors.status()['published_chunks'] == 0
        assert not restarted.vectors.status()['pending']
    finally:
        restarted.close()


def test_slow_embedding_does_not_block_new_file_and_pause_tracks_both_lanes(tmp_path, monkeypatch):
    config, root = configuration(tmp_path)
    (root / 'first.txt').write_text('firstsyntheticneedle', encoding='utf-8')
    engine = Engine(config)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(module, 'model_ready', lambda _: True)
    monkeypatch.setattr(engine.vectors, 'sync', lambda **kwargs: None)
    def encode(texts):
        entered.set()
        while not release.wait(.02):
            if engine._background_cancelled():
                raise ResourceLimit('indexing_paused_or_stopping')
        return [[1., *([0.] * 511)] for _ in texts]
    monkeypatch.setattr(engine, '_encode', encode)
    try:
        engine.start_background()
        assert entered.wait(8)
        assert engine.semantic_active
        (root / 'second.txt').write_text('secondsyntheticneedle', encoding='utf-8')
        engine.scan_event.set()
        wait_until(lambda: engine.store.rows("SELECT 1 FROM documents WHERE name='second.txt' AND status='ready'"))
        assert not release.is_set()
        assert engine.search('secondsyntheticneedle', 'keyword')['results']
        paused = engine.dispatch('pause', {})
        assert paused['background_activity']['embedding'] is True
        wait_until(lambda: not engine.semantic_active)
        assert engine.semantic_error is None
        assert engine._pause_status(True)['pause_state'] == 'paused'
        wait_until(lambda: engine.telemetry.snapshot({})['stages']['wait']['seconds'] > .15)
    finally:
        release.set()
        engine.close()
        assert not engine.semantic_thread.is_alive()


def test_battery_admission_does_not_starve_semantic_lane(tmp_path):
    config, _ = configuration(tmp_path)
    config['runtime_policy'] = {'enabled': True}
    tick = [100.]
    policy = RuntimePolicy(config, monotonic=lambda: tick[0])
    state = {'cpu_percent': 0, 'on_battery': True, 'battery_percent': 80}
    assert policy.decision(state)['background_allowed']
    assert policy.decision(state, lane='semantic')['background_allowed']
    assert policy.decision(state)['reason'] == 'battery_saving'
    assert policy.decision(state, lane='semantic')['reason'] == 'battery_saving'
    tick[0] += 1
    assert policy.decision(state)['background_allowed']
    assert policy.decision(state, lane='semantic')['background_allowed']
