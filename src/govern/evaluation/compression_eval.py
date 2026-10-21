"""Paired Full-vs-Compact evaluation for context cost and evidence fidelity."""
from dataclasses import dataclass, field
import json
from statistics import mean, median
import time
from typing import Any, Dict, Iterable, List, Sequence

from govern.memory.context_manager import ContextManager, estimate_tokens


@dataclass(frozen=True)
class CompressionCase:
    name: str
    diff: str
    required_paths: Sequence[str] = field(default_factory=tuple)
    risk_domains: Sequence[str] = field(default_factory=tuple)
    observations: Sequence[Dict[str, Any]] = field(default_factory=tuple)


class CompressionEvaluation:
    """Measure reduction, latency and loss of required paths/evidence references."""

    def __init__(self, manager: ContextManager):
        self.manager = manager

    def run(self, cases: Iterable[CompressionCase], repeats: int = 3) -> Dict[str, Any]:
        results = [self._run_case(case, repeats) for case in cases]
        if not results:
            raise ValueError("compression evaluation requires at least one case")
        full = sum(item["full_estimated_tokens"] for item in results)
        compact = sum(item["compact_estimated_tokens"] for item in results)
        latencies = [item["compact_latency_ms"] for item in results]
        report = {
            "schema_version": 1,
            "cases": len(results),
            "full_estimated_tokens": full,
            "compact_estimated_tokens": compact,
            "token_reduction_ratio": round(1.0 - compact / max(1, full), 4),
            "required_path_recall": round(mean(item["required_path_recall"] for item in results), 4),
            "evidence_reference_recall": round(mean(item["evidence_reference_recall"] for item in results), 4),
            "compact_latency_mean_ms": round(mean(latencies), 3),
            "compact_latency_p50_ms": round(median(latencies), 3),
            "compact_latency_max_ms": round(max(latencies), 3),
            "results": results,
        }
        report["gates"] = {
            "token_reduction_at_least_30_percent": report["token_reduction_ratio"] >= 0.30,
            "required_path_recall_100_percent": report["required_path_recall"] == 1.0,
            "evidence_reference_recall_100_percent": report["evidence_reference_recall"] == 1.0,
        }
        report["passed"] = all(report["gates"].values())
        return report

    def _run_case(self, case: CompressionCase, repeats: int) -> Dict[str, Any]:
        timings: List[float] = []
        payload = None
        compact_observations = None
        for _ in range(max(1, int(repeats))):
            started = time.perf_counter()
            payload = self.manager.compress_diff(
                case.diff, label="eval:%s" % case.name,
                focus_files=case.required_paths, risk_domains=case.risk_domains,
            )
            compact_observations, _stats = self.manager.compact_observations(case.observations)
            timings.append((time.perf_counter() - started) * 1000.0)
        selected_paths = {
            str(item.get("path", "")) for item in payload.get("selected_hunks") or []
        }
        required = {str(path) for path in case.required_paths}
        path_recall = len(required.intersection(selected_paths)) / max(1, len(required)) if required else 1.0
        before_refs = self.manager._collect_references(list(case.observations))
        after_refs = self.manager._collect_references(compact_observations)
        before_ids = {
            item.get("evidence_id") for item in before_refs["evidence_refs"] if item.get("evidence_id")
        }
        before_ids.update(
            item.get("artifact_ref") for item in before_refs["artifact_refs"] if item.get("artifact_ref")
        )
        after_ids = {
            item.get("evidence_id") for item in after_refs["evidence_refs"] if item.get("evidence_id")
        }
        after_ids.update(
            item.get("artifact_ref") for item in after_refs["artifact_refs"] if item.get("artifact_ref")
        )
        evidence_recall = len(before_ids.intersection(after_ids)) / max(1, len(before_ids)) if before_ids else 1.0
        full_tokens = estimate_tokens({"diff": case.diff, "observations": list(case.observations)})
        compact_tokens = estimate_tokens({
            "diff": self.manager.render_diff_view(payload),
            "diff_context": self.manager.diff_metadata(payload),
            "observations": compact_observations,
        })
        return {
            "name": case.name,
            "full_estimated_tokens": full_tokens,
            "compact_estimated_tokens": compact_tokens,
            "token_reduction_ratio": round(1.0 - compact_tokens / max(1, full_tokens), 4),
            "required_path_recall": round(path_recall, 4),
            "evidence_reference_recall": round(evidence_recall, 4),
            "compact_latency_ms": round(mean(timings), 3),
            "selected_paths": sorted(selected_paths),
            "source_sha256": payload.get("source_sha256"),
        }


def render_markdown(report: Dict[str, Any]) -> str:
    """Render a stable benchmark table suitable for checked-in documentation."""
    rows = [
        "| 指标 | 结果 | 门禁 |",
        "|---|---:|---:|",
        "| 输入 Token 降幅 | %.2f%% | >= 30%% |" % (report["token_reduction_ratio"] * 100),
        "| 必需高风险路径召回 | %.2f%% | 100%% |" % (report["required_path_recall"] * 100),
        "| Evidence/Artifact 引用保留 | %.2f%% | 100%% |" % (report["evidence_reference_recall"] * 100),
        "| Compact 平均耗时 | %.3f ms | 记录值 |" % report["compact_latency_mean_ms"],
        "| Compact 最大耗时 | %.3f ms | 记录值 |" % report["compact_latency_max_ms"],
    ]
    return "\n".join(rows)
