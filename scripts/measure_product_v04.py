"""Finite synthetic RPC/lifecycle smoke; not a production-scale soak benchmark."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import threading
import time

import psutil

from data_search import __version__
from data_search.config import atomic_json, defaults, load_config
from data_search.model import model_ready
from data_search.service import rpc, start_service, stop_service


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--concurrent-seconds', type=int, default=45)
    args = parser.parse_args(argv)
    if not 10 <= args.concurrent_seconds <= 120:
        parser.error('--concurrent-seconds must be 10..120')
    root = args.output.resolve()
    if root.exists():
        raise ValueError('Use a fresh output directory')
    if not model_ready(str(args.model_dir)):
        raise ValueError('A prepared pinned model is required; benchmark never downloads models')
    root.mkdir(parents=True)
    corpus, data = root / 'synthetic-corpus', root / 'data'
    corpus.mkdir()
    for n in range(24):
        (corpus / f'合同-{n:03}.txt').write_text(f'这是合成单机验收资料。项目海棠，采购批次{n}，批准金额{18000+n}元。状态已批准。' * 3, encoding='utf-8')
    config_path = root / 'config.json'
    config = defaults(str(data), [str(corpus)])
    config['semantic']['model_dir'] = str(args.model_dir.resolve())
    config['semantic']['idle_seconds'] = 60
    config['resource']['batch_sleep_ms'] = 0
    atomic_json(config_path, config)
    config = load_config(str(config_path))
    report = {'schema_version': 1, 'product_version': __version__, 'stage': 'source working tree',
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'host': {'platform': platform.platform(), 'python': platform.python_version(), 'cpu_logical': psutil.cpu_count(),
                 'physical_memory_bytes': psutil.virtual_memory().total},
        'corpus': {'initial_files': 24, 'initial_text_bytes': sum(p.stat().st_size for p in corpus.iterdir()), 'synthetic_only': True},
        'transport': 'authenticated local HTTP RPC including service/engine; excludes DSH generation',
        'method': {'warm_samples_per_mode': 12, 'concurrent_seconds': args.concurrent_seconds,
                   'cold_definition': 'first query of each mode after daemon restart; OS file cache is not flushed'},
        'limits': ['One short synthetic run, not hours/days soak', 'Not a physical 8GB/16GB test',
                   'Small corpus, not hundreds of GB', 'OS file cache may remain warm after daemon restart'], 'checks': {}}
    stop_event = threading.Event()
    writer = None
    modes = ['files', 'keyword', 'semantic']
    def check(condition, name):
        report['checks'][name] = bool(condition)
        if not condition:
            raise AssertionError(name)
    def wait_ready(maximum=90, require_quiet=False):
        deadline = time.monotonic() + maximum
        while time.monotonic() < deadline:
            status = rpc(config, 'index_status')
            coverage = status['coverage']
            if (coverage['chunks'] and coverage['embedded_chunks'] >= coverage['semantic_eligible_chunks']
                    and not status['vector_index'].get('pending')
                    and (not require_quiet or not coverage['scanning'])):
                return status
            time.sleep(.25)
        raise TimeoutError('Synthetic index readiness timed out')
    def timed(mode):
        query = '合同-003.txt' if mode == 'files' else '海棠 采购'
        start = time.perf_counter()
        result = rpc(config, 'search', {'query': query, 'mode': mode, 'limit': 5})
        elapsed = (time.perf_counter() - start) * 1000
        if not result.get('results'):
            raise AssertionError('Expected synthetic evidence for ' + mode)
        return elapsed
    def summary(values):
        ordered = sorted(values)
        return {'samples': len(values), 'median_ms': round(statistics.median(values), 3),
                'p95_ms': round(ordered[max(0, int(len(ordered) * .95 + .999999) - 1)], 3), 'max_ms': round(max(values), 3)}
    try:
        started = time.monotonic()
        start_service(config)
        indexed = wait_ready()
        report['initial_index_seconds'] = round(time.monotonic() - started, 3)
        report['corpus']['chunks'] = indexed['coverage']['chunks']
        report['corpus']['vectors'] = indexed['coverage']['embedded_chunks']
        stop_service(config)
        start_service(config)
        report['cold_first_query_ms'] = {mode: round(timed(mode), 3) for mode in modes}
        report['warm_rpc'] = {mode: summary([timed(mode) for _ in range(12)]) for mode in modes}
        rpc(config, 'pause', {'seconds': 1})
        check(rpc(config, 'index_status')['runtime_policy']['user_paused'], 'timed_pause_applied')
        check(timed('keyword') > 0, 'search_during_pause')
        time.sleep(1.1)
        check(not rpc(config, 'index_status')['runtime_policy']['user_paused'], 'timed_pause_expired')
        rpc(config, 'pause')
        stop_service(config)
        start_service(config)
        check(rpc(config, 'index_status')['runtime_policy']['user_paused'], 'user_pause_survived_restart')
        rpc(config, 'resume')
        writes = [0]
        writer_errors = []
        def update_source():
            try:
                while not stop_event.wait(.4):
                    n = writes[0] % 24
                    (corpus / f'合同-{n:03}.txt').write_text(f'合成单机并发验收。海棠采购项目，版本{writes[0]}，批准金额18000元。' * 3, encoding='utf-8')
                    writes[0] += 1
                    rpc(config, 'scan')
            except Exception as error:
                writer_errors.append(type(error).__name__)
        writer = threading.Thread(target=update_source, daemon=True)
        writer.start()
        deadline = time.monotonic() + args.concurrent_seconds
        measurements = {mode: [] for mode in modes}
        work_observations = []
        while time.monotonic() < deadline:
            for mode in modes:
                measurements[mode].append(timed(mode))
            status = rpc(config, 'index_status')
            work_observations.append({'scanning': status['coverage']['scanning'], 'pending_files': status['scheduler'].get('queued_files'),
                                      'vector_pending': status['vector_index'].get('pending')})
            time.sleep(.3)
        stop_event.set()
        writer.join(timeout=15)
        check(not writer.is_alive() and not writer_errors, 'background_writer_completed')
        report['concurrent_rpc'] = {mode: summary(values) for mode, values in measurements.items()}
        report['background'] = {'source_writes': writes[0], 'observations': len(work_observations),
            'scanning_observations': sum(bool(x['scanning']) for x in work_observations),
            'vector_pending_observations': sum(bool(x['vector_pending']) for x in work_observations)}
        rpc(config, 'pause')
        queued = corpus / '重启恢复.txt'
        queued.write_text('recoveryfixture2609 合成重启恢复标记', encoding='utf-8')
        for _ in range(50):
            result = rpc(config, 'prioritize_path', {'path': str(queued)})
            if result.get('accepted'):
                break
            time.sleep(.1)
        check(bool(result.get('accepted')), 'queued_while_paused')
        stop_service(config)
        start_service(config)
        check(rpc(config, 'index_status')['runtime_policy']['user_paused'], 'pending_restart_preserved_pause')
        recovery_started = time.monotonic()
        rpc(config, 'resume')
        deadline = time.monotonic() + 30
        found = False
        while time.monotonic() < deadline:
            found = bool(rpc(config, 'search', {'query': 'recoveryfixture2609', 'mode': 'keyword'})['results'])
            if found:
                break
            time.sleep(.2)
        check(found, 'queued_file_recovered_after_restart')
        report['queued_keyword_recovery_seconds'] = round(time.monotonic() - recovery_started, 3)
        settled = wait_ready(maximum=30, require_quiet=True)
        check(settled['coverage']['chunks'] == 25 and settled['coverage']['embedded_chunks'] == 25,
              'all_synthetic_vectors_recovered')
        report['all_vectors_recovery_seconds'] = round(time.monotonic() - recovery_started, 3)
        report['final_coverage'] = settled['coverage']
        report['ok'] = True
    except BaseException as error:
        report['ok'] = False
        report['error_type'] = type(error).__name__
        raise
    finally:
        stop_event.set()
        if writer:
            writer.join(timeout=15)
        try:
            stop_service(config)
            report['daemon_stopped'] = True
        except Exception:
            report['daemon_stopped'] = False
        atomic_json(root / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
