"""Reproducible SQLite/ANN query-kernel benchmark, NOT semantic model evaluation.

Default: 100,000 synthetic document records and 100,000 real text chunks. The
indexed paths are synthetic; this does not benchmark filesystem enumeration,
document parsing, MCP transport, embedding inference, or answer generation.
Vectors are seeded random numbers with no language meaning, explicitly tagged
as synthetic in both SQLite settings and the JSON report. Never reuse this
benchmark directory as a user index.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psutil

from data_search.config import atomic_json, defaults
from data_search.model import MODEL_ID
from data_search.resources import Budget
from data_search.store import Store, pack_vector, query_terms, text_hash, unpack_vector
from data_search.vectors import Vectors


TOPICS = [
    ("server-cost", "服务器降本", "测试环境夜间停机，周末关闭闲置机器，生产数据库保持运行。Cloud spending is reduced by stopping idle test instances."),
    ("order-return", "订单退货", "收到商品时杯柄破裂，客服核验照片后批准换货，商家承担运输费用。Damaged items require a documented replacement."),
    ("onboarding", "员工入职", "新员工先领取电脑并登记资产，再开通工作账号，下午参加信息安全培训。New employees complete device registration and security training."),
    ("project-plan", "项目计划", "先验证单机检索，再接入只读数据库，最后保留多机接口。The project starts with a local pilot and measurable acceptance criteria."),
    ("backup-recovery", "备份恢复", "在隔离环境恢复备份，核对记录总数和抽样订单，记录恢复时间。A completed backup does not prove that a recovery will succeed."),
    ("storage-io", "磁盘容量", "批量导入期间磁盘等待升高，限制写入并发并错开报表时间。Disk write contention can slow queries despite low CPU utilization."),
    ("remote-work", "远程办公", "员工可以申请外接显示器和键盘，维修先登记设备资产编号。Remote staff request peripherals through the equipment approval process."),
    ("resource-budget", "资源预算", "电脑剩余内存不足时暂停后台索引，释放本地模型，让办公软件优先运行。Background work pauses under memory pressure."),
]


class PeakRSS:
    def __init__(self):
        self.stop = threading.Event()
        self.peak = self._rss()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stop.wait(0.02):
            self.peak = max(self.peak, self._rss())

    @staticmethod
    def _rss():
        process = psutil.Process()
        result = 0
        for child in [process] + process.children(recursive=True):
            try:
                result += child.memory_info().rss
            except psutil.Error:
                pass
        return result

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.peak = max(self.peak, self._rss())
        self.stop.set()
        self.thread.join()


def populate(store: Store, start: int, count: int, generator, batch_size=500):
    text_bytes = 0
    for offset in range(0, count, batch_size):
        size = min(batch_size, count - offset)
        matrix = generator.standard_normal((size, 512), dtype=np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        documents, chunks, embeddings = [], [], []
        for local in range(size):
            number = start + offset + local
            slug, title, paragraph = TOPICS[number % len(TOPICS)]
            name = f"document-{number:06d}-{slug}.md"
            path = f"/synthetic/department-{number % 23:02d}/{slug}/{name}"
            text = f"{title} 合成记录 {number}。参考编号 REF-BENCH-{number:06d}。\n{paragraph}\n计划日期 2026-10-{number % 28 + 1:02d}，负责人为演示小组。"
            digest = text_hash(text)
            text_bytes += len(text.encode('utf-8'))
            documents.append((number, 'file:' + path, 'files', path, name, '.md', len(text.encode('utf-8')), 'ready'))
            chunks.append((number, number, text, digest, '{"line_start":1,"line_end":3}'))
            embeddings.append((digest, MODEL_ID, pack_vector(matrix[local])))
        with store.lock, store.db:
            store.db.executemany('INSERT INTO documents(id,key,source_id,path,name,extension,size,status) VALUES(?,?,?,?,?,?,?,?)', documents)
            store.db.executemany('INSERT INTO chunks(id,doc_id,text,hash,locator) VALUES(?,?,?,?,?)', chunks)
            store.db.executemany('INSERT INTO embeddings(hash,model,vector) VALUES(?,?,?)', embeddings)
        if (offset + size) % 10_000 == 0:
            print(f"Indexed {offset + size}/{count} synthetic text records", file=sys.stderr, flush=True)
    return text_bytes


def timed_queries(function, values, warmup):
    for value in values[:min(warmup, len(values))]:
        function(value)
    elapsed = []
    counts = []
    for value in values:
        start = time.perf_counter()
        result = function(value)
        elapsed.append((time.perf_counter() - start) * 1000)
        counts.append(len(result))
    return {
        'queries': len(values), 'warmup_queries': min(warmup, len(values)),
        'p50_ms': round(float(np.percentile(elapsed, 50)), 3),
        'p95_ms': round(float(np.percentile(elapsed, 95)), 3),
        'max_ms': round(max(elapsed), 3),
        'mean_results': round(sum(counts) / len(counts), 2),
        'samples_ms': [round(value, 4) for value in elapsed],
    }


def disk_bytes(directory):
    return sum(path.stat().st_size for path in directory.rglob('*') if path.is_file())


def sync_until_ready(vectors, expected_count, *, timeout_seconds=3600, max_passes=1000):
    """Each worker pass is bounded; timing covers the complete publication."""
    deadline = time.monotonic() + timeout_seconds
    passes = []
    aggregate = {'rebuilt':False, 'added':0, 'removed':0, 'count':0}
    for number in range(max_passes):
        if time.monotonic() >= deadline:
            raise RuntimeError('Benchmark ANN publication exceeded its deadline')
        started = time.perf_counter()
        vectors.sync(cancelled=lambda: time.monotonic() >= deadline)
        status = vectors.status()
        summary = dict(vectors.last_sync)
        aggregate['rebuilt'] = aggregate['rebuilt'] or summary['rebuilt']
        aggregate['added'] += summary['added']
        aggregate['removed'] += summary['removed']
        aggregate['count'] = summary['count']
        passes.append({'pass':number+1, 'seconds':round(time.perf_counter()-started,3),
                       'sync':summary, 'pending':status['pending'], 'segments':status.get('segments')})
        print(f"ANN pass {number+1}: {summary['count']}/{expected_count} published; pending={status['pending']}", file=sys.stderr, flush=True)
        if not status['pending']:
            if summary['count'] != expected_count:
                raise RuntimeError(f"Incomplete ANN benchmark: expected {expected_count}, published {summary['count']}")
            return aggregate, passes
    raise RuntimeError('Benchmark ANN publication exceeded its bounded pass limit')


def run(args):
    directory = args.output.expanduser().resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError('Benchmark output must be new or empty; existing indexes are not overwritten.')
    directory.mkdir(parents=True, exist_ok=True)
    config = defaults(str(directory), [])
    config['resource']['memory_mb'] = args.memory_mb
    config['resource']['max_disk_mb'] = args.disk_mb
    budget = Budget(config)
    store = Store(str(directory))
    vectors = Vectors(store, budget)
    generator = np.random.default_rng(args.seed)
    query_generator = np.random.default_rng(args.seed + 1)
    report = {
        'schema_version': 2,
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'local SQLite filename/FTS query kernels and random-vector ANN; NOT end-to-end semantic search',
        'synthetic_vectors': True,
        'embedding_inference_included': False,
        'physical_source_files_created': False,
        'document_parsing_included': False,
        'seed': args.seed,
        'documents': args.documents,
        'chunks': args.documents,
        'vector_dimensions': 512,
        'vector_storage': 'SQLite float16 source vectors; USearch cosine float16 ANN',
        'environment': {
            'os': platform.platform(), 'python': platform.python_version(),
            'processor': platform.processor(), 'logical_cpus': psutil.cpu_count(),
            'physical_cpus': psutil.cpu_count(logical=False),
            'installed_memory_mb': round(psutil.virtual_memory().total / 1048576, 2),
            'sqlite': sqlite3.sqlite_version,
            'numpy': np.__version__, 'usearch': importlib.metadata.version('usearch'),
            'storage_type': 'not automatically verified; record SSD/HDD separately',
        },
        'limits': {'memory_mb': args.memory_mb, 'disk_mb': args.disk_mb},
        'ann_configuration': {key:config['semantic'][key] for key in
                              ('segment_size','max_segments_per_sync','compact_deleted_ratio')},
        'ann_worker_controls': {key:config['resource'][key] for key in
                                ('worker_memory_mb','worker_cpu_percent')},
        'timing_boundary': {
            'filename': 'Fixed paths trigram/LIKE SQL kernel, doc metadata, max 60 candidates; excludes Engine adaptive expansion/ordering, filesystem checks and MCP response.',
            'keyword': 'Fixed FTS5/bm25 candidate SQL with tokenization, max 100 candidates; excludes Engine adaptive expansion, evidence formatting and coverage calculation.',
            'ann': 'Vectors.search with metadata/generation validation and up to 100 candidates; excludes tokenization, embedding inference and source fetch.',
        },
        'limitations': [
            'This synthetic corpus is small in source-text bytes even with 100,000 rows; it does not represent hundreds of GB of extracted text.',
            'Warm timings do not include cold process/model startup, initial filesystem reads, document parsing, network or answer generation.',
            'Random vectors measure index mechanics and latency, never semantic relevance or embedding model quality.',
            'RSS samples include this process and its ANN worker; full daemon plus parser/model workloads are separate.',
            'Fixed query mix covers exact identifiers, common topics and no-match terms; different distributions can change latency.',
            'Initial and incremental ANN times include every bounded pass until pending is false and the published count is verified.',
            'The v0.3 schema adds short Chinese path postings and durable embedding queues; build/disk comparisons include these schema changes.',
            'Host load is not controlled between historical runs; single-run timing differences are observations, not isolated causal speedup measurements.',
        ],
    }
    try:
        store.set_setting('benchmark_synthetic_vectors', 'true; never use as a real semantic index')
        with PeakRSS() as build_peak:
            started = time.perf_counter()
            report['source_text_bytes'] = populate(store, 1, args.documents, generator)
            store.set_setting('vector_generation', '1')
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            report['text_and_vector_source_build_seconds'] = round(time.perf_counter() - started, 3)
            started = time.perf_counter()
            summary, passes = sync_until_ready(vectors, args.documents)
            report['initial_ann_build_seconds'] = round(time.perf_counter() - started, 3)
            report['initial_ann_sync'] = summary
            report['initial_ann_passes'] = passes
            initial_manifest = json.loads(vectors.meta.read_text(encoding='utf-8'))
            initial_segments = {segment['snapshot'] for segment in initial_manifest.get('segments',[])}
        report['build_peak_rss_mb'] = round(build_peak.peak / 1048576, 2)
        report['initial_disk_mb'] = round(disk_bytes(directory) / 1048576, 2)
        sample_ids = query_generator.integers(1, args.documents + 1, size=args.queries)
        filename_values, keyword_values, ann_values = [], [], []
        for index, number in enumerate(sample_ids):
            number = int(number)
            slug, title, _ = TOPICS[number % len(TOPICS)]
            filename_values.append(f'document-{number:06d}' if index % 4 < 2 else slug if index % 4 == 2 else 'not-a-real-source')
            keyword_values.append(f'REF-BENCH-{number:06d}' if index % 4 < 2 else title if index % 4 == 2 else 'NOANSWERXYZ')
            row = store.rows('SELECT e.vector FROM embeddings e JOIN chunks c ON c.hash=e.hash WHERE c.id=?', (number,))[0]
            query_vector = unpack_vector(row['vector']).astype(np.float32)
            query_vector += query_generator.standard_normal(512, dtype=np.float32) * .002
            query_vector /= np.linalg.norm(query_vector)
            ann_values.append(query_vector)

        def filename_query(query):
            if '%' in query or '_' in query:
                literal = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                return store.rows("SELECT d.* FROM paths_fts p JOIN documents d ON d.id=p.rowid WHERE p.path LIKE ? ESCAPE '\\' LIMIT ?", ('%' + literal + '%', 60))
            return store.rows('SELECT d.* FROM paths_fts p JOIN documents d ON d.id=p.rowid WHERE p.path LIKE ? LIMIT ?', ('%' + query + '%', 60))

        def keyword_query(query):
            return store.rows('SELECT c.id chunk_id,bm25(chunks_fts) rank FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.id=c.doc_id WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?', (query_terms(query), 100))

        with PeakRSS() as query_peak:
            report['warm_filename'] = timed_queries(filename_query, filename_values, args.warmup)
            report['warm_keyword'] = timed_queries(keyword_query, keyword_values, args.warmup)
            report['warm_ann_synthetic_only'] = timed_queries(lambda vector: vectors.search(vector, 100), ann_values, args.warmup)
        report['query_peak_rss_mb'] = round(query_peak.peak / 1048576, 2)

        changes = min(args.update_count, args.documents)
        if changes:
            vectors.close()
            with PeakRSS() as update_peak:
                started = time.perf_counter()
                store.remove(list(range(1, changes + 1)))
                populate(store, args.documents + 1, changes, generator)
                store.set_setting('vector_generation', '2')
                store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                report['incremental_source_update_seconds'] = round(time.perf_counter() - started, 3)
                started = time.perf_counter()
                summary, passes = sync_until_ready(vectors, args.documents)
                report['incremental_ann_sync_seconds'] = round(time.perf_counter() - started, 3)
                report['incremental_ann_sync'] = summary
                report['incremental_ann_passes'] = passes
                final_manifest = json.loads(vectors.meta.read_text(encoding='utf-8'))
                final_segments = {segment['snapshot'] for segment in final_manifest.get('segments',[])}
                report['incremental_segment_reuse'] = {'initial':len(initial_segments),
                    'unchanged_reused':len(initial_segments & final_segments), 'new':len(final_segments-initial_segments)}
            report['incremental_peak_rss_mb'] = round(update_peak.peak / 1048576, 2)
        report['final_disk_mb'] = round(disk_bytes(directory) / 1048576, 2)
        report['disk_files'] = {path.name: path.stat().st_size for path in directory.iterdir() if path.is_file()}
        report['peak_rss_mb'] = max(report['build_peak_rss_mb'], report['query_peak_rss_mb'], report.get('incremental_peak_rss_mb', 0))
        report_path = args.report.expanduser().resolve() if args.report else directory / 'benchmark.json'
        atomic_json(report_path, report)
        print(json.dumps({
            'report': str(report_path), 'documents': args.documents,
            'filename_p95_ms': report['warm_filename']['p95_ms'],
            'keyword_p95_ms': report['warm_keyword']['p95_ms'],
            'ann_synthetic_p95_ms': report['warm_ann_synthetic_only']['p95_ms'],
            'peak_rss_mb': report['peak_rss_mb'],
            'disk_mb': report['final_disk_mb'],
            'incremental_ann_sync_seconds': report.get('incremental_ann_sync_seconds'),
        }, indent=2))
        return report
    finally:
        vectors.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--documents', type=int, default=100_000)
    parser.add_argument('--queries', type=int, default=200)
    parser.add_argument('--warmup', type=int, default=20)
    parser.add_argument('--seed', type=int, default=20260925)
    parser.add_argument('--update-count', type=int, default=1000)
    parser.add_argument('--memory-mb', type=int, default=1024)
    parser.add_argument('--disk-mb', type=int, default=10240)
    args = parser.parse_args()
    if args.documents < 1 or args.queries < 1 or args.warmup < 0 or args.update_count < 0:
        parser.error('documents/queries must be positive and warmup/update-count nonnegative')
    try:
        run(args)
    except ValueError as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
