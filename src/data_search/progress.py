"""Presentation contract for known work, never a whole-machine percentage."""
from __future__ import annotations

from datetime import datetime, timezone


def index_progress(status: dict) -> dict:
    """Summarize an existing status snapshot without queries or resource sampling.

    Stages overlap. File discovery has no known denominator, and an idle scheduler
    tick does not mean its durable queues are empty. Counts may be cached for five
    seconds; semantic coverage can decrease when new content is discovered.
    """
    coverage, scheduler = status['coverage'], status['scheduler']
    policy, semantic, vectors = status['runtime_policy'], status['semantic'], status['vector_index']
    scope = status['file_scope']
    counts = dict(coverage['file_documents'])
    roots = [{key: row.get(key) for key in ('path', 'phase', 'generation', 'errors', 'completed_at')}
             | {'observed_files': row['discovered']} for row in scheduler['roots']]
    root_paths = {row['path'] for row in roots}
    unstarted = any(path not in root_paths for path in scope['effective_roots'])
    discovery_active = scheduler['discovery_active'] or unstarted
    scan_errors = scope.get('scan_errors', {'count': 0, 'samples': []})
    discovery_complete = (not discovery_active and not scheduler['queued_directories']
                          and not any(row['errors'] for row in roots) and not scan_errors['count']
                          and not scope.get('unavailable_roots') and not scope.get('discovery_error'))

    # Do not expose DB cursors, key values or watermarks in the UI contract.
    table_keys = ('phase', 'mode', 'scanned_rows', 'pages', 'deleted_rows',
                  'started_at', 'completed_at', 'last_error')
    sources = {source_id: {'last_error': source.get('last_error'), 'tables': {
        table: {key: row.get(key) for key in table_keys} for table, row in source.get('tables', {}).items()}}
        for source_id, source in status['database_sync'].items()}
    database_work = any(not source['last_error'] and (not source['tables'] or any(
        row['phase'] in ('scanning', 'reconciling') and not row['last_error']
        for row in source['tables'].values())) for source in sources.values())
    enabled = semantic['enabled']
    model_state = semantic['lifecycle']['state']
    model_ready = semantic['lifecycle']['ready']
    eligible, embedded = coverage['semantic_eligible_chunks'], coverage['embedded_chunks']
    semantic_work = enabled and model_ready and (embedded < eligible or vectors['building'] or
        (vectors['pending'] and (eligible > 0 or vectors['published_chunks'] > 0)))
    has_errors = (status['last_error'] or coverage['source_errors'] or scan_errors['count']
                  or scope.get('unavailable_roots') or scope.get('discovery_error')
                  or any(row['errors'] for row in roots) or (enabled and status['vector_error'])
                  or any(counts.get(key, 0) for key in ('error', 'budget', 'encrypted', 'partial'))
                  or any(source['last_error'] or any(row['last_error'] for row in source['tables'].values())
                         for source in sources.values()))
    known_work = (discovery_active or scheduler['queued_directories'] or scheduler['ready_files']
                  or scheduler['queued_events'] or not scheduler['chunking_migration']['done']
                  or database_work or semantic_work)
    if policy['user_paused']:
        state, reason = 'paused', 'user_pause'
    elif policy['automatic_wait']:
        state, reason = 'waiting', policy['reason']
    elif known_work:
        state, reason = 'indexing', 'known_tasks_pending'
    elif has_errors:
        state, reason = 'needs_attention', 'incomplete_sources'
    elif scheduler['retry_files']:
        state, reason = 'waiting', 'retry_backoff'
    elif enabled and not model_ready:
        state, reason = ('needs_attention' if model_state in ('failed', 'interrupted', 'cancelled')
                         else 'waiting'), 'model_not_ready'
    else:
        state, reason = 'up_to_date', None

    return {
        'schema_version': 1, 'sampled_at': datetime.now(timezone.utc).isoformat(),
        'overall': {'state': state, 'reason': reason, 'scope_complete': False},
        'known_unique_files': sum(counts.values()),
        'discovery': {'complete': discovery_complete, 'active': bool(discovery_active), 'total': None,
                      'queued_directories': scheduler['queued_directories'], 'roots': roots},
        'content': {'counts': counts, 'pending': scheduler['queued_files'],
                    'ready_to_process': scheduler['ready_files'], 'retry_waiting': scheduler['retry_files'],
                    'next_retry_at': scheduler['next_retry_at'], 'queued_events': scheduler['queued_events']},
        'semantic': {'enabled': enabled, 'embedded': embedded, 'eligible': eligible,
                     'vector_pending': bool(enabled and vectors['pending']),
                     'vector_building': bool(enabled and vectors['building']), 'model_state': model_state},
        'databases': {'sources': sources}, 'resources': status['resources'], 'runtime_policy': policy,
        'error_summary': {'last_error': status['last_error'], 'source_errors': coverage['source_errors'],
                          'scan_errors': scan_errors, 'vector_error': status['vector_error'],
                          'unavailable_roots': scope.get('unavailable_roots', []),
                          'discovery_error': scope.get('discovery_error')},
    }
