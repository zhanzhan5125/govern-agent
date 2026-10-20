"""Deterministic release policy shared by Prompt and Agent Skill candidates.

Policy values are server configuration, never fields accepted from candidate JSON.
Missing replay measurements fail closed rather than being interpreted as zero cost.
"""
import math
import random
from dataclasses import asdict, dataclass


TRANSITIONS = {
    "DRAFT": {"STATIC_PASSED", "REJECTED"},
    "STATIC_PASSED": {"VALIDATION_PASSED", "REJECTED"},
    "VALIDATION_PASSED": {"HOLDOUT_PASSED", "REJECTED"},
    "HOLDOUT_PASSED": {"HUMAN_APPROVED", "REJECTED"},
    "HUMAN_APPROVED": {"SHADOW", "REJECTED"},
    "SHADOW": {"ACTIVE", "REJECTED"},
    "ACTIVE": {"SUPERSEDED", "ROLLED_BACK"},
    "SUPERSEDED": set(),
    "ROLLED_BACK": set(),
    "REJECTED": set(),
}


class CandidateConflict(ValueError):
    """A stale version, baseline or concurrent operation must be retried explicitly."""


def canonical_json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    import hashlib
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def number(mapping, key, minimum=0.0, maximum=None):
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing or nonnumeric measurement: " + key)
    value = float(value)
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("invalid measurement: " + key)
    return value


@dataclass(frozen=True)
class CandidatePolicy:
    min_improvement: float = 0.01
    max_regression: float = 0.0
    max_cost_growth: float = 0.10
    min_validation_cases: int = 3
    min_holdout_cases: int = 2
    min_shadow_cases: int = 3
    bootstrap_samples: int = 1000
    bootstrap_seed: int = 20260918
    max_candidates: int = 3

    def __post_init__(self):
        for key in ("min_improvement", "max_regression", "max_cost_growth"):
            number(asdict(self), key, 0.0, 1.0)
        for key in ("min_validation_cases", "min_holdout_cases", "min_shadow_cases", "bootstrap_samples", "max_candidates"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("policy requires a positive integer: " + key)
        if self.max_candidates > 3:
            raise ValueError("candidate pool is bounded at three")

    def snapshot(self):
        return asdict(self)

    def compare(self, baseline, candidate, phase="validation"):
        if phase not in {"validation", "holdout", "shadow"}:
            raise ValueError("unknown replay phase")
        gates, errors = {}, []
        try:
            minimum = getattr(self, "min_%s_cases" % phase)
            gates["sample_count"] = (
                number(candidate, "cases") >= minimum
                and number(candidate, "cases") == number(baseline, "cases")
            )
            for key in ("f1", "high_severity_recall", "clean_accuracy", "evidence_accuracy", "success_rate"):
                before = number(baseline, key, maximum=1.0)
                after = number(candidate, key, maximum=1.0)
                gates[key + "_non_regression"] = after + self.max_regression >= before
            gates["evaluation_success"] = number(candidate, "success_rate") == number(baseline, "success_rate") == 1.0
            if phase == "validation":
                gates["minimum_effect"] = number(candidate, "f1") >= number(baseline, "f1") + self.min_improvement
            for key in ("total_tokens", "cost_usd", "duration_ms", "tool_calls"):
                before, after = number(baseline, key), number(candidate, key)
                gates[key + "_budget"] = after <= before * (1 + self.max_cost_growth)
            for key in ("permission_violations", "budget_violations", "invalid_findings", "required_gate_misses"):
                gates[key] = number(candidate, key) == 0
            gates["telemetry_complete"] = candidate.get("telemetry_complete") is True and baseline.get("telemetry_complete") is True
        except ValueError as exc:
            errors.append(str(exc))
        return {"passed": not errors and bool(gates) and all(gates.values()), "gates": gates, "errors": errors}

    def paired_interval(self, baseline_scores, candidate_scores):
        """Paired bootstrap of per-case utility, not an unpaired F1 interval."""
        if len(baseline_scores) != len(candidate_scores) or len(baseline_scores) < 2:
            raise ValueError("paired bootstrap requires at least two aligned cases")
        deltas = []
        for before, after in zip(baseline_scores, candidate_scores):
            deltas.append(number({"v": after}, "v", 0, 1) - number({"v": before}, "v", 0, 1))
        rng = random.Random(self.bootstrap_seed)
        samples = sorted(
            sum(rng.choice(deltas) for _ in deltas) / len(deltas)
            for _ in range(self.bootstrap_samples)
        )
        low = samples[int((len(samples) - 1) * .025)]
        high = samples[int((len(samples) - 1) * .975)]
        return {"metric": "mean_paired_case_utility_delta", "lower": low, "upper": high,
                "mean": sum(deltas) / len(deltas), "seed": self.bootstrap_seed,
                "samples": self.bootstrap_samples, "passed": low >= -self.max_regression}

    def pareto(self, candidates):
        """Return at most three nondominated, passing candidates in stable order."""
        eligible = [item for item in candidates if item.get("passed") is True]
        def values(item):
            metrics = item["metrics"]
            return tuple(number(metrics, k, maximum=1) for k in ("f1", "high_severity_recall", "clean_accuracy")) + tuple(
                -number(metrics, k) for k in ("total_tokens", "cost_usd", "duration_ms", "tool_calls")
            )
        scored = [(item, values(item)) for item in eligible]
        frontier = [item for item, score in scored if not any(
            all(a >= b for a, b in zip(other, score)) and any(a > b for a, b in zip(other, score))
            for _, other in scored
        )]
        return sorted(frontier, key=lambda item: (-item["metrics"]["f1"], item["metrics"]["total_tokens"], str(item["id"])))[:self.max_candidates]
