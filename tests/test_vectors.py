import json

import numpy as np
import pytest
from usearch.index import Index

from data_search.model import MODEL_ID
from data_search.store import Store, text_hash
from data_search.vectors import Vectors


class Budget:
    def check(self, **kwargs):
        return {}


@pytest.fixture
def cache(tmp_path):
    store = Store(str(tmp_path))
    vectors = Vectors(store, Budget())
    yield store, vectors
    vectors.close()
    store.close()


def add_document(store, key, component):
    vector = np.zeros(512, dtype=np.float32)
    vector[component] = 1
    text = f"synthetic document {key}"
    digest = text_hash(text)
    with store.lock, store.db:
        doc = store.db.execute(
            'INSERT INTO documents(key,source_id,path,name,status) VALUES(?,?,?,?,?)',
            (key, 'files', key, key, 'ready')).lastrowid
        chunk = store.db.execute(
            'INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)',
            (doc, text, digest, '{}')).lastrowid
        store.db.execute('INSERT INTO embeddings VALUES(?,?,?)', (digest, MODEL_ID, vector.tobytes()))
        store.set_setting('vector_generation', str(int(store.setting('vector_generation', '0')) + 1))
    return doc, chunk, vector


def test_add_delete_and_restart_change_only_affected_ids(cache, monkeypatch):
    store, vectors = cache
    first_doc, first_id, first_vector = add_document(store, 'one', 0)
    _, second_id, second_vector = add_document(store, 'two', 1)
    calls = []
    original = Index.add

    def record_add(self, keys, values, **kwargs):
        calls.extend(map(int, keys))
        return original(self, keys, values, **kwargs)

    monkeypatch.setattr(Index, 'add', record_add)
    vectors.sync()
    assert set(calls) == {first_id, second_id}
    assert vectors.last_sync['rebuilt'] is True
    assert vectors.search(first_vector, 2)[0][0] == first_id
    calls.clear()
    _, third_id, third_vector = add_document(store, 'three', 2)
    store.remove([first_doc])
    store.set_setting('vector_generation', str(int(store.setting('vector_generation')) + 1))
    vectors.sync()
    assert calls == [third_id]
    assert vectors.last_sync == {'rebuilt': False, 'added': 1, 'removed': 1, 'count': 2}
    assert vectors.search(third_vector, 2)[0][0] == third_id
    assert first_id not in {key for key, _ in vectors.search(first_vector, 10)}
    vectors.close()
    restarted = Vectors(store, Budget())
    restarted.sync()
    assert restarted.last_sync == {'rebuilt': False, 'added': 0, 'removed': 0, 'count': 2}
    assert restarted.search(second_vector, 2)[0][0] == second_id
    restarted.close()


def test_generation_mismatch_keeps_last_published_snapshot_available(cache):
    store, vectors = cache
    _, _, vector = add_document(store, 'one', 0)
    vectors.sync()
    store.set_setting('vector_generation', '99')
    assert vectors.search(vector, 10)
    assert vectors.status()['pending'] is True
    vectors.sync()
    assert vectors.last_sync['added'] == 0
    assert vectors.last_sync['rebuilt'] is False
    assert vectors.search(vector, 10)


def test_cancelled_build_does_not_publish_partial_index_and_can_resume(cache, monkeypatch):
    from data_search.resources import ResourceLimit
    store, vectors = cache
    for i in range(270):
        _, _, vector = add_document(store, f'cancel-{i}', i % 512)
    calls = []
    original = Index.add
    def add_then_pause(self, keys, values, **kwargs):
        calls.append(len(keys))
        return original(self, keys, values, **kwargs)
    monkeypatch.setattr(Index, 'add', add_then_pause)
    with pytest.raises(ResourceLimit, match='paused_or_stopping'):
        vectors.sync(cancelled=lambda: bool(calls))
    assert calls == [256]
    assert not vectors.meta.exists()
    assert store.rows('SELECT count(*) n FROM chunks')[0]['n'] == 270
    vectors.sync()
    assert vectors.last_sync['count'] == 270
    assert vectors.search(vector, 10)


@pytest.mark.parametrize('damage', ['metadata', 'snapshot', 'dirty', 'model'])
def test_corrupt_or_uncommitted_cache_rebuilds_from_sqlite(cache, damage):
    store, vectors = cache
    _, expected_id, vector = add_document(store, 'one', 0)
    vectors.sync()
    vectors.close()
    if damage == 'metadata':
        vectors.meta.write_text('broken-json', encoding='utf-8')
    elif damage == 'snapshot':
        vectors.path.write_bytes(b'not an ANN file')
    elif damage == 'dirty':
        vectors.dirty.write_text('process interrupted', encoding='ascii')
    else:
        metadata = json.loads(vectors.meta.read_text())
        metadata['model'] = 'different embedding space'
        vectors.meta.write_text(json.dumps(metadata), encoding='utf-8')
    with pytest.raises(RuntimeError, match='semantic_index_pending'):
        vectors.search(vector, 10)
    vectors.sync()
    assert vectors.last_sync['rebuilt'] is True
    assert not vectors.dirty.exists()
    assert vectors.search(vector, 10)[0][0] == expected_id


def test_interrupted_metadata_commit_is_detected_and_recovered(cache, monkeypatch):
    import data_search.vectors as module

    store, vectors = cache
    _, _, vector = add_document(store, 'one', 0)
    vectors.sync()
    _, new_id, new_vector = add_document(store, 'two', 1)
    original = module.atomic_json

    def fail_commit(*args, **kwargs):
        raise OSError('simulated interruption after snapshot replacement')

    monkeypatch.setattr(module, 'atomic_json', fail_commit)
    with pytest.raises(OSError, match='simulated interruption'):
        vectors.sync()
    assert not vectors.dirty.exists()
    assert vectors.search(vector, 10)
    assert new_id not in {key for key, _ in vectors.search(new_vector, 10)}
    monkeypatch.setattr(module, 'atomic_json', original)
    vectors.sync()
    assert vectors.last_sync['rebuilt'] is False
    assert vectors.search(new_vector, 10)[0][0] == new_id
    assert not vectors.dirty.exists()
    assert not vectors.path.with_suffix('.tmp').exists()


def test_empty_index_and_removing_last_document(cache):
    store, vectors = cache
    vectors.sync()
    assert vectors.search(np.ones(512, dtype=np.float32), 10) == []
    doc, _, vector = add_document(store, 'one', 0)
    vectors.sync()
    store.remove([doc])
    store.set_setting('vector_generation', '2')
    vectors.sync()
    assert vectors.last_sync['removed'] == 1
    assert vectors.search(vector, 10) == []


def test_invalid_embedding_does_not_publish_a_snapshot(cache):
    store, vectors = cache
    add_document(store, 'one', 0)
    with store.db:
        store.db.execute('UPDATE embeddings SET vector=?', (np.ones(128, dtype=np.float32).tobytes(),))
    with pytest.raises(ValueError, match='512-dimensional'):
        vectors.sync()
    assert not vectors.path.exists()
