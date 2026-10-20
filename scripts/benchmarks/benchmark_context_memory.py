"""Run the reproducible controlled long-context benchmark for Context/Memory v2."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evoagent.evaluation.compression_eval import CompressionCase, CompressionEvaluation
from evoagent.memory.context_manager import ContextManager
from evoagent.memory.memory import MemoryManager
from evoagent.memory.memory_governance import MemoryStatus
from evoagent.storage.store import TaskStore


def load_cases(dataset: Path, case_count: int, bundle_size: int):
    records = [
        json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    risky = [record for record in records if record.get("expected_findings")]
    clean = [record for record in records if not record.get("expected_findings")]
    cases = []
    for index, target in enumerate(risky[:case_count]):
        companions = []
        pool = clean or records
        for offset in range(bundle_size - 1):
            companions.append(pool[(index * (bundle_size - 1) + offset) % len(pool)])
        diff = "\n".join([target["diff"], *[item["diff"] for item in companions]])
        finding = target["expected_findings"][0]
        evidence_id = "ev-benchmark-%03d" % index
        artifact_ref = "artifact://benchmark-%03d" % index
        evidence = {
            "evidence_id": evidence_id,
            "artifact_ref": artifact_ref,
            "artifact_sha256": "sha256-%03d" % index,
            "path": finding["path"],
            "added_lines": [finding["start_line"]],
            "excerpt_hash": "excerpt-%03d" % index,
            "tool": "changed_line", "created_by": "security",
        }
        observations = [
            {
                "step": step, "tool": "changed_line", "ok": True,
                "result": {
                    "evidence_id": evidence_id, "evidence_record": evidence,
                    "output": {
                        "path": finding["path"], "line": finding["start_line"],
                        "content": ("controlled tool trace %d " % step) * 900,
                    },
                },
            }
            for step in range(1, 5)
        ]
        cases.append(CompressionCase(
            name=str(target["id"]), diff=diff,
            required_paths=[finding["path"]],
            risk_domains=[str(finding.get("cwe", "")), str(finding.get("rule_id", ""))],
            observations=observations,
        ))
    return cases


def benchmark_memory_retrieval(dataset: Path, query_count: int = 20, repeats: int = 5):
    records = [
        json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("expected_findings")
    ]
    handle, path = tempfile.mkstemp(suffix=".db")
    __import__("os").close(handle)
    try:
        memory = MemoryManager(TaskStore(path), recall_limit=3)
        targets = []
        for index, record in enumerate(records[:40]):
            finding = record["expected_findings"][0]
            stored = memory.remember(
                "tenant-a", record["repository"], "semantic", "confirmed_rule",
                "Confirmed %s %s risk at %s with added-line evidence" % (
                    finding["rule_id"], finding.get("cwe", ""), finding["path"],
                ),
                importance=0.9, status=MemoryStatus.VERIFIED.value,
                source_evidence=["ev-%03d" % index],
            )
            if index < query_count:
                targets.append((record, stored["id"]))
        # Exact lexical decoys must be excluded by lifecycle and tenant scope.
        first = targets[0][0]
        finding = first["expected_findings"][0]
        memory.remember(
            "tenant-a", first["repository"], "semantic", "rejected_rule",
            "Exact %s %s %s" % (finding["rule_id"], finding.get("cwe", ""), finding["path"]),
            status=MemoryStatus.REJECTED.value, importance=1.0,
        )
        memory.remember(
            "tenant-b", first["repository"], "semantic", "cross_tenant_secret",
            "Exact %s %s %s" % (finding["rule_id"], finding.get("cwe", ""), finding["path"]),
            status=MemoryStatus.PROMOTED.value, importance=1.0,
        )
        hits, reciprocal, timings, leaks = 0, 0.0, [], 0
        for _ in range(repeats):
            for record, expected_id in targets:
                finding = record["expected_findings"][0]
                query = "%s %s %s" % (
                    finding["rule_id"], finding.get("cwe", ""), finding["path"],
                )
                started = time.perf_counter()
                recalled = memory.recall("tenant-a", record["repository"], query, limit=3)
                timings.append((time.perf_counter() - started) * 1000.0)
                ids = [item["id"] for item in recalled]
                if ids and ids[0] == expected_id:
                    hits += 1
                if expected_id in ids:
                    reciprocal += 1.0 / (ids.index(expected_id) + 1)
                leaks += sum(item.get("tenant_id") != "tenant-a" for item in recalled)
                leaks += sum(item.get("status") in {"REJECTED", "SUPERSEDED", "EXPIRED"} for item in recalled)
        timings.sort()
        total = len(targets) * repeats
        return {
            "queries": total,
            "top1_accuracy": round(hits / max(1, total), 4),
            "mrr_at_3": round(reciprocal / max(1, total), 4),
            "scope_or_terminal_leaks": int(leaks),
            "latency_mean_ms": round(mean(timings), 3),
            "latency_p95_ms": round(timings[min(len(timings) - 1, int(len(timings) * 0.95))], 3),
            "gates": {
                "top1_at_least_95_percent": hits / max(1, total) >= 0.95,
                "mrr_at_3_at_least_95_percent": reciprocal / max(1, total) >= 0.95,
                "no_scope_or_terminal_leak": leaks == 0,
            },
        }
    finally:
        __import__("os").unlink(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(ROOT / "data/benchmarks/pr_diff_100.jsonl"))
    parser.add_argument("--output", default=str(ROOT / "docs" / "benchmarks" / "context_memory_benchmark.json"))
    parser.add_argument("--cases", type=int, default=20)
    parser.add_argument("--bundle-size", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    manager = ContextManager(
        context_window_tokens=8192, input_token_budget=5000,
        diff_token_budget=1800, observation_token_budget=700,
        recent_observations=1, map_chunk_tokens=500,
    )
    report = CompressionEvaluation(manager).run(
        load_cases(Path(args.dataset), args.cases, args.bundle_size),
        repeats=args.repeats,
    )
    report["memory_retrieval"] = benchmark_memory_retrieval(
        Path(args.dataset), min(args.cases, 20), args.repeats,
    )
    report["memory_retrieval"]["passed"] = all(
        report["memory_retrieval"]["gates"].values()
    )
    report["passed"] = bool(report["passed"] and report["memory_retrieval"]["passed"])
    report["benchmark"] = {
        "kind": "controlled-long-context-stress",
        "dataset": Path(args.dataset).name,
        "dataset_sha256": __import__("hashlib").sha256(
            Path(args.dataset).read_bytes()
        ).hexdigest(),
        "case_count": args.cases,
        "bundle_size": args.bundle_size,
        "repeats": args.repeats,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "note": "Controlled benchmark; it is not a production PR quality claim.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "token_reduction_ratio": report["token_reduction_ratio"],
        "required_path_recall": report["required_path_recall"],
        "evidence_reference_recall": report["evidence_reference_recall"],
        "compact_latency_mean_ms": report["compact_latency_mean_ms"],
        "compact_latency_max_ms": report["compact_latency_max_ms"],
        "memory_top1_accuracy": report["memory_retrieval"]["top1_accuracy"],
        "memory_mrr_at_3": report["memory_retrieval"]["mrr_at_3"],
        "memory_latency_p95_ms": report["memory_retrieval"]["latency_p95_ms"],
        "memory_scope_or_terminal_leaks": report["memory_retrieval"]["scope_or_terminal_leaks"],
        "output": str(output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
