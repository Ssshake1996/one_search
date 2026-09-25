import sqlite3

import pytest

from data_search.model import MODEL_ID
from data_search.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path))
    yield value
    value.close()


def add(store, key, digest, semantic=1):
    with store.db:
        document = store.db.execute('INSERT INTO documents(key,source_id,path,name,status) VALUES(?,?,?,?,?)',
            (key,'files',key,key,'ready')).lastrowid
        return store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator,semantic) VALUES(?,?,?,?,?)',
            (document,key,digest,'{}',semantic)).lastrowid


def hashes(store, table):
    return {row['hash'] for row in store.rows('SELECT hash FROM '+table)}


def embed(store, digest, model=MODEL_ID):
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (digest,model,b'cache'))


def test_duplicate_hashes_enqueue_once_and_orphan_only_after_last_semantic_reference(store):
    first = add(store,'first','shared')
    second = add(store,'second','shared')
    add(store,'body-only','shared',semantic=0)
    assert hashes(store,'embedding_queue') == {'shared'}
    embed(store,'shared')
    assert not hashes(store,'embedding_queue')
    with store.db:
        store.db.execute('DELETE FROM chunks WHERE id=?',(first,))
    assert not hashes(store,'orphan_embedding_queue')
    with store.db:
        store.db.execute('DELETE FROM chunks WHERE id=?',(second,))
    assert hashes(store,'orphan_embedding_queue') == {'shared'}
    with store.db:
        store.db.execute('UPDATE chunks SET semantic=1 WHERE hash=?',('shared',))
    assert not hashes(store,'orphan_embedding_queue')
    assert not hashes(store,'embedding_queue')


def test_body_only_chunk_does_not_create_pending_and_reenable_does(store):
    chunk = add(store,'off','needed',semantic=0)
    assert not hashes(store,'embedding_queue')
    with store.db:
        store.db.execute('UPDATE chunks SET semantic=1 WHERE id=?',(chunk,))
    assert hashes(store,'embedding_queue') == {'needed'}
    with store.db:
        store.db.execute('UPDATE chunks SET semantic=0 WHERE id=?',(chunk,))
    assert not hashes(store,'embedding_queue')


def test_embedding_before_chunk_reuse_and_bounded_orphan_cleanup(store):
    embed(store,'reuse')
    assert hashes(store,'orphan_embedding_queue') == {'reuse'}
    add(store,'file','reuse')
    assert not hashes(store,'embedding_queue')
    assert not hashes(store,'orphan_embedding_queue')
    embed(store,'unused')
    with store.db:
        store.db.execute('DELETE FROM embeddings WHERE hash=? AND NOT EXISTS(SELECT 1 FROM chunks WHERE hash=? AND semantic=1)',('unused','unused'))
    assert not hashes(store,'orphan_embedding_queue')


def test_embedding_replace_model_update_delete_and_chunk_hash_update(store):
    chunk = add(store,'file','first')
    embed(store,'first','old-model')
    assert hashes(store,'embedding_queue') == {'first'}
    embed(store,'first')
    assert not hashes(store,'embedding_queue')
    with store.db:
        store.db.execute('UPDATE embeddings SET model=? WHERE hash=?',('wrong-model','first'))
    assert hashes(store,'embedding_queue') == {'first'}
    with store.db:
        store.db.execute('UPDATE embeddings SET model=? WHERE hash=?',(MODEL_ID,'first'))
    assert not hashes(store,'embedding_queue')
    with store.db:
        store.db.execute('UPDATE chunks SET hash=? WHERE id=?',('replacement',chunk))
    assert hashes(store,'embedding_queue') == {'replacement'}
    assert hashes(store,'orphan_embedding_queue') == {'first'}
    embed(store,'replacement')
    with store.db:
        store.db.execute('DELETE FROM embeddings WHERE hash=?',('replacement',))
    assert hashes(store,'embedding_queue') == {'replacement'}
    with store.db:
        store.db.execute('DELETE FROM chunks WHERE id=?',(chunk,))
    assert not hashes(store,'embedding_queue')


def test_queue_changes_rollback_with_canonical_rows(store):
    chunk = add(store,'file','hash')
    embed(store,'hash')
    with pytest.raises(RuntimeError):
        with store.db:
            store.db.execute('DELETE FROM chunks WHERE id=?',(chunk,))
            assert hashes(store,'orphan_embedding_queue') == {'hash'}
            raise RuntimeError('abort')
    assert not hashes(store,'orphan_embedding_queue')
    assert not hashes(store,'embedding_queue')
    assert store.rows('SELECT id FROM chunks')[0]['id'] == chunk


def test_legacy_queue_migration_and_restart_never_rescans_complete_embeddings(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    add(store,'missing','missing')
    add(store,'ready','ready')
    add(store,'old-model','old')
    embed(store,'ready')
    embed(store,'old','prior-model')
    embed(store,'orphan')
    names = store.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'embedding_%'")
    with store.db:
        for row in names:
            store.db.execute('DROP TRIGGER '+row['name'])
        store.db.execute('DROP TABLE embedding_queue')
        store.db.execute('DROP TABLE orphan_embedding_queue')
        store.db.execute("DELETE FROM settings WHERE key='embedding_queue_model'")
    store.close()
    store = Store(str(tmp_path))
    assert hashes(store,'embedding_queue') == {'missing','old'}
    assert hashes(store,'orphan_embedding_queue') == {'orphan'}
    embed(store,'missing')
    embed(store,'old')
    store.close()
    calls = []
    original = sqlite3.connect
    def connect(*args,**kwargs):
        connection = original(*args,**kwargs)
        connection.set_trace_callback(calls.append)
        return connection
    monkeypatch.setattr(sqlite3,'connect',connect)
    store = Store(str(tmp_path))
    try:
        assert not hashes(store,'embedding_queue')
        assert not any('INSERT OR IGNORE INTO embedding_queue SELECT c.hash FROM chunks' in sql for sql in calls)
        assert not any('DELETE FROM embedding_queue' in sql for sql in calls)
        assert not any(row['name']=='chunks_hash' for row in store.rows("SELECT name FROM sqlite_master WHERE type='index'"))
        plan = store.rows('EXPLAIN QUERY PLAN SELECT 1 FROM chunks WHERE hash=? AND semantic=1 LIMIT 1',('hash',))
        assert any('COVERING INDEX chunks_hash_semantic' in row['detail'] for row in plan)
        # Repeated no-work discovery executes only a tiny queue lookup.
        operations = 0
        def step():
            nonlocal operations
            operations += 1
            return 0
        store.db.set_progress_handler(step,1)
        for _ in range(100):
            assert not store.db.execute('SELECT 1 FROM embedding_queue LIMIT 1').fetchone()
        store.db.set_progress_handler(None,0)
        assert operations < 1000
    finally:
        store.close()
