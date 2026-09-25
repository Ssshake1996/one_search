"""Measure first and unchanged catalog scans using ONLY freshly generated files.

Can run under v0.2 or v0.3 via PYTHONPATH for the same workload. No supplied user
directory is scanned. File creation is excluded from timings; content and semantic
indexing are disabled. It is not a hundreds-GB document extraction benchmark.
"""
from __future__ import annotations

import argparse
import json
import platform
import threading
import time
from pathlib import Path

import psutil

from data_search import __version__
from data_search.config import defaults
from data_search.engine import Engine


def run(args):
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new/empty benchmark output directory')
    root = output/'synthetic-files'
    root.mkdir(parents=True)
    for index in range(args.files):
        directory = root/f'{index//100:04d}'
        directory.mkdir(exist_ok=True)
        (directory/f'目录基准-{index:06d}.txt').write_text('Synthetic catalog fixture.\n',encoding='utf-8')
    config = defaults(str(output/'index'),[str(root)])
    config['semantic']['enabled'] = False
    config['indexing'].update(content_scope='none',semantic_scope='none')
    config['resource']['batch_sleep_ms'] = 0
    config['resource']['min_available_mb'] = 0
    config['resource']['min_free_disk_mb'] = 0
    engine = Engine(config)
    process = psutil.Process()
    report = {'version':__version__,'physical_files':args.files,'synthetic_only':True,
        'source_bytes':args.files*len('Synthetic catalog fixture.\n'.encode()),'content_indexing':False,'embedding_inference':False,
        'scope':'Engine first/unchanged filename catalog and metadata-only processing; no MCP',
        'environment':{'platform':platform.platform(),'python':platform.python_version(),
            'physical_memory_mb':round(psutil.virtual_memory().total/1048576,2),
            'logical_cpus':psutil.cpu_count()},'runs':[]}
    try:
        for phase in ('first','unchanged'):
            transactions = [0]
            engine.store.db.set_trace_callback(lambda sql: transactions.__setitem__(0,transactions[0]+1) if sql=='COMMIT' else None)
            before = process.io_counters()
            cpu_before = process.cpu_times()
            peak = [process.memory_info().rss]
            stop = threading.Event()
            def sample():
                while not stop.wait(.05):
                    peak[0] = max(peak[0],process.memory_info().rss)
            sampler=threading.Thread(target=sample,daemon=True)
            sampler.start()
            start=time.perf_counter()
            ticks=0
            try:
                while True:
                    status=engine.scan_once(full=ticks==0)
                    ticks+=1
                    if status.get('last_error'):
                        raise RuntimeError(status['last_error'])
                    if not hasattr(engine,'_has_work') or not engine._has_work():
                        break
                    if ticks>100000 or time.perf_counter()-start>3600:
                        raise TimeoutError('catalog benchmark exceeded its limit')
            finally:
                seconds=time.perf_counter()-start
                stop.set()
                sampler.join()
                engine.store.db.set_trace_callback(None)
            after=process.io_counters()
            cpu_after=process.cpu_times()
            count=engine.store.rows("SELECT count(*) n FROM documents WHERE source_id='files'")[0]['n']
            assert count==args.files,(count,args.files)
            report['runs'].append({'phase':phase,'seconds':round(seconds,4),'ticks':ticks,
                'catalog_count':count,'commits':transactions[0],'peak_rss_mb':round(peak[0]/1048576,2),
                'cpu_seconds':round(cpu_after.user+cpu_after.system-cpu_before.user-cpu_before.system,4),
                'process_read_bytes':after.read_bytes-before.read_bytes,
                'process_write_bytes':after.write_bytes-before.write_bytes})
    finally:
        engine.close()
    report['limitations']=['Windows process I/O counters include cached I/O; they are not physical drive traffic.',
        'SQLite trace counting is enabled for both runs and adds instrumentation cost.',
        'No model/parser or real user files. Physical 8GB/16GB machines are not simulated.',
        'Scheduling ticks are driven immediately for measurement, excluding production idle intervals.']
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('version','physical_files','runs')},ensure_ascii=True,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--files',type=int,default=5000)
    args=parser.parse_args()
    if not 1<=args.files<=1000000:
        parser.error('--files must be 1..1000000')
    run(args)
