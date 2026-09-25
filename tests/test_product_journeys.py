import json
import os
from pathlib import Path

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.product import context, diagnose, prioritize, refresh


@pytest.fixture
def local(tmp_path):
    root = tmp_path/'files'
    root.mkdir()
    config = defaults(str(tmp_path/'data'),[str(root)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    engine = Engine(config)
    yield engine,root,config
    engine.close()


def test_filters_and_exact_filename_are_actually_applied(local):
    engine,root,_ = local
    sub = root/'one_%'
    sub.mkdir()
    (root/'other').mkdir()
    for p in [sub/'report.txt',sub/'report.md',root/'other'/'report.txt',sub/'copy-report.txt']:
        p.write_text('needle content',encoding='utf8')
        os.utime(p,(1735689600,1735689600))  # 2025-01-01 UTC
    engine.scan_once()
    args = dict(directory=str(sub),extensions=['.txt'],modified_after='2025-01-01',modified_before='2026-01-01',min_size=2,max_size=100,category='document')
    response = engine.search('report.txt',mode='files',**args)
    assert [r['name'] for r in response['results']]==['report.txt','copy-report.txt']
    assert response['applied_filters']['directory']==str(sub)
    assert len(engine.search('needle',mode='keyword',**args)['results'])==2
    assert not engine.search('needle',mode='keyword',modified_after='2026-01-01')['results']
    with pytest.raises(ValueError):
        engine.search('needle',extensions='.txt')
    with pytest.raises(ValueError):
        engine.search('needle',min_size=10,max_size=2)


def test_miss_diagnosis_targeted_refresh_and_context(local):
    engine,root,conf = local
    p = root/'memo.txt'
    p.write_text('first paragraph\n'+'middle '*110+'\nlastneedle',encoding='utf8')
    assert diagnose(engine,str(p))['code']=='not_discovered'
    assert prioritize(engine,str(p))['accepted']
    engine.scan_once(full=False)
    hit = engine.search('lastneedle','keyword')['results'][0]
    surrounding = context(engine,hit['id'],before=2,after=1)
    assert surrounding['citation']['path']==str(p)
    assert len(surrounding['chunks'])>1
    p.write_text('newneedle after update',encoding='utf8')
    assert diagnose(engine,str(p))['code']=='stale'
    engine.dispatch('pause',{})
    assert refresh(engine,str(p))['diagnosis']['code']=='ready'
    assert engine.paused
    assert not engine.search('lastneedle','keyword')['results']
    assert engine.search('newneedle','keyword')['results'][0]['stale'] is False
    assert diagnose(engine,str(p),query='absent') ['query_assessment']=='no_keyword_match_in_indexed_content'
    with pytest.raises(ValueError):
        prioritize(engine,str(root.parent))


def test_replaced_file_cannot_inherit_old_reference(local):
    engine,root,_ = local
    p = root/'same.txt'
    p.write_text('oldneedle')
    engine.scan_once()
    old = engine.search('oldneedle','keyword')['results'][0]
    replacement = root/'replacement.txt'
    replacement.write_text('newneedle')
    os.replace(replacement,p)
    assert not engine.search('oldneedle','keyword')['results']
    with pytest.raises(ValueError):
        engine.fetch(old['document_id'])
    assert diagnose(engine,str(p))['code']=='replaced_file'
    refresh(engine,str(p))
    new = engine.search('newneedle','keyword')['results'][0]
    assert old['document_id']!=new['document_id']
    with pytest.raises(ValueError):
        engine.fetch(old['document_id'])


def test_legacy_identity_cannot_adopt_same_metadata_replacement(local):
    engine,root,_ = local
    p = root/'legacy.txt'
    p.write_text('oldneedle')
    engine.scan_once()
    old = engine.search('oldneedle','keyword')['results'][0]
    stat = p.stat()
    with engine.store.lock,engine.store.db:
        engine.store.db.execute("UPDATE documents SET file_identity=NULL WHERE source_id='files'")
    replacement = root/'new.txt'
    replacement.write_text('newneedle')
    os.utime(replacement,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    os.replace(replacement,p)
    assert not engine.search('oldneedle','keyword')['results']
    assert diagnose(engine,str(p))['code']=='legacy_identity_unverified'
    engine.scan_once()
    assert engine.search('newneedle','keyword')['results'][0]['document_id']!=old['document_id']
    with pytest.raises(ValueError):
        engine.fetch(old['document_id'])


def test_specific_coverage_partial_excluded_and_unsupported(local):
    engine,root,conf = local
    p = root/'unsupported.bin'
    p.write_bytes(b'abc')
    hidden = root/'.git'
    hidden.mkdir()
    (hidden/'x.txt').write_text('secret')
    engine.scan_once()
    assert diagnose(engine,str(p))['code']=='unsupported_content'
    assert diagnose(engine,str(hidden/'x.txt'))['code']=='excluded_or_outside_scope'
    assert diagnose(engine,str(root/'missing.txt'))['code']=='missing_or_moved'
    assert diagnose(engine,str(root))['coverage_complete'] is False


def test_sensitive_template_preserves_filename_and_revokes_content(local):
    engine,root,conf = local
    p = root/'.env'
    p.write_text('syntheticsecret')
    engine.scan_once()
    hit = engine.search('syntheticsecret','keyword')['results'][0]
    conf['indexing']['sensitive_content_excluded'] = True
    engine._apply_indexing_scope()
    assert not engine.search('syntheticsecret','keyword')['results']
    assert engine.search('.env','files')['results']
    with pytest.raises(ValueError):
        engine.fetch(hit['id'])
    assert diagnose(engine,str(p))['code']=='content_excluded'


def test_directory_priority_and_duplicate_evidence(local):
    engine,root,_ = local
    sub = root/'project'
    sub.mkdir()
    (sub/'a.txt').write_text('same evidence')
    (sub/'b.txt').write_text('same evidence')
    assert prioritize(engine,str(sub))['accepted']
    for _ in range(4):
        engine.scan_once(full=False)
    result = engine.search('evidence','keyword',fold_duplicates=True)['results']
    assert len(result)==1
    assert len(result[0]['other_locations'])==1
    assert result[0]['duplicate_basis']=='complete_extracted_text_in_returned_candidates'


def test_case_sensitive_directory_filter_uses_binary_boundaries(tmp_path,monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from data_search import search_filters
    directory = tmp_path/'area'
    prefix = str(directory)+os.sep
    values = [prefix+'one.txt',str(tmp_path/'Area')+os.sep+'two.txt',str(directory)+'extra'+os.sep+'three.txt']
    monkeypatch.setattr(search_filters,'os',SimpleNamespace(name='posix',sep=os.sep))
    clause,args = search_filters.path_predicate(directory)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE documents(source_id,path)')
        db.executemany("INSERT INTO documents VALUES('files',?)",[(v,) for v in values])
        assert db.execute('SELECT path FROM documents d WHERE 1=1'+clause,args).fetchall()==[(values[0],)]
