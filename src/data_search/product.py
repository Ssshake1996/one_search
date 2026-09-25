"""User-facing diagnostics and bounded evidence operations over the shared engine."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .extractors import TEXT_EXTENSIONS, TEXT_NAMES
from .model import model_ready
from .search_filters import path_predicate
from .store import query_terms

SCHEMA_VERSION = 1
CONTENT_EXTENSIONS = TEXT_EXTENSIONS | {'.pdf', '.docx', '.xlsx', '.pptx', '.csv', '.tsv', '.html', '.htm'}
SENSITIVE_NAMES = {'.env', '.npmrc', '.pypirc', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
SENSITIVE_DIRS = {'.ssh', '.aws', '.azure', '.gnupg', '.kube'}


def file_identity(stat):
    # In-place edits retain identity; replacement files cannot inherit old IDs.
    return f'{stat.st_dev}:{stat.st_ino}' if stat.st_ino else 'unavailable'


def sensitive_path(path):
    p = Path(path)
    return p.name.casefold() in SENSITIVE_NAMES or p.name.casefold().startswith('.env.') or any(part.casefold() in SENSITIVE_DIRS for part in p.parts)


def cite(result):
    return {'node_id': result['node_id'], 'source_id': result['source_id'], 'id': result['id'],
            'path': result['path'], 'locator': result['locator'], 'indexed_at': result['indexed_at'],
            'revision': result.get('revision'), 'stale': result['stale']}


def diagnose(engine, path, query=None):
    p = Path(os.path.abspath(Path(path).expanduser()))
    report = {'schema_version': SCHEMA_VERSION, 'node_id': engine.config['node_id'], 'path': str(p),
              'checked_at': time.time(), 'coverage_complete': False, 'actions': [], 'query': query}
    def finish(code, *actions):
        report.update(code=code, actions=list(actions))
        return report
    if not engine.allowed(p):
        return finish('excluded_or_outside_scope', 'review_scope')
    try:
        stat = p.stat()
        if p.is_dir():
            with os.scandir(p):
                pass
            clause, args = path_predicate(p)
            counts = engine.store.rows('SELECT d.status,count(*) count FROM documents d WHERE 1=1'+clause+' GROUP BY d.status', args)
            passes = [row for row in engine.catalog.progress()['roots'] if p.is_relative_to(Path(row['path'])) or Path(row['path']).is_relative_to(p)]
            report.update(kind='directory', indexed_documents={r['status']:r['count'] for r in counts}, discovery_passes=passes,
                          note='Counts describe known indexed files; discovery may not have seen all files. No coverage percentage is inferred.')
            return finish('directory_coverage', 'prioritize_path', 'review_scope')
        with p.open('rb'):
            pass
    except FileNotFoundError:
        return finish('missing_or_moved', 'search_filename')
    except OSError:
        return finish('permission_denied_or_unavailable', 'check_source_access')
    report.update(kind='file', size=stat.st_size, content_supported=p.suffix.lower() in CONTENT_EXTENSIONS or p.name.lower() in TEXT_NAMES or p.name.lower() in TEXT_EXTENSIONS)
    rows = engine.store.rows('SELECT * FROM documents WHERE key=?', ('file:'+str(p.resolve()),))
    if not rows:
        return finish('not_discovered', 'prioritize_path')
    row = rows[0]
    report.update(document_id='d:'+str(row['id']), status=row['status'], reason=row['reason'], indexed_at=row['indexed_at'])
    if row.get('file_identity') is None:
        return finish('legacy_identity_unverified','refresh_path')
    if row.get('file_identity') and row['file_identity'] != file_identity(stat):
        return finish('replaced_file', 'refresh_path')
    if (row['size'],row['mtime_ns']) != (stat.st_size,stat.st_mtime_ns):
        return finish('stale', 'refresh_path')
    pending = engine.store.rows('SELECT available_at,attempts FROM file_work WHERE doc_id=?', (row['id'],))
    report['queue'] = pending[0] if pending else None
    if not engine._tier_allowed(p, 'content'):
        return finish('content_excluded', 'review_content_scope')
    if row['status'] in {'pending', 'error', 'budget', 'unsupported', 'partial','encrypted'}:
        codes = {'pending':'content_pending', 'error':'extraction_failed', 'budget':'resource_or_file_limit',
                 'unsupported':'unsupported_content', 'partial':'partial_content','encrypted':'encrypted_document'}
        actions = ['review_format_support'] if row['status']=='unsupported' else ['unlock_source_then_refresh'] if row['status']=='encrypted' else ['refresh_path','review_limits']
        return finish(codes[row['status']], *actions)
    report['coverage_complete'] = row['status']=='ready'
    if query:
        expression = query_terms(query)
        hit = bool(expression and engine.store.rows('SELECT 1 FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? AND c.doc_id=? LIMIT 1', (expression,row['id'])))
        report['keyword_match'] = hit
        report['filename_match'] = query.casefold() in str(p).casefold()
        report['query_assessment'] = 'match' if hit or report['filename_match'] else 'no_keyword_match_in_indexed_content'
    if not engine.config['semantic']['enabled'] or not engine._tier_allowed(p,'semantic'):
        report['semantic'] = 'disabled_or_excluded'
    elif not model_ready(engine.config['semantic']['model_dir']):
        report['semantic'] = 'model_missing'
    else:
        report['semantic'] = 'pending' if engine.vectors.status()['pending'] else 'published'
    return finish('ready', 'fetch', *(['model_start'] if report['semantic']=='model_missing' else []))


def prioritize(engine, path):
    p = Path(os.path.abspath(Path(path).expanduser()))
    if not engine.allowed(p) or not p.exists():
        raise ValueError('target unavailable or outside allowed scope')
    if not engine.scan_lock.acquire(blocking=False):
        return {'accepted':False, 'code':'indexer_busy', 'retry_after_seconds':1}
    try:
        engine.budget.check(disk=True, reserve_mb=1)
        if p.is_dir():
            engine.catalog.prioritize_directory(p)
        else:
            stat = p.stat()
            engine._metadata_batch([(str(p.resolve()),stat.st_size,stat.st_mtime_ns,file_identity(stat))], priority=True)
            with engine.store.lock, engine.store.db:
                row = engine.store.db.execute('SELECT id,status FROM documents WHERE key=?', ('file:'+str(p.resolve()),)).fetchone()
                if row and engine._tier_allowed(p,'content'):
                    engine.store.db.execute("UPDATE documents SET status='pending' WHERE id=?", (row['id'],))
                    engine.store.db.execute('INSERT INTO file_work(doc_id,priority) VALUES(?,1) ON CONFLICT(doc_id) DO UPDATE SET available_at=0,priority=1', (row['id'],))
        return {'accepted':True, 'code':'queued', 'path':str(p), 'paused':engine.paused, 'scope_expanded':False}
    finally:
        engine.scan_lock.release()


def refresh(engine, path):
    p = Path(os.path.abspath(Path(path).expanduser()))
    if not engine.allowed(p):
        raise ValueError('target outside allowed scope')
    if p.is_dir():
        return prioritize(engine, str(p))
    if not engine.scan_lock.acquire(blocking=False):
        return {'accepted':False, 'code':'indexer_busy', 'retry_after_seconds':1}
    try:
        # One explicitly requested file, under normal parser/time/memory limits.
        # Background pause is preserved, explicit refresh is permitted.
        rows = engine.store.rows('SELECT id FROM documents WHERE key=?', ('file:'+str(p.resolve()),))
        if rows:
            with engine.store.lock, engine.store.db:
                engine.store.db.execute("UPDATE documents SET status='pending' WHERE id=?", (rows[0]['id'],))
        engine._file(p,'on_demand',explicit=True)
        engine._coverage_cache = None
        return {'accepted':True, 'code':'refreshed', 'diagnosis':diagnose(engine,str(p)), 'semantic':'queued_for_background'}
    finally:
        engine.scan_lock.release()


def context(engine, id, before=1, after=2):
    before, after = int(before), int(after)
    if not 0<=before<=5 or not 0<=after<=10:
        raise ValueError('context bounds: before 0..5, after 0..10')
    found = engine.fetch(id,limit=1)
    doc = found.get('document')
    if not doc:  # Live database fetch has its own bounded row result.
        return found
    with engine.store.lock:
        # Revalidate the anchor and read neighbors without an intervening index write.
        doc = engine.fetch(id,limit=1)['document']
        doc_id = int(doc['document_id'].split(':')[1])
        anchor = int(id.split(':')[1]) if id.startswith('c:') else None
        rank = engine.store.rows('SELECT count(*) n FROM chunks WHERE doc_id=? AND id<?', (doc_id,anchor))[0]['n'] if anchor else 0
        start = max(0,rank-before)
        result = engine.fetch(doc['document_id'],offset=start,limit=rank-start+after+1)
        result['citation'] = cite(doc)
        result['hit_chunk_id'] = anchor
    return result


def open_source(engine, id, folder=False):
    prefix, raw = id.split(':',1)
    if prefix=='c':
        rows = engine.store.rows('SELECT d.* FROM documents d JOIN chunks c ON c.doc_id=d.id WHERE c.id=?',(int(raw),))
    elif prefix=='d':
        rows = engine.store.rows('SELECT * FROM documents WHERE id=?',(int(raw),))
    else:
        raise ValueError('invalid result ID')
    if not rows or rows[0]['source_id']!='files' or not engine._result(rows[0],'open',0):
        raise ValueError('file unavailable or outside scope')
    p = Path(rows[0]['path'])
    # Executables/scripts/shortcuts are never launched as an evidence action.
    if not folder and p.suffix.lower() not in {'.txt','.md','.pdf','.docx','.xlsx','.pptx','.csv','.tsv','.log'}:
        raise ValueError('use folder=true for this file type; executable content is not opened')
    if sys.platform=='win32':
        if folder:
            subprocess.Popen(['explorer.exe','/select,',str(p)], creationflags=0x08000000)
        else:
            os.startfile(str(p))
    elif sys.platform.startswith('linux'):
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            return {'opened':False,'code':'headless','path':str(p),'citation':cite(engine._result(rows[0],'open',0))}
        subprocess.Popen(['xdg-open',str(p.parent if folder else p)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:
        raise ValueError('opening sources unsupported on this platform')
    return {'opened':True,'path':str(p),'folder':bool(folder)}


def group_evidence(engine, results, fold=False):
    """Fold only equal complete extracted text hashes among the returned candidates."""
    groups, output = {}, []
    for hit in results:
        doc_id = int(hit['document_id'].split(':')[1])
        hashes = engine.store.rows('SELECT hash FROM chunks WHERE doc_id=? ORDER BY id LIMIT 20001', (doc_id,))
        digest = hashlib.sha256(''.join(r['hash'] for r in hashes).encode()).hexdigest() if hashes and len(hashes)<=20000 and hit['status']=='ready' and not hit['stale'] else None
        hit['duplicate_basis'] = 'complete_extracted_text_in_returned_candidates' if digest else None
        hit['content_group'] = digest
        hit['version_note'] = 'Modification date and filename do not establish the latest approved version.'
        if fold and digest and digest in groups:
            groups[digest].setdefault('other_locations',[]).append(cite(hit))
        else:
            output.append(hit)
            if digest:
                groups[digest] = hit
    return output


def capabilities(engine):
    coverage, progress = engine.coverage(), engine.catalog.progress()
    cached = getattr(engine,'_source_status_cache',None)
    if cached is None or time.monotonic()-cached[0]>5:
        rows = engine.store.rows('SELECT source_id,status,count(*) count,max(indexed_at) latest FROM documents GROUP BY source_id,status')
        engine._source_status_cache = (time.monotonic(),rows)
    else:
        rows = cached[1]
    sources = {}
    for row in rows:
        source = sources.setdefault(row['source_id'], {'states':{},'last_indexed_at':None})
        source['states'][row['status']] = row['count']
        if row['latest'] and (not source['last_indexed_at'] or row['latest']>source['last_indexed_at']):
            source['last_indexed_at'] = row['latest']
    model = model_ready(engine.config['semantic']['model_dir'])
    return {'schema_version':SCHEMA_VERSION, 'filename':{'available':True,'discovery_active':progress['discovery_active']},
            'keyword':{'available':True,'pending_files':progress['queued_files']},
            'semantic':{'available':engine.config['semantic']['enabled'] and model and engine.vectors.meta.is_file(),
                        'model_ready':model,'publication_pending':engine.vectors.status()['pending']},
            'sources':sources, 'remote_nodes':{'implemented':False},
            'scope_complete':False, 'coverage_note':'Counts cover discovered items; unknown and inaccessible items prevent claiming whole-machine completeness.',
            'known_documents':sum(coverage['documents'].values())}


def scope_impact(engine, changes, max_documents=10000):
    from copy import deepcopy
    from .scope import FileScope
    allowed_keys = {'scope','roots','exclude_paths','exclude_names','indexing'}
    if not isinstance(changes,dict) or set(changes)-allowed_keys:
        raise ValueError('scope preview accepts only scope/roots/exclusions/indexing')
    candidate = deepcopy(engine.config)
    candidate.update(changes)
    scope = FileScope(candidate)
    after, checked, removed, content_removed = 0,0,0,0
    limit = max(1,min(int(max_documents),10000))
    def content_allowed(path):
        settings = candidate.get('indexing',{})
        p = Path(path)
        return (settings.get('content_scope','all')!='none' and
                (settings.get('content_scope','all')!='directories' or any(p.is_relative_to(Path(root).resolve()) for root in settings.get('content_roots',[]))) and
                (not settings.get('content_extensions') or p.suffix.lower() in settings['content_extensions']) and
                not any(p.is_relative_to(Path(root).resolve()) for root in settings.get('content_exclude_paths',[])) and
                not (settings.get('sensitive_content_excluded') and sensitive_path(p)))
    while checked<limit:
        rows = engine.store.rows("SELECT id,path,status FROM documents WHERE source_id='files' AND id>? ORDER BY id LIMIT ?",(after,min(256,limit-checked)))
        if not rows:
            break
        for row in rows:
            if not scope.allowed(Path(row['path'])):
                removed += 1
            elif not content_allowed(row['path']) and row['status'] in {'ready','partial','pending'}:
                content_removed += 1
        after,checked = rows[-1]['id'],checked+len(rows)
    truncated = bool(engine.store.rows("SELECT 1 FROM documents WHERE source_id='files' AND id>? LIMIT 1",(after,)))
    return {'preview':True,'applied':False,'checked_known_files':checked,'truncated':truncated,
            'known_files_revoked':removed,'known_content_caches_revoked':content_removed,'scope':scope.report(),
            'note':'A bounded preview of known files; newly included files are discovered after applying settings. Revoked caches are cleared when the service activates the new scope.'}
