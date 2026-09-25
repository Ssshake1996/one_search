"""Measure only the real daemon process tree on a small synthetic local corpus.

Run with the project's Python environment. No downloads or business files are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import tempfile
import threading
import time

import psutil

from create_demo import create_demo
from data_search.config import defaults, load_config
from data_search.model import MODEL_ID, model_ready
from data_search.service import rpc, start_service, stop_service


class ProcessTreeSampler:
    def __init__(self, pid: int, interval: float = 0.1):
        self.pid, self.interval = pid, interval
        self.root = psutil.Process(pid)
        self.root_created = self.root.create_time()
        self.phase = "indexing"
        self.samples = []
        self.previous_cpu = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(3)

    def _sample(self):
        previous_time = time.perf_counter()
        first = True
        while not self.stop_event.is_set():
            moment = time.perf_counter()
            duration = moment - previous_time
            previous_time = moment
            rss, cpu_delta, tree = 0, 0.0, []
            try:
                if self.root.create_time() != self.root_created:
                    return
                processes = [self.root, *self.root.children(recursive=True)]
            except psutil.Error:
                return
            for process in processes:
                try:
                    identity = (process.pid, process.create_time())
                    cpu = process.cpu_times()
                    total_cpu = cpu.user + cpu.system
                    memory = process.memory_info().rss
                    # Existing processes establish a baseline. A worker born later
                    # contributes its CPU since birth when first observed.
                    baseline = self.previous_cpu.get(identity, total_cpu if first else 0.0)
                    cpu_delta += max(0.0, total_cpu - baseline)
                    self.previous_cpu[identity] = total_cpu
                    rss += memory
                    tree.append({"pid": process.pid, "role": "daemon" if process.pid == self.pid else "worker",
                                 "rss_mib": round(memory / 1048576, 3)})
                except psutil.Error:
                    continue
            if not first and duration > 0:
                self.samples.append({"phase": self.phase, "duration_seconds": duration,
                    "rss_mib": rss / 1048576, "cpu_seconds": cpu_delta,
                    "cpu_single_core_percent": cpu_delta / duration * 100,
                    "process_count": len(tree), "tree": tree})
            first = False
            self.stop_event.wait(self.interval)

    def summary(self, logical_cpus):
        result = {}
        for phase in dict.fromkeys(sample["phase"] for sample in self.samples):
            values = [sample for sample in self.samples if sample["phase"] == phase]
            duration = sum(value["duration_seconds"] for value in values)
            total_cpu = sum(value["cpu_seconds"] for value in values)
            peak = max(values, key=lambda value: value["rss_mib"])
            mean_cpu = total_cpu / duration * 100
            result[phase] = {
                "samples": len(values), "measured_seconds": round(duration, 3),
                "rss_mean_mib": round(statistics.mean(value["rss_mib"] for value in values), 3),
                "rss_peak_mib": round(peak["rss_mib"], 3),
                "rss_min_mib": round(min(value["rss_mib"] for value in values), 3),
                "cpu_seconds": round(total_cpu, 4),
                "cpu_single_core_percent_mean": round(mean_cpu, 3),
                "cpu_whole_machine_percent_mean": round(mean_cpu / logical_cpus, 3),
                "cpu_single_core_percent_sample_peak": round(max(value["cpu_single_core_percent"] for value in values), 3),
                "cpu_whole_machine_percent_sample_peak": round(max(value["cpu_single_core_percent"] for value in values) / logical_cpus, 3),
                "max_processes": max(value["process_count"] for value in values),
                "peak_memory_process_tree": peak["tree"],
            }
        return result


def wait_for(config, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = rpc(config, "index_status")
        if predicate(status):
            return status
        time.sleep(.2)
    raise RuntimeError("Timed out waiting for " + description)


def measure(model_directory: Path, output: Path, work_parent: Path):
    if not model_ready(str(model_directory)):
        raise ValueError("An existing downloaded model is required; this script does not download models")
    work_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="resources-", dir=work_parent))
    demo = create_demo(workspace / "synthetic")
    assert demo["source_count"] == 16
    config_path = workspace / "config.json"
    config = defaults(str(workspace / "data"), [demo["file_root"]])
    config["semantic"].update(model_dir=str(model_directory.resolve()), idle_seconds=2, threads=1, batch_size=8)
    config["resource"]["memory_mb"] = 1024
    config["databases"] = [json.loads(Path(demo["database_config"]).read_text(encoding="utf-8"))]
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    config = load_config(config_path)
    health = start_service(config)
    sampler = ProcessTreeSampler(health["pid"])
    sampler.start()
    report = {"schema_version": 1, "measured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "single local daemon PID and its descendant workers only; measuring process, pytest, MCP bridge, DSH and other applications excluded",
        "hardware": {"os": platform.platform(), "python": platform.python_version(),
                     "cpu": platform.processor(), "logical_cpus": psutil.cpu_count(), "physical_cpus": psutil.cpu_count(logical=False),
                     "physical_memory_gib": round(psutil.virtual_memory().total / 1073741824, 3)},
        "corpus": {"synthetic": True, "files": demo["source_count"],
                   "file_bytes": sum(item["bytes"] for item in demo["files"]), "sqlite_rows": 8, "sqlite_tables": 4},
        "configuration": {"memory_budget_mib": 1024, "model_id": MODEL_ID, "model_threads": 1,
                          "embedding_batch_size": 8, "test_model_idle_seconds": 2, "product_default_model_idle_seconds": 120,
                          "scan_interval_seconds": config["scan_interval_seconds"], "sampling_interval_seconds": sampler.interval,
                          "idle_unloaded_observation_seconds": 10},
        "limitations": [
            "This small synthetic corpus is not a 100,000-file or hundreds-of-GB scale benchmark.",
            "A 1024 MiB configured budget on this physical machine does not validate operation on an 8 GiB physical PC.",
            "RSS is summed per process and may count shared pages more than once; it is not unique physical memory or system file-cache usage.",
            "Samples start when the daemon is ready and exclude pre-ready interpreter startup; 100 ms sampling can miss shorter memory peaks.",
            "CPU is sampled from daemon/worker process times; short-lived workers may exit between samples. Small interval peaks are noisy.",
            "A zero CPU delta means no increment was visible at the operating system's CPU-time resolution during this observation; it is not a guarantee of literally zero CPU work.",
            "Single-core CPU 100% means one fully occupied logical core; whole-machine percent divides by the recorded logical CPU count.",
            "Indexing and unload-wait phases include status polling every 200 ms. Idle-unloaded observations make no RPC calls for ten seconds.",
            "The 2-second idle setting shortens this test; the default keeps the model for 120 seconds, so default post-query memory stays higher for longer.",
            "No MCP bridge is kept running in this measurement; MCP/DSH client memory is additional.",
        ], "workspace": str(workspace), "daemon_pid": health["pid"]}
    failure = None
    try:
        indexed = wait_for(config, lambda status: status["coverage"]["last_scan"] and not status["coverage"]["scanning"]
                           and not status['scheduler']['discovery_active'] and not status['scheduler']['queued_files']
                           and status['coverage']['chunks']==status['coverage']['embedded_chunks']
                           and not status['vector_index']['pending'],
                           120, "initial indexing")
        if indexed["last_error"] or indexed["coverage"]["source_errors"]:
            raise RuntimeError("Initial indexing reported errors: " + json.dumps(indexed, ensure_ascii=False))
        if indexed["coverage"]["chunks"] != indexed["coverage"]["embedded_chunks"]:
            raise RuntimeError("Not all synthetic chunks have semantic embeddings")
        report["coverage_after_indexing"] = indexed["coverage"]
        report["storage_after_indexing_mib"] = indexed["resources"]["disk_mb"]
        # The indexer just used the model, but a first query makes the warm-state
        # requirement explicit if a slow machine already unloaded it.
        sampler.phase = "warmup"
        warmup = rpc(config, "search", {"query": "怎样减少测试服务器的闲置费用", "mode": "semantic", "limit": 5})
        if warmup.get("warnings"):
            raise RuntimeError("Semantic warmup reported warnings: " + json.dumps(warmup["warnings"]))
        assert rpc(config, "index_status")["semantic"]["model_loaded"]
        queries = ["如何减少云服务器的开销", "买到破损的杯子怎么办", "索引占用内存过高怎么处理", "新员工第一天需要做什么"]
        sampler.phase = "warm_queries"
        timings = []
        for _ in range(5):
            for query in queries:
                started = time.perf_counter()
                response = rpc(config, "search", {"query": query, "mode": "semantic", "limit": 5})
                timings.append((time.perf_counter() - started) * 1000)
                if response.get("warnings"):
                    raise RuntimeError("Warm query returned semantic warnings")
                time.sleep(.1)
        report["warm_query_latency_ms"] = {"count": len(timings), "median": round(statistics.median(timings), 3),
            "p95": round(sorted(timings)[max(0, int(len(timings) * .95) - 1)], 3), "max": round(max(timings), 3),
            "includes_loopback_rpc": True, "excludes_sleep_between_queries": True}
        sampler.phase = "waiting_for_model_unload"
        wait_for(config, lambda status: not status["semantic"]["model_loaded"], 30, "idle model release")
        sampler.phase = "idle_unloaded"
        # This observation deliberately issues no status queries.
        time.sleep(10)
        sampler.phase = "final_status"
        final_status = rpc(config, "index_status")
        report["model_still_unloaded_after_observation"] = not final_status["semantic"]["model_loaded"]
        report["final_resources_reported_by_daemon"] = final_status["resources"]
        report["completed"] = True
    except Exception as error:
        report["completed"] = False
        report["failure"] = str(error)
        failure = error
    finally:
        sampler.stop()
        logical_cpus = psutil.cpu_count() or 1
        report["phases"] = sampler.summary(logical_cpus)
        report["overall_sampled_peak_rss_mib"] = round(max((sample["rss_mib"] for sample in sampler.samples), default=0), 3)
        report["sampled_peak_within_1024_mib_budget"] = report["overall_sampled_peak_rss_mib"] <= 1024
        stop_service(config)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(health["pid"]) and time.monotonic() < deadline:
            time.sleep(.1)
        report["daemon_exited"] = not psutil.pid_exists(health["pid"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if failure:
        raise failure
    return report


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=project / ".runtime/models/bge-small-zh-v1.5")
    parser.add_argument("--output", type=Path, default=project / "docs/validation/resources.json")
    parser.add_argument("--work-parent", type=Path, default=project / ".runtime")
    args = parser.parse_args()
    report = measure(args.model_dir, args.output, args.work_parent)
    print(json.dumps({"report": str(args.output.resolve()), "completed": report["completed"],
        "peak_rss_mib": report["overall_sampled_peak_rss_mib"], "idle_unloaded": report["phases"].get("idle_unloaded"),
        "daemon_exited": report["daemon_exited"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
