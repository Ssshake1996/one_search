import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from usearch.index import Index

from data_search.config import defaults
from data_search.engine import Engine
from data_search.model import MODEL_ID
from data_search.store import Store, pack_vector, unpack_vector
from data_search.vectors import Vectors


def configuration(tmp_path):
    root = tmp_path/'files'
    root.mkdir()
    config = defaults(str(tmp_path/'index'),[str(root)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    return root, config


def test_filename_scope_can_exceed_body_scope_and_semantic_scope(tmp_path):
    root, config = configuration(tmp_path)
    text, body = root/'nameonly.txt', root/'body.md'
    text.write_text('excludedbodytoken')
    body.write_text('includedbodytoken')
    config['indexing']['content_extensions'] = ['.md']
    config['indexing']['semantic_scope'] = 'none'
    engine = Engine(config)
    try:
        engine.scan_once()
        assert engine.search('nameonly', 'files')['results'][0]['status']=='metadata'
        assert not engine.search('excludedbodytoken','keyword')['results']
        assert engine.search('includedbodytoken','keyword')['results']
        assert engine.coverage()['semantic_eligible_chunks']==0
    finally:
        engine.close()


def test_narrowing_and_broadening_content_scope_reconciles_cached_chunks(tmp_path):
    root, config = configuration(tmp_path)
    (root/'record.txt').write_text('cachedprivatetoken')
    engine = Engine(config)
    engine.scan_once()
    old = engine.search('cachedprivatetoken','keyword')['results'][0]
    engine.close()
    config['indexing']['content_scope']='none'
    engine = Engine(config)
    assert not engine.search('cachedprivatetoken','keyword')['results']
    assert engine.fetch(old['document_id'])['chunks']==[]
    engine.close()
    config['indexing']['content_scope']='all'
    engine = Engine(config)
    try:
        engine.scan_once()
        assert engine.search('cachedprivatetoken','keyword')['results']
    finally:
        engine.close()


def test_adaptive_candidates_recover_narrow_extension_and_long_document(tmp_path, monkeypatch):
    root, config = configuration(tmp_path)
    (root/'first.txt').write_text('irrelevant text')
    (root/'target.md').write_text('targetneedle text')
    engine = Engine(config)
    try:
        engine.scan_once()
        rows = engine.store.rows('SELECT c.id,d.extension FROM chunks c JOIN documents d ON c.doc_id=d.id ORDER BY c.id')
        first = next(r['id'] for r in rows if r['extension']=='.txt')
        target = next(r['id'] for r in rows if r['extension']=='.md')
        requested = []
        def near(vector, count):
            requested.append(count)
            # A long leading document occupies the first 150 ANN positions.
            return [(first,0.1)]*min(count,150) + ([(target,0.2)] if count>150 else [])
        monkeypatch.setattr(engine,'_encode',lambda *a,**k:[[1.0]+[0.0]*511])
        monkeypatch.setattr(engine.vectors,'search',near)
        result=engine.search('semantic question','semantic',extension='.md',limit=1)
        assert result['results'][0]['name']=='target.md'
        assert requested==[128,256]
    finally:
        engine.close()


def test_old_ann_cannot_restore_revoked_semantic_scope(tmp_path, monkeypatch):
    root, config = configuration(tmp_path)
    (root/'record.md').write_text('searchneedle')
    engine = Engine(config)
    try:
        engine.scan_once()
        chunk=engine.store.rows('SELECT id FROM chunks')[0]['id']
        with engine.store.db:
            engine.store.db.execute('UPDATE chunks SET semantic=0')
        monkeypatch.setattr(engine,'_encode',lambda *a,**k:[[1.0]+[0.0]*511])
        monkeypatch.setattr(engine.vectors,'search',lambda *a:[(chunk,0.0)])
        assert not engine.search('searchneedle','semantic')['results']
        assert engine.search('searchneedle','hybrid')['results'][0]['match']=='keyword'
    finally:
        engine.close()


def test_filename_mode_never_returns_database_table_paths(tmp_path):
    _, config = configuration(tmp_path)
    engine = Engine(config)
    try:
        with engine.store.db:
            engine.store.db.execute("INSERT INTO documents(key,source_id,path,name,status) VALUES('db:test','db','x','x','ready')")
        assert not engine.search('x','files')['results']
    finally:
        engine.close()


def test_half_vectors_retain_dimensions_and_support_legacy(tmp_path):
    value=np.random.default_rng(42).standard_normal(512).astype(np.float32)
    packed=pack_vector(value)
    assert len(packed)==1024
    assert np.allclose(unpack_vector(packed),value,atol=.001,rtol=.001)
    assert np.array_equal(unpack_vector(value.tobytes()),value)
    with pytest.raises(ValueError):
        pack_vector([float('nan')]*512)


def legacy_store(path):
    store=Store(str(path))
    with store.db:
        doc=store.db.execute("INSERT INTO documents(key,source_id,path,name,status) VALUES('one','files','legacy.txt','legacy.txt','ready')").lastrowid
        store.db.execute("INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,'migrationneedle','hash','{}')",(doc,))
    store.close()
    db=sqlite3.connect(path/'index.sqlite3')
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        db.execute('DROP TRIGGER '+name)
    db.executescript("DROP TABLE chunks_fts; DROP TABLE paths_fts; CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens); CREATE VIRTUAL TABLE paths_fts USING fts5(path,tokenize='trigram'); INSERT INTO chunks_fts(rowid,tokens) VALUES(1,'migrationneedle'); INSERT INTO paths_fts(rowid,path) VALUES(1,'legacy.txt');")
    db.close()


def test_fts_migration_and_recovery_after_rebuild_failure(tmp_path,monkeypatch):
    legacy_store(tmp_path)
    original=sqlite3.connect
    connections=[]
    class Interrupted(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql=="INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')":
                raise sqlite3.OperationalError('simulated crash during migration')
            return super().execute(sql,*args,**kwargs)
    def connect(*args,**kwargs):
        kwargs['factory']=Interrupted
        result=original(*args,**kwargs)
        connections.append(result)
        return result
    monkeypatch.setattr(sqlite3,'connect',connect)
    with pytest.raises(sqlite3.OperationalError):
        Store(str(tmp_path))
    for connection in connections:
        connection.close()
    monkeypatch.setattr(sqlite3,'connect',original)
    store=Store(str(tmp_path))
    try:
        assert store.rows("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'migrationneedle'")
        assert not store.rows("SELECT name FROM sqlite_master WHERE name IN ('chunks_fts_content','paths_fts_content')")
        store.remove([1])
        assert not store.rows("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'migrationneedle'")
    finally:
        store.close()


class Unlimited:
    def check(self,**kwargs):
        return {}


def seed(store,key):
    with store.db:
        doc=store.db.execute('INSERT INTO documents(key,source_id,path,name,status) VALUES(?,?,?,?,?)',(key,'files',key,key,'ready')).lastrowid
        chunk=store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)',(doc,key,key,'{}')).lastrowid
        store.db.execute('INSERT INTO embeddings VALUES(?,?,?)',(key,MODEL_ID,pack_vector([1.0]+[0.0]*511)))
        store.set_setting('vector_generation',str(chunk))
    return chunk


def test_queries_and_sqlite_reads_continue_while_ann_build_paused(tmp_path,monkeypatch):
    store=Store(str(tmp_path))
    cache=Vectors(store,Unlimited())
    first=seed(store,'first')
    cache.sync()
    seed(store,'second')
    entered,release=threading.Event(),threading.Event()
    original=Index.add
    errors=[]
    def blocked(*args,**kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args,**kwargs)
    monkeypatch.setattr(Index,'add',blocked)
    def build():
        try: cache.sync()
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=build)
    thread.start()
    try:
        assert entered.wait(5)
        started=time.monotonic()
        assert store.rows('SELECT count(*) n FROM chunks')[0]['n']==2
        assert cache.search([1.0]+[0.0]*511,10)[0][0]==first
        assert time.monotonic()-started<2
        assert cache.status()['pending']
    finally:
        release.set()
        thread.join(10)
        cache.close()
        store.close()
    assert not errors and not thread.is_alive()


def test_unchanged_sync_maps_without_loading_full_ann_and_cleans_orphan(tmp_path,monkeypatch):
    store=Store(str(tmp_path))
    cache=Vectors(store,Unlimited())
    seed(store,'first')
    cache.sync()
    (tmp_path/'vectors-interrupted.usearch').write_bytes(b'orphan')
    original=Index.restore
    views=[]
    def restore(*args,**kwargs):
        views.append(kwargs.get('view'))
        return original(*args,**kwargs)
    monkeypatch.setattr(Index,'restore',restore)
    try:
        cache.sync()
        assert views==[True]
        assert not (tmp_path/'vectors-interrupted.usearch').exists()
        metadata=json.loads(cache.meta.read_text())
        del metadata['count']
        cache.meta.write_text(json.dumps(metadata))
        assert cache.status()['pending']
        cache.sync()
        assert cache.last_sync['rebuilt']
    finally:
        cache.close()
        store.close()


def test_unicode_directory_build_query_restart_and_compact(tmp_path):
    from data_search.resources import Budget
    directory=tmp_path/'中文目录 with spaces'
    directory.mkdir()
    config=defaults(str(directory),[])
    store=Store(str(directory))
    cache=Vectors(store,Budget(config))
    first=seed(store,'中文资料')
    before=Path.cwd()
    try:
        cache.sync()  # Actual subprocess uses a Unicode cwd and ASCII basename.
        assert Path.cwd()==before
        assert cache.search([1.0]+[0.0]*511,10)[0][0]==first
        cache.close()
        cache=Vectors(store,Budget(config))
        cache.sync()
        assert cache.search([1.0]+[0.0]*511,10)[0][0]==first
        seed(store,'新增资料')
        cache.sync()
        assert len(cache.search([1.0]+[0.0]*511,10))==2
        assert Path.cwd()==before
    finally:
        cache.close()
        store.close()
