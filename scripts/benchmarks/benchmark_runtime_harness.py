"""Reproducible local microbenchmark for Runtime Harness v2.

The baseline mirrors the former node/checkpoint path.  The optimized arm adds
Hook dispatch, append-only Journal events and semantic keys.  This benchmark
measures framework overhead only; it intentionally excludes LLM/network time.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.runtime.artifacts import ArtifactOffloadHook, ArtifactStore  # noqa: E402
from evoagent.runtime.hooks import HookPipeline, HookPoint, HookRegistration  # noqa: E402
from evoagent.runtime.journal import EffectExecutor  # noqa: E402
from evoagent.runtime.runtime import AgentRuntime, AgentTool, RuntimeNode, ToolRegistry  # noqa: E402
from evoagent.storage.store import TaskStore  # noqa: E402


def percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
    return ordered[index]


def summary(values):
    return {
        "runs": len(values),
        "mean_ms": round(statistics.mean(values), 4),
        "median_ms": round(statistics.median(values), 4),
        "p95_ms": round(percentile(values, .95), 4),
        "min_ms": round(min(values), 4),
        "max_ms": round(max(values), 4),
    }


def baseline_execute(store, task_id, nodes):
    state = {}
    for node in nodes:
        output = node.handler(state) or {}
        state.update(output)
        store.save_checkpoint(task_id, node.name, output, "completed", 1)
    return state


def run_benchmark(runs):
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        store = TaskStore(path)
        nodes = [
            RuntimeNode("planning", lambda _state: {"planned": True}),
            RuntimeNode("executing", lambda state: {"value": int(state["planned"]) + 1}),
            RuntimeNode("reviewing", lambda state: {"report": {"value": state["value"]}}),
        ]
        baseline_times, optimized_times = [], []
        optimized = AgentRuntime(max_steps=4, timeout_seconds=30)
        for index in range(runs):
            task_id = "baseline-%d" % index
            store.create(task_id, "bench/repo", index, {})
            started = time.perf_counter()
            baseline_execute(store, task_id, nodes)
            baseline_times.append((time.perf_counter() - started) * 1000)

        for index in range(runs):
            task_id = "optimized-%d" % index
            store.create(task_id, "bench/repo", index, {})
            started = time.perf_counter()
            optimized.execute({}, nodes, task_id, store)
            optimized_times.append((time.perf_counter() - started) * 1000)

        resume_times = []
        for index in range(runs):
            task_id = "optimized-%d" % index
            started = time.perf_counter()
            optimized.execute({}, nodes, task_id, store)
            resume_times.append((time.perf_counter() - started) * 1000)

        # Measure committed side-effect replay and prove that the handler runs once.
        effect_task = "effect-benchmark"
        store.create(effect_task, "bench/repo", None, {})
        effect_calls = []
        effects = EffectExecutor(store)
        effects.execute_once(
            effect_task, "publish", {"target": "pr"},
            lambda: effect_calls.append(1) or {"published": True},
        )
        effect_replay = []
        for _index in range(runs):
            started = time.perf_counter()
            effects.execute_once(
                effect_task, "publish", {"target": "pr"},
                lambda: effect_calls.append(1) or {"published": True},
            )
            effect_replay.append((time.perf_counter() - started) * 1000)

        # Measure a representative 128 KiB result offloaded to an artifact handle.
        artifact_task = "artifact-benchmark"
        store.create(artifact_task, "bench/repo", None, {}, "tenant-a")
        artifact_store = ArtifactStore(store)
        pipeline = HookPipeline([HookRegistration(
            "offload", ArtifactOffloadHook(artifact_store, 32_768),
            (HookPoint.TOOL_AFTER,), fail_open=True,
        )])
        registry = ToolRegistry([
            AgentTool(
                "large-read", "large read",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda: {"evidence_id": "large:1", "tool": "large-read",
                         "output": {"content": "x" * 131_072}},
            )
        ], pipeline, {"task_id": artifact_task, "tenant_id": "tenant-a"}, store)
        artifact_times = []
        compact = None
        artifact_runs = min(runs, 50)
        for _index in range(artifact_runs):
            started = time.perf_counter()
            compact = registry.invoke("large-read", {})
            artifact_times.append((time.perf_counter() - started) * 1000)
        compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode("utf-8"))
        original_bytes = 131_072

        baseline = summary(baseline_times)
        optimized_summary = summary(optimized_times)
        return {
            "schema_version": 1,
            "environment": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "database": "sqlite",
                "scope": "framework-only; excludes model and network latency",
            },
            "fresh_three_node_run": {
                "baseline_checkpoint_only": baseline,
                "optimized_hook_journal_checkpoint": optimized_summary,
                "mean_overhead_ms": round(
                    optimized_summary["mean_ms"] - baseline["mean_ms"], 4
                ),
                "mean_overhead_ms_per_node": round(
                    (optimized_summary["mean_ms"] - baseline["mean_ms"]) / 3, 4
                ),
                "p95_overhead_ms_per_node": round(
                    (optimized_summary["p95_ms"] - baseline["p95_ms"]) / 3, 4
                ),
                "relative_slowdown": round(
                    optimized_summary["mean_ms"] / baseline["mean_ms"], 4
                ) if baseline["mean_ms"] else None,
            },
            "resume_three_completed_nodes": summary(resume_times),
            "side_effect_replay": {
                **summary(effect_replay),
                "handler_calls": len(effect_calls),
                "duplicate_handler_calls": max(0, len(effect_calls) - 1),
            },
            "artifact_offload_128k": {
                **summary(artifact_times),
                "original_payload_bytes": original_bytes,
                "model_visible_bytes": compact_bytes,
                "visible_reduction_percent": round(
                    (1 - compact_bytes / original_bytes) * 100, 2
                ),
                "artifact_count": len(store.list_runtime_artifacts(artifact_task)),
            },
            "journal_sample": {
                "event_count": len(store.list_runtime_events("optimized-0")),
                "sequences_monotonic": [
                    item["sequence"] for item in store.list_runtime_events("optimized-0")
                ] == list(range(1, len(store.list_runtime_events("optimized-0")) + 1)),
            },
        }
    finally:
        os.unlink(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    args = parser.parse_args()
    if args.runs < 10:
        parser.error("--runs must be at least 10")
    print(json.dumps(run_benchmark(args.runs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
