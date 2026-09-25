import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from usearch.index import Index

from data_search.model import MODEL_ID
from data_search.store import Store, pack_vector, text_hash
from data_search.vectors import Vectors


class SegmentBudget:
    def __init__(self, size=64, maximum=2, ratio=.3):
        self.config = {'semantic': {'segment_size':size, 'max_segments_per_sync':maximum,
                                    'compact_deleted_ratio':ratio}}
    def check(self, **kwargs):
        return {}


@pytest.fixture
def segmented(tmp_path):
    store = Store(str(tmp_path))
    vectors = Vectors(store, SegmentBudget())
    yield store, vectors
    vectors.close()
    store.close()


def put(store, number, vector=None, source='files', extension='.txt'):
    if vector is None:
        vector = np.zeros(512, dtype=np.float32)
        vector[number % 512] = 1
    digest = text_hash(f'chunk-{number}')
    with store.lock, store.db:
        doc = store.db.execute('INSERT INTO documents(key,source_id,path,name,extension,status) VALUES(?,?,?,?,?,?)',
            (f'doc-{number}', source, f'file-{number}', f'file-{number}', extension, 'ready')).lastrowid
        chunk = store.db.execute('INSERT INTO chunks(doc_id,text,hash,locator) VALUES(?,?,?,?)',
            (doc, f'chunk-{number}', digest, '{}')).lastrowid
        store.db.execute('INSERT INTO embeddings VALUES(?,?,?)', (digest, MODEL_ID, pack_vector(vector)))
        bump(store)
    return doc, chunk, vector


def bump(store):
    store.set_setting('vector_generation', str(int(store.setting('vector_generation', '0')) + 1))


def sync(vectors):
    vectors.sync(isolated=False)


def finish(vectors, maximum=30):
    for _ in range(maximum):
        sync(vectors)
        if not vectors.status()['pending']:
            return
    pytest.fail('segmented index remained pending')


def metadata(vectors):
    return json.loads(vectors.meta.read_text(encoding='utf-8'))


def test_build_caps_segment_work_and_resumes_without_global_restore(segmented, monkeypatch):
    store, vectors = segmented
    for number in range(270):
        put(store, number)
    sizes = []
    original_add, original_restore = Index.add, Index.restore
    def bounded_add(self, keys, values, **kwargs):
        assert len(self) + len(keys) <= 64
        sizes.append(len(keys))
        return original_add(self, keys, values, **kwargs)
    def view_only(*args, **kwargs):
        assert kwargs.get('view') is True, 'A build must never mutable-load the full previous ANN'
        return original_restore(*args, **kwargs)
    monkeypatch.setattr(Index, 'add', bounded_add)
    monkeypatch.setattr(Index, 'restore', view_only)
    sync(vectors)
    first = metadata(vectors)
    assert [s['count'] for s in first['segments']] == [64, 64]
    assert first['count'] == 128 and vectors.status()['pending']
    first_names = {s['snapshot'] for s in first['segments']}
    vectors.close()
    restarted = Vectors(store, SegmentBudget())
    try:
        sync(restarted)
        assert first_names.issubset({s['snapshot'] for s in metadata(restarted)['segments']})
        assert metadata(restarted)['count'] == 256
        sync(restarted)
        assert metadata(restarted)['count'] == 270
        assert not restarted.status()['pending']
        assert sizes == [64,64,64,64,14]
    finally:
        restarted.close()


def test_small_delta_reuses_unchanged_segment_bytes(segmented, monkeypatch):
    store, vectors = segmented
    for number in range(125):
        put(store, number)
    finish(vectors)
    old = {s['snapshot']: (store.path.parent/s['snapshot']).read_bytes() for s in metadata(vectors)['segments']}
    _, new_id, query = put(store, 300)
    additions=[]
    original=Index.add
    def record(self, keys, values, **kwargs):
        additions.extend(map(int,keys))
        return original(self,keys,values,**kwargs)
    monkeypatch.setattr(Index,'add',record)
    sync(vectors)
    assert additions == [new_id]
    current = {s['snapshot'] for s in metadata(vectors)['segments']}
    assert set(old).issubset(current)
    assert all((store.path.parent/name).read_bytes() == raw for name,raw in old.items())
    assert vectors.search(query,5)[0][0] == new_id


def test_delete_churn_compacts_only_affected_segment_and_keeps_old_reader(segmented):
    store, vectors = segmented
    records=[put(store,i) for i in range(128)]
    finish(vectors)
    before=metadata(vectors)
    first, second=before['segments']
    old_reader=vectors._get_reader(first)
    store.remove([record[0] for record in records[:40]])
    bump(store)
    sync(vectors)
    after=metadata(vectors)
    names={s['snapshot'] for s in after['segments']}
    assert second['snapshot'] in names and first['snapshot'] not in names
    assert first['snapshot'] in after['previous_segments']
    assert (store.path.parent/first['snapshot']).exists()
    assert old_reader.search(records[0][2],count=1,threads=1).keys[0] == records[0][1]
    assert records[0][1] not in {key for key,_ in vectors.search(records[0][2],100)}
    assert after['count'] == 88
    vectors.close()
    restarted=Vectors(store,SegmentBudget())
    try:
        assert len(restarted.search(records[100][2],100)) == 88
        sync(restarted)
        assert restarted.last_sync['added'] == 0
    finally:
        restarted.close()


def test_tombstones_and_tier_revocation_filter_before_compaction(segmented):
    store,vectors=segmented
    records=[put(store,i) for i in range(64)]
    finish(vectors)
    before=metadata(vectors)['segments'][0]['snapshot']
    store.remove([records[0][0]])
    with store.db:
        store.db.execute('UPDATE chunks SET semantic=0 WHERE id=?',(records[1][1],))
    bump(store)
    # Old generation remains published, but current eligibility is authoritative.
    hits={key for key,_ in vectors.search(records[0][2],64)}
    assert records[0][1] not in hits and records[1][1] not in hits and len(hits)==62
    sync(vectors)
    assert metadata(vectors)['segments'][0]['snapshot']==before
    assert metadata(vectors)['count']==62
    with store.db:
        store.db.execute('UPDATE chunks SET semantic=1 WHERE id=?',(records[1][1],))
    bump(store)
    sync(vectors)
    assert records[1][1] in {key for key,_ in vectors.search(records[1][2],64)}


def test_failed_manifest_publication_leaves_old_segments_queryable(segmented,monkeypatch):
    import data_search.vectors as module
    store,vectors=segmented
    _,old_id,query=put(store,0)
    sync(vectors)
    old_bytes=vectors.meta.read_bytes()
    for number in range(1,130):
        put(store,number)
    original=module.atomic_json
    def fail(*args,**kwargs):
        raise OSError('publication interrupted')
    monkeypatch.setattr(module,'atomic_json',fail)
    with pytest.raises(OSError,match='publication interrupted'):
        sync(vectors)
    assert vectors.meta.read_bytes()==old_bytes
    assert vectors.search(query,128)==[(old_id,0.0)]
    monkeypatch.setattr(module,'atomic_json',original)
    finish(vectors)
    assert metadata(vectors)['count']==130
    active={s['snapshot'] for s in metadata(vectors)['segments']}
    with vectors._catalog_connection() as catalog:
        assert catalog.execute('SELECT count(*) FROM members WHERE segment IN ('+','.join('?' for _ in active)+')',list(active)).fetchone()[0]==130


def test_failure_after_manifest_commit_retains_committed_files(segmented,monkeypatch):
    import data_search.vectors as module
    store,vectors=segmented
    put(store,0)
    sync(vectors)
    _,expected,query=put(store,1)
    original=module.atomic_json
    def commit_then_error(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('after replace')
    monkeypatch.setattr(module,'atomic_json',commit_then_error)
    with pytest.raises(OSError,match='after replace'):
        sync(vectors)
    assert vectors.search(query,2)[0][0]==expected
    monkeypatch.setattr(module,'atomic_json',original)
    sync(vectors)
    assert not vectors.status()['pending']


def test_v3_legacy_migration_preserves_fallback_until_complete(segmented):
    store,vectors=segmented
    records=[put(store,i) for i in range(140)]
    legacy=Index(ndim=512,metric='cos',dtype='f16',connectivity=16)
    legacy.add(np.asarray([r[1] for r in records],dtype=np.uint64),np.vstack([r[2] for r in records]),threads=1)
    path=store.path.with_name('vectors-legacy.usearch')
    path.write_bytes(legacy.save())
    stat=path.stat()
    old={'schema_version':3,'generation':store.setting('vector_generation'),'model':MODEL_ID,'count':140,
         'snapshot':path.name,'snapshot_bytes':stat.st_size,'snapshot_mtime_ns':stat.st_mtime_ns}
    vectors.meta.write_text(json.dumps(old),encoding='utf-8')
    assert vectors.search(records[-1][2],200)[0][0]==records[-1][1]
    sync(vectors)
    assert metadata(vectors)['schema_version']==4
    assert metadata(vectors)['legacy']['snapshot']==path.name
    assert metadata(vectors)['count']==128 and vectors.status()['pending']
    assert vectors.search(records[-1][2],200)[0][0]==records[-1][1]
    sync(vectors)
    assert metadata(vectors)['legacy'] is None
    assert metadata(vectors)['count']==140 and not vectors.status()['pending']
    assert vectors.search(records[-1][2],200)[0][0]==records[-1][1]


def test_unicode_path_segment_build_and_restart_without_cwd_change(tmp_path,monkeypatch):
    import data_search.vectors as module
    directory=tmp_path/'中文 用户'/'分段索引'
    store=Store(str(directory))
    vectors=Vectors(store,SegmentBudget(size=2,maximum=1))
    def forbidden(*args):
        pytest.fail('Daemon/library code must not change process cwd')
    monkeypatch.setattr(module.os,'chdir',forbidden)
    try:
        records=[put(store,i) for i in range(5)]
        finish(vectors)
        vectors.close()
        vectors=Vectors(store,SegmentBudget(size=2,maximum=1))
        assert vectors.search(records[-1][2],5)[0][0]==records[-1][1]
        assert metadata(vectors)['count']==5
    finally:
        vectors.close()
        store.close()


def test_global_merge_ranks_segments_by_distance_and_bounds_output(segmented):
    store,vectors=segmented
    vectors.budget.config['semantic'].update(segment_size=2,max_segments_per_sync=2)
    query=np.zeros(512,dtype=np.float32);query[0]=1
    records=[]
    for number,angle in enumerate([1.2,.9,.6,.3,.01,.15]):
        value=np.zeros(512,dtype=np.float32);value[0]=np.cos(angle);value[1]=np.sin(angle)
        records.append(put(store,number,value))
    finish(vectors)
    assert [key for key,_ in vectors.search(query,3)]==[records[i][1] for i in (4,5,3)]
    assert len(vectors.search(query,100))==6


def test_source_and_extension_filters_score_only_current_eligible_rows(segmented,monkeypatch):
    store,vectors=segmented
    query=np.zeros(512,dtype=np.float32);query[0]=1
    for number in range(130):
        put(store,number,query,source='large-source',extension='.txt')
    rare=np.zeros(512,dtype=np.float32);rare[1]=1
    _,rare_id,_=put(store,999,rare,source='rare-source',extension='.md')
    finish(vectors)
    def no_unfiltered_ann(*args,**kwargs):
        pytest.fail('A source-filtered query must not depend on global ANN candidates')
    monkeypatch.setattr(Index,'search',no_unfiltered_ann)
    assert vectors.search(query,10,source_id='rare-source')==[(rare_id,1.0)]
    assert vectors.search(query,10,extension='.MD')==[(rare_id,1.0)]
    assert vectors.search(query,10,source_id='rare-source',extension='.txt')==[]


def test_small_delta_segments_are_compacted_without_exceeding_capacity(segmented):
    store,vectors=segmented
    vectors.budget.config['semantic'].update(segment_size=8,max_segments_per_sync=2)
    for number in range(14):
        put(store,number)
        sync(vectors)
    finish(vectors)
    segments=metadata(vectors)['segments']
    assert len(segments)<=8
    assert all(segment['count']<=8 for segment in segments)
    assert metadata(vectors)['count']==14


def test_changed_hash_for_existing_chunk_does_not_use_old_segment_vector(segmented):
    store,vectors=segmented
    _,chunk,old_query=put(store,0)
    sync(vectors)
    new_vector=np.zeros(512,dtype=np.float32);new_vector[1]=1
    digest=text_hash('replacement')
    with store.db:
        store.db.execute('UPDATE chunks SET hash=?,text=? WHERE id=?',(digest,'replacement',chunk))
        store.db.execute('INSERT INTO embeddings VALUES(?,?,?)',(digest,MODEL_ID,pack_vector(new_vector)))
    bump(store)
    assert vectors.search(old_query,10)==[]
    sync(vectors)
    assert vectors.search(new_vector,1)==[(chunk,0.0)]
    assert metadata(vectors)['count']==1


def test_query_retries_if_publication_and_gc_overtake_snapshot_setup(segmented, monkeypatch):
    store, vectors = segmented
    records = [put(store, number) for number in range(128)]
    finish(vectors)
    old = metadata(vectors)['segments'][0]['snapshot']
    original = vectors._read_connection
    raced = False
    def open_connection(*, catalog=False):
        nonlocal raced
        if catalog and not raced:
            raced = True
            store.remove([record[0] for record in records[:40]])
            bump(store)
            sync(vectors)
            put(store, 999)
            sync(vectors)
            sync(vectors)  # GC may now retire the reader's initially seen segment.
        return original(catalog=catalog)
    monkeypatch.setattr(vectors, '_read_connection', open_connection)
    hits = vectors.search(records[-1][2], 200, source_id='files')
    assert len(hits) == 89 and raced
    assert not {key for key,_ in hits}.intersection(record[1] for record in records[:40])
    assert old not in {segment['snapshot'] for segment in metadata(vectors)['segments']}
