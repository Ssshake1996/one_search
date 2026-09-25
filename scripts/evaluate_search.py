"""Reproducible tiny-corpus quality check; never a general accuracy claim."""
import argparse
import json
import platform
import time
from pathlib import Path

from data_search.config import defaults
from data_search.engine import Engine


def run(args):
    demo = Path(args.demo).resolve()
    config = defaults(str(Path(args.data).resolve()), [str(demo / 'files')])
    config['semantic']['model_dir'] = str(Path(args.model).resolve())
    config['resource']['batch_sleep_ms'] = 0
    config['databases'] = [json.loads((demo / 'database_config.json').read_text(encoding='utf-8'))]
    cases = json.loads(Path(args.cases).read_text(encoding='utf-8'))['cases']
    engine = Engine(config)
    records = []
    begin = time.perf_counter()
    try:
        # Production ticks are deliberately bounded. Evaluation must wait for
        # this explicitly selected synthetic corpus, not measure a partial index.
        deadline = time.monotonic() + 300
        for tick in range(200):
            status = engine.scan_once()
            if engine.vector_thread is not None:
                engine.vector_thread.join(30)
            pending = engine.store.rows('SELECT count(*) n FROM chunks c LEFT JOIN embeddings e ON c.hash=e.hash AND e.model=? WHERE c.semantic=1 AND e.hash IS NULL', (status['semantic']['model_id'],))[0]['n']
            progress = engine.catalog.progress()
            if (not pending and not progress['discovery_active'] and not progress['queued_files']
                    and not progress['queued_events'] and not engine.vectors.status()['pending']):
                engine._coverage_cache = None
                status = engine.status()
                break
            if status['last_error'] or status.get('vector_error') or time.monotonic() >= deadline:
                raise RuntimeError('Synthetic evaluation indexing did not complete: '+str(status['last_error'] or status.get('vector_error') or 'deadline'))
        else:
            raise RuntimeError('Synthetic evaluation indexing exceeded 200 bounded ticks')
        index_seconds = time.perf_counter() - begin
        for case in cases:
            if case['mode'] == 'database':
                result = engine.query_database(case['source_id'], case['request'])
                expected = case['expected_evidence']
                if not case['expected_sources']:
                    passed = result['row_count'] == 0
                elif isinstance(expected, dict):
                    passed = bool(result['rows']) and all(result['rows'][0].get(k) == v for k,v in expected.items())
                else:
                    passed = expected in json.dumps(result['rows'], ensure_ascii=False)
                records.append({'id':case['id'], 'mode':'database', 'passed':passed, 'result':result})
                continue
            result = engine.search(case['query'], case['mode'], limit=10,
                                   source_id=case.get('source_id','files'), extension=case.get('extension'))
            paths = [Path(r['path']).relative_to(demo).as_posix() for r in result['results']]
            rank = next((i+1 for i,p in enumerate(paths) if p in case['expected_sources']), None)
            records.append({'id':case['id'], 'mode':case['mode'], 'language':case['language'],
                'answerability':case['answerability'], 'first_relevant_rank':rank,
                'recall_at_5':bool(rank and rank<=5), 'recall_at_10':bool(rank and rank<=10),
                'elapsed_ms':result['elapsed_ms'], 'warnings':result['warnings'],
                'expected_evidence':case['expected_evidence'],
                'top_results':[{'path':p,'snippet':r['snippet'],'locator':r['locator']} for p,r in zip(paths,result['results'])],
                'evidence_only':result['evidence_only']})
        summaries = {}
        for mode in ('semantic','keyword','files'):
            selected = [r for r in records if r['mode']==mode and r['answerability']=='answerable']
            summaries[mode] = {'count':len(selected),'recall_at_5':sum(r['recall_at_5'] for r in selected)/max(1,len(selected)),
                               'recall_at_10':sum(r['recall_at_10'] for r in selected)/max(1,len(selected))}
        for language in ('zh','en','cross_language'):
            selected = [r for r in records if r['mode']=='semantic' and r['language']==language]
            summaries[language] = {'count':len(selected),'recall_at_5':sum(r['recall_at_5'] for r in selected)/max(1,len(selected))}
        file_count = sum(path.is_file() for path in (demo/'files').rglob('*'))
        report = {'corpus':f'synthetic {file_count} files and SQLite demo only', 'platform':platform.platform(),
            'model_id':status['semantic']['model_id'], 'initial_index_seconds':round(index_seconds,3),
            'status_after_index':status, 'summary':summaries,
            'database_passed':sum(r.get('passed',False) for r in records if r['mode']=='database'),
            'limitations':['File-level recall is not answer/evidence correctness.',
                          'No-answer cases are recorded for evidence review; no answer model is evaluated.',
                          'Tiny synthetic corpus is not a large-file performance benchmark.'], 'cases':records}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'summary':summaries,'database_passed':report['database_passed'],'report':str(output)},ensure_ascii=True))
    finally:
        engine.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--data',required=True)
    parser.add_argument('--cases',default='tests/evaluation_queries.json')
    parser.add_argument('--output',default='docs/validation/quality.json')
    run(parser.parse_args())
