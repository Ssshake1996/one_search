from copy import deepcopy
from datetime import datetime
import json
import time

import pytest

from data_search.config import defaults
from data_search.engine import Engine
from data_search.progress import index_progress


@pytest.fixture
def snapshot():
    return {
        'coverage': {'file_documents': {'ready': 2}, 'documents': {'ready': 8},
                     'semantic_eligible_chunks': 3, 'embedded_chunks': 3, 'source_errors': {},
                     'scanning': False},
        'scheduler': {'roots': [{'path': '/files', 'phase': 'done', 'generation': 'one',
                                'discovered': 4, 'errors': 0, 'completed_at': 1.0}],
                      'discovery_active': False, 'queued_directories': 0, 'queued_files': 0,
                      'ready_files': 0, 'retry_files': 0, 'next_retry_at': None,
                      'queued_events': 0, 'chunking_migration': {'done': True}},
        'file_scope': {'effective_roots': ['/files'], 'unavailable_roots': [], 'discovery_error': None,
                       'scan_errors': {'count': 0, 'samples': []}},
        'runtime_policy': {'user_paused': False, 'automatic_wait': False, 'reason': None},
        'semantic': {'enabled': True, 'lifecycle': {'state': 'ready', 'ready': True}},
        'vector_index': {'pending': False, 'building': False, 'published_chunks': 3},
        'database_sync': {}, 'last_error': None, 'vector_error': None,
        'resources': {'rss_mb': 24, 'disk_mb': 5, 'available_mb': 2048},
    }


def test_known_counts_are_not_whole_machine_completeness(snapshot):
    before = deepcopy(snapshot)
    report = index_progress(snapshot)
    assert report['schema_version'] == 1
    assert datetime.fromisoformat(report['sampled_at']).utcoffset().total_seconds() == 0
    assert report['overall'] == {'state': 'up_to_date', 'reason': None, 'scope_complete': False}
    assert report['known_unique_files'] == 2  # Neither DB records nor repeat traversal observations.
    assert report['discovery']['roots'][0]['observed_files'] == 4
    assert report['discovery']['total'] is None
    assert report['discovery']['complete']
    assert snapshot == before


def test_paused_and_automatic_wait_keep_work_visible(snapshot):
    snapshot['scheduler'].update(queued_files=6, ready_files=6)
    snapshot['runtime_policy'].update(user_paused=True)
    report = index_progress(snapshot)
    assert report['overall']['state'] == 'paused'
    assert report['content']['pending'] == 6
    snapshot['runtime_policy'].update(user_paused=False, automatic_wait=True, reason='waiting_for_ac_power')
    assert index_progress(snapshot)['overall'] == {
        'state': 'waiting', 'reason': 'waiting_for_ac_power', 'scope_complete': False}


@pytest.mark.parametrize('state', ['missing', 'downloading', 'failed', 'cancelled', 'interrupted'])
def test_model_unavailable_is_not_a_completed_semantic_stage(snapshot, state):
    snapshot['semantic']['lifecycle'].update(state=state, ready=False)
    snapshot['coverage']['embedded_chunks'] = 0
    report = index_progress(snapshot)
    assert report['overall']['state'] == ('waiting' if state in ('missing', 'downloading') else 'needs_attention')
    assert report['overall']['reason'] == 'model_not_ready'
    assert report['semantic']['model_state'] == state
    assert report['semantic']['eligible'] == 3
    # Basic discovery/parse work continues even when a model is missing.
    snapshot['scheduler'].update(queued_files=1, ready_files=1)
    assert index_progress(snapshot)['overall']['state'] == 'indexing'


def test_vector_publication_is_separate_and_disabled_semantics_do_not_block(snapshot):
    snapshot['vector_index'].update(pending=True, building=True)
    report = index_progress(snapshot)
    assert report['semantic']['embedded'] == report['semantic']['eligible']
    assert report['semantic']['vector_pending'] and report['semantic']['vector_building']
    assert report['overall']['state'] == 'indexing'
    snapshot['semantic']['enabled'] = False
    snapshot['semantic']['lifecycle'].update(state='missing', ready=False)
    report = index_progress(snapshot)
    assert report['overall']['state'] == 'up_to_date'
    assert not report['semantic']['vector_pending'] and not report['semantic']['vector_building']


def test_unknown_new_roots_and_new_content_never_get_a_global_percent(snapshot):
    snapshot['file_scope']['effective_roots'].append('/new-root')
    report = index_progress(snapshot)
    assert report['discovery']['active'] and not report['discovery']['complete']
    assert report['overall']['state'] == 'indexing'
    snapshot['coverage']['semantic_eligible_chunks'] = 7
    report = index_progress(snapshot)
    assert report['semantic']['embedded'] == 3 and report['semantic']['eligible'] == 7
    assert report['discovery']['total'] is None


def test_scan_and_source_failures_prevent_a_complete_report(snapshot):
    snapshot['scheduler']['roots'][0]['errors'] = 1
    report = index_progress(snapshot)
    assert not report['discovery']['complete']
    assert report['overall']['state'] == 'needs_attention'
    snapshot['scheduler']['roots'][0]['errors'] = 0
    snapshot['file_scope']['scan_errors'] = {'count': 1, 'samples': [{'path': '/files/private', 'reason': 'PermissionError'}]}
    report = index_progress(snapshot)
    assert not report['discovery']['complete']
    assert report['error_summary']['scan_errors']['count'] == 1


def test_retry_queue_and_database_pipeline_are_distinguished(snapshot):
    snapshot['scheduler'].update(queued_files=2, retry_files=2, next_retry_at=123.0)
    report = index_progress(snapshot)
    assert report['overall']['reason'] == 'retry_backoff'
    assert report['content']['ready_to_process'] == 0
    assert report['content']['next_retry_at'] == 123.0
    snapshot['database_sync'] = {'business': {'tables': {'notes': {
        'phase': 'reconciling', 'mode': 'full', 'scanned_rows': 500, 'pages': 5,
        'last_error': None, 'cursor': {'id': 'secret-row-key'}, 'watermark': 'secret-watermark'}}, 'last_error': None}}
    report = index_progress(snapshot)
    assert report['overall']['state'] == 'indexing'
    assert report['databases']['sources']['business']['tables']['notes']['scanned_rows'] == 500
    assert 'secret' not in json.dumps(report)


@pytest.mark.parametrize('field,value', [
    ('unavailable_roots', [{'path': '/missing', 'reason': 'FileNotFoundError'}]),
    ('discovery_error', 'local_volumes_unavailable'),
])
def test_scope_discovery_errors_explain_attention_state(snapshot, field, value):
    snapshot['file_scope'][field] = value
    report = index_progress(snapshot)
    assert report['overall']['state'] == 'needs_attention'
    assert not report['discovery']['complete']
    assert report['error_summary'][field] == value


def test_real_catalog_new_files_pause_restart_and_cached_unique_counts(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    config = defaults(str(tmp_path / 'index'), [str(root)])
    config['semantic']['enabled'] = False
    engine = Engine(config)
    try:
        # No scheduler tick is running, but an unstarted accessible root is work.
        initial = engine.status()
        assert not initial['coverage']['scanning']
        assert initial['progress']['overall']['state'] == 'indexing'
        engine.catalog.begin()
        engine.catalog.discover()
        assert engine.status()['progress']['overall']['state'] == 'up_to_date'
        (root / 'new.txt').write_text('new known content', encoding='utf-8')
        engine.catalog.begin()
        engine.catalog.discover()
        with engine.store.lock, engine.store.db:
            engine.store.db.execute("INSERT INTO documents(key,source_id,path,name,status) VALUES('db:one','business','notes','notes','ready')")
        engine._coverage_cache = None
        report = engine.status()
        assert report['progress']['known_unique_files'] == 1
        assert sum(report['coverage']['documents'].values()) == 2
        assert report['progress']['content']['pending'] == 1
        assert report['progress']['content']['ready_to_process'] == 1
        cached = engine._coverage_cache
        assert engine.status()['progress']['known_unique_files'] == 1
        assert engine._coverage_cache is cached
        retry_at = time.time() + 60
        with engine.store.lock, engine.store.db:
            engine.store.db.execute('UPDATE file_work SET available_at=?', (retry_at,))
        report = engine.status()['progress']
        assert report['content']['retry_waiting'] == 1
        assert report['content']['next_retry_at'] == retry_at
        engine.dispatch('pause', {'seconds': 1800})
        assert engine.status()['progress']['overall']['state'] == 'paused'
    finally:
        engine.close()
    reopened = Engine(config)
    try:
        report = reopened.status()['progress']
        assert report['overall']['state'] == 'paused'
        assert report['known_unique_files'] == 1
        assert report['content']['pending'] == 1
        reopened.dispatch('resume', {})
        assert reopened.status()['progress']['overall']['reason'] == 'retry_backoff'
    finally:
        reopened.close()
