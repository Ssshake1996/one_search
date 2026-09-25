import json
import sqlite3

import numpy as np
import pytest

from data_search.chunking import split_chunks
from data_search.config import defaults
from data_search.engine import Engine
from data_search.model import MODEL_ID
from data_search.resources import ResourceLimit
from data_search.store import Store, pack_vector


def pieces(text, locator=None, **bounds):
    return list(split_chunks([{'text':text, 'locator':locator or {'line_start':1}}], **bounds))


def assert_exact_sources(inputs, result):
    for piece in result:
        for span in piece['locator']['source_spans']:
            expected = inputs[span['source_chunk']]['text'][span['char_start']:span['char_end']]
            assert piece['text'][span['piece_start']:span['piece_end']] == expected


def test_heading_stays_with_its_paragraph_and_starts_new_chunk():
    text = '# 第一章\n第一段资料。\n\n# 第二章\n目标证据在这里。\n后续说明。'
    result = pieces(text)
    assert len(result) == 2
    assert result[0]['text'].endswith('\n\n')
    assert result[1]['text'].startswith('# 第二章\n目标证据')
    assert result[1]['locator']['heading'] == '第二章'
    assert result[1]['locator']['line_start'] == 4
    assert result[1]['locator']['line_end'] == 6
    assert_exact_sources([{'text':text}], result)


def test_sentence_boundary_preserves_complete_evidence():
    text = '这是完整背景句。' * 23 + '退款将在三个工作日内发起，银行到账时间另计。' + '这是后续完整句。' * 30
    result = pieces(text, maximum=200)
    assert all(len(piece['text']) <= 200 for piece in result)
    assert any('退款将在三个工作日内发起，银行到账时间另计。' in piece['text'] for piece in result)
    assert all(piece['text'].endswith('。') for piece in result)
    assert ''.join(piece['text'] for piece in result) == text


@pytest.mark.parametrize('text', ['unbrokenword'*220, '长文本'*1500, 'one two three four '*300])
def test_long_unstructured_input_stays_bounded_and_loses_no_characters(text):
    result = pieces(text, max_chars=1300)
    covered = set()
    for piece in result:
        loc = piece['locator']
        assert len(piece['text']) <= 350
        assert piece['text'] == text[loc['char_start']:loc['char_end']]
        covered.update(range(loc['char_start'], loc['char_end']))
    assert covered == set(range(1300))
    assert len(result) < 8


def test_grouping_same_locator_has_reversible_source_spans_and_respects_other_pages():
    inputs = [
        {'text':'页面标题', 'locator':{'page':1}},
        {'text':'同一页面的证据内容。', 'locator':{'page':1}},
        {'text':'不同页不能合在一起。', 'locator':{'page':2}},
        {'text':'表格的第一条记录。', 'locator':{'record':1}},
        {'text':'表格的第二条记录。', 'locator':{'record':2}},
    ]
    result = list(split_chunks(inputs))
    assert len(result) == 4
    assert result[0]['text'] == '页面标题\n同一页面的证据内容。'
    assert result[0]['locator']['offset_basis'] == 'grouped_extraction'
    assert [s['source_chunk'] for s in result[0]['locator']['source_spans']] == [0,1]
    assert result[1]['locator']['page'] == 2
    assert_exact_sources(inputs, result)


def test_line_ranges_account_for_prefix_and_trailing_newline():
    text = ('word '*35 + '\n')*12
    result = pieces(text, {'line_start':25, 'line_end':36})
    for piece in result:
        loc = piece['locator']
        first = 25 + text[:loc['char_start']].count('\n')
        assert loc['line_start'] == first
        assert loc['line_end'] == first + piece['text'].count('\n') - int(piece['text'].endswith('\n'))


def test_character_budget_does_not_consume_unbounded_input_iterator():
    def inputs():
        yield {'text':'x'*1000, 'locator':{}}
        raise AssertionError('input iterator exceeded the character budget')
    # The first oversized block is truncated before downstream splitting.
    assert sum(len(row['text']) for row in split_chunks(inputs(), max_chars=100, overlap=0)) == 100


def test_short_chinese_path_postings_migrate_update_delete_and_exclude_databases(tmp_path):
    store = Store(str(tmp_path))
    with store.db:
        first = store.db.execute("INSERT INTO documents(key,source_id,path,name,status) VALUES('a','files','财务/预算.txt','预算.txt','ready')").lastrowid
        store.db.execute("INSERT INTO documents(key,source_id,path,name,status) VALUES('b','db','财务','财务','ready')")
    # Simulate a pre-v0.3 cache without the new index, preserving canonical rows.
    store.db.executescript('DROP TRIGGER paths_short_insert; DROP TRIGGER paths_short_delete; DROP TRIGGER paths_short_update; DROP TABLE paths_short_fts; DROP VIEW short_file_paths;')
    store.close()
    class FullDisk:
        def check(self, **kwargs):
            raise ResourceLimit('test_disk_limit')
    with pytest.raises(ResourceLimit, match='test_disk_limit'):
        Store(str(tmp_path), FullDisk())
    with sqlite3.connect(tmp_path/'index.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM documents').fetchone()[0] == 2
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='paths_short_fts'").fetchone() is None
    store = Store(str(tmp_path))
    try:
        def match(query):
            return [row['rowid'] for row in store.rows('SELECT rowid FROM paths_short_fts WHERE paths_short_fts MATCH ?', ('"'+query+'"',))]
        assert match('财') == [first] and match('预算') == [first]
        with store.db:
            store.db.execute('UPDATE documents SET path=? WHERE id=?', ('合同/采购.txt',first))
        assert not match('预算') and match('采购') == [first]
        with store.db:
            store.db.execute("UPDATE documents SET source_id='db' WHERE id=?", (first,))
        assert not match('采购')
        with store.db:
            store.db.execute("UPDATE documents SET source_id='files' WHERE id=?", (first,))
        store.remove([first])
        assert not match('采购')
        with store.db:
            store.db.execute("INSERT INTO paths_short_fts(paths_short_fts,rank) VALUES('integrity-check',1)")
    finally:
        store.close()


@pytest.fixture
def engine(tmp_path):
    root = tmp_path/'files'
    root.mkdir()
    config = defaults(str(tmp_path/'data'), [str(root)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    instance = Engine(config)
    yield instance, root
    instance.close()


def test_short_chinese_files_use_posting_candidates_and_preserve_literal_paths(engine):
    instance, root = engine
    for name in ['财务资料.md', '年度预算.txt', 'prefix_100%预算.txt', 'prefixA100x.txt']:
        (root/name).write_text('synthetic only', encoding='utf-8')
    instance.scan_once()
    statements = []
    instance.store.db.set_trace_callback(statements.append)
    try:
        assert [r['name'] for r in instance.search('财','files')['results']] == ['财务资料.md']
        assert {r['name'] for r in instance.search('预算','files')['results']} == {'年度预算.txt','prefix_100%预算.txt'}
        sql = next(s for s in statements if 'paths_short_fts MATCH' in s and s.startswith('SELECT d.*'))
        plan = instance.store.rows('EXPLAIN QUERY PLAN '+sql)
        assert any('VIRTUAL TABLE INDEX' in row['detail'] and 'M' in row['detail'] for row in plan)
        assert any('INTEGER PRIMARY KEY' in row['detail'] for row in plan)
        assert [r['name'] for r in instance.search('_100%','files')['results']] == ['prefix_100%预算.txt']
        assert [r['name'] for r in instance.search(str(root/'prefix_100%预算.txt'),'files')['results']] == ['prefix_100%预算.txt']
        assert not instance.search('财','files',extension='.txt')['results']
    finally:
        instance.store.db.set_trace_callback(None)


def test_filtered_semantic_engine_dispatch_scores_only_matching_source_extension(engine, monkeypatch):
    instance, root = engine
    for name in ['near.txt','target.md']:
        (root/name).write_text(name, encoding='utf-8')
    instance.scan_once()
    query = np.zeros(512, dtype=np.float32)
    query[0] = 1
    with instance.store.db:
        for row in instance.store.rows('SELECT hash FROM chunks'):
            instance.store.db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (row['hash'], MODEL_ID, pack_vector(query)))
    instance._changed()
    instance.vectors.sync(isolated=False)
    monkeypatch.setattr(instance, '_encode', lambda *a,**kw:[query.tolist()])
    result = instance.search('semantic question', 'semantic', source_id='files', extension='.MD', limit=1)
    assert result['results'][0]['name'] == 'target.md'
    assert any(w.startswith('filtered_semantic_exact:') for w in result['warnings'])
    assert not instance.search('semantic question','semantic',source_id='absent')['results']
