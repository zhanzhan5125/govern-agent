"""Independent, measured replay of evolution candidates through the product runtime."""
import copy
import time

from govern.policy.candidate_policy import CandidatePolicy, fingerprint, number
from govern.core.diff_parser import parse_unified_diff
from govern.evaluation.evaluation_harness import SEVERITY_RANK, one_to_one_match, validate_case


class EvolutionDataset:
    """An evaluator-owned snapshot. Proposers receive neither cases nor case results."""

    def __init__(self, cases, shadow_cases=()):
        self._cases = copy.deepcopy(list(cases))
        self._shadow = copy.deepcopy(list(shadow_cases))
        seen_ids, repositories, hashes = set(), {}, {}
        for phase, items in ((None, self._cases), ("shadow", self._shadow)):
            for item in items:
                validate_case(item)
                identity = str(item["id"])
                if identity in seen_ids:
                    raise ValueError("evolution dataset contains duplicate identities")
                seen_ids.add(identity)
                split = phase or item["split"]
                repository = str(item["repository"]).strip()
                if not repository:
                    raise ValueError("repository is required for split isolation")
                repositories.setdefault(split, set()).add(repository)
                hashes.setdefault(split, set()).add(fingerprint(item["diff"]))
        for left in repositories:
            for right in repositories:
                if left >= right:
                    continue
                if repositories[left] & repositories[right] or hashes[left] & hashes[right]:
                    raise ValueError("evolution datasets must be repository and diff disjoint across splits")
        self.fingerprint = fingerprint({"cases": self._cases, "shadow": self._shadow})
        source_ok = all(
            (item.get("source") or {}).get("kind") in {"public-github-pr", "private-historical-pr"}
            and all("should_comment" in label for label in item["expected_findings"])
            for item in self._cases + self._shadow
        )
        self.production_ready = (source_ok and len(self._cases) >= 300
                                 and all(repositories.get(split) for split in ("train", "validation", "holdout", "shadow")))

    def cases(self, phase):
        if phase == "shadow":
            return copy.deepcopy(self._shadow)
        if phase not in {"train", "validation", "holdout"}:
            raise ValueError("unknown dataset phase")
        return copy.deepcopy([x for x in self._cases if x["split"] == phase])

    def summary(self):
        return {"sha256": self.fingerprint, "production_ready": self.production_ready,
                "counts": {p: len(self.cases(p)) for p in ("train", "validation", "holdout", "shadow")}}


class EvolutionProductReviewer:
    """Both kinds run the same full Agentic topology, rules, model and budget."""

    def __init__(self, kind, content, client, tokens=12000, seconds=240, input_price=0., output_price=0.):
        from govern.evaluation.evaluation_v2 import ProductArmReviewer
        from govern.skills.skills import AgentSkill
        self.delegate = ProductArmReviewer("full-agentic", client, tokens, seconds)
        self.tokens, self.seconds = tokens, seconds
        self.kind = kind
        self.skill_name = None
        router = self.delegate.router
        router.input_cost_per_million = input_price
        router.output_cost_per_million = output_price
        if kind == "prompt":
            router.prompt_overlay = content["prompt"]
        elif kind == "skill" and content:
            skill = AgentSkill.from_artifact(content)
            self.skill_name = skill.name
            router.skill_provider = lambda _tenant: [skill]
            self.delegate.store.task_input["enabled_skills"] = [skill.name]
        elif kind != "skill":
            raise ValueError("unsupported candidate kind")

    def review_case(self, case, parsed):
        # No labels, holdout ids, repository_root or tool permissions reach the model.
        return self.delegate.review_case({"diff": case["diff"], "repository": "replay://" + str(case["repository"])}, parsed)

    def evaluation_execution(self):
        return self.delegate.evaluation_execution()

    def evaluation_contract(self):
        summary = self.delegate._last_summary
        execution = summary.get("execution") or {}
        traces = execution.get("agent_traces") or {}
        tool_failures = [x for x in execution.get("tool_call_log", []) if not x.get("ok")]
        # Failed tool calls are treated conservatively; do not infer safety from omission.
        exhausted = sum(x.get("event") == "budget_exhausted" for values in traces.values() for x in values)
        missing_gate = not any(x.get("event") == "completed" for x in traces.get("evidence-gate", []))
        result = {"permission_violations": len(tool_failures),
                  "budget_violations": exhausted + int(execution.get("total_tokens", 0) > self.tokens)
                  + int(execution.get("duration_ms", 0) > self.seconds * 1000),
                  "required_gate_misses": int(missing_gate),
                  "telemetry_complete": bool(execution.get("model_call_log")),
                  "skill_selected": True}
        if self.kind == "skill" and self.skill_name:
            # Require real Skill selection in a worker trace, not merely registration.
            result["skill_selected"] = any(
                self.skill_name in (entry.get("selected_skills") or entry.get("skill_names") or [])
                for values in traces.values() for entry in values
            )
        return result


class EvolutionReplay:
    def __init__(self, reviewer_factory, policy=None):
        self.reviewer_factory = reviewer_factory
        self.policy = policy or CandidatePolicy()

    def run(self, kind, content, cases):
        reviewer = self.reviewer_factory(kind, content)
        extended = hasattr(reviewer, "evaluation_measurements")
        additional = {k:0 for k in ("p01_hits","p01_total","verification_attempts","sandbox_duration_ms","verification_timeouts","verification_environment_errors","verified_findings","runtime_reproduced")}
        if extended:
            additional["p01_total"] = sum(x.get("business_priority") in {"P0","P1"} and x.get("should_comment",True) for c in cases for x in c["expected_findings"])
        totals = {k: 0 for k in ("tp", "fp", "fn", "high_hits", "high_total", "clean_hits", "clean_total",
                                "evidence_hits", "predictions", "successful", "total_tokens", "cost_usd",
                                "duration_ms", "tool_calls", "permission_violations", "budget_violations",
                                "required_gate_misses", "invalid_findings")}
        complete, utilities, errors, selected = True, [], 0, True
        for case in cases:
            expected = [x for x in case["expected_findings"] if x.get("should_comment", True)]
            high = [x for x in expected if x["severity"] in {"high", "critical"}]
            totals["high_total"] += len(high)
            totals["clean_total"] += int(not expected)
            try:
                parsed = parse_unified_diff(case["diff"])
                start = time.monotonic()
                findings = reviewer.review_case(case, parsed)
                if extended:
                    for key,value in reviewer.evaluation_measurements(case, findings).items():
                        if key in additional and key != "p01_total": additional[key] += number({key:value},key)
                elapsed = (time.monotonic() - start) * 1000
                execution = reviewer.evaluation_execution()
                contract = reviewer.evaluation_contract()
                complete &= contract.get("telemetry_complete") is True
                selected &= contract.get("skill_selected") is True
                for key in ("total_tokens", "cost_usd", "tool_calls"):
                    totals[key] += number(execution, key)
                totals["duration_ms"] += number(execution, "duration_ms")
                # Wall time is available for diagnostics, but comparison uses the actual
                # runtime ledger so the chosen measurement is consistent across arms.
                if elapsed < 0:
                    raise ValueError("monotonic clock moved backwards")
                for key in ("permission_violations", "budget_violations", "required_gate_misses"):
                    totals[key] += number(contract, key)
                locations = {(x.path, x.line): x.content for x in parsed.added_lines}
                for finding in findings:
                    at = locations.get((finding.path, finding.line), "")
                    valid = bool(at and finding.evidence.strip() and finding.evidence.strip() in at)
                    # Tool evidence may support cross-line claims; the product Gate must
                    # have accepted it and returned an exact changed-line location.
                    valid |= bool(at and finding.evidence_refs and (finding.gate or {}).get("passed"))
                    totals["evidence_hits"] += int(valid)
                    totals["invalid_findings"] += int(not valid)
                matches = one_to_one_match(expected, findings, line_tolerance=0)
                tp, fp, fn = len(matches), len(findings) - len(matches), len(expected) - len(matches)
                totals["tp"] += tp
                totals["fp"] += fp
                totals["fn"] += fn
                totals["predictions"] += len(findings)
                totals["high_hits"] += sum(
                    SEVERITY_RANK[findings[match.predicted_index].severity.value] >= SEVERITY_RANK[high[match.expected_index]["severity"]]
                    for match in one_to_one_match(high, findings, line_tolerance=0)
                )
                totals["clean_hits"] += int(not expected and not findings)
                totals["successful"] += 1
                utilities.append(2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 1.)
            except Exception:
                # Never persist model errors: they can quote hidden case contents.
                errors += 1
                totals["fn"] += len(expected)
                complete = False
                utilities.append(0.)
        denominator = 2 * totals["tp"] + totals["fp"] + totals["fn"]
        result = {"cases": len(cases), "f1": 2 * totals["tp"] / denominator if denominator else 1.,
                  "precision": totals["tp"] / totals["predictions"] if totals["predictions"] else 1.,
                  "recall": totals["tp"] / (totals["tp"] + totals["fn"]) if totals["tp"] + totals["fn"] else 1.,
                  "false_discovery_rate": totals["fp"] / totals["predictions"] if totals["predictions"] else 0.,
                  "high_severity_recall": totals["high_hits"] / totals["high_total"] if totals["high_total"] else 1.,
                  "clean_accuracy": totals["clean_hits"] / totals["clean_total"] if totals["clean_total"] else 1.,
                  "evidence_accuracy": totals["evidence_hits"] / totals["predictions"] if totals["predictions"] else 1.,
                  "success_rate": totals["successful"] / len(cases) if cases else 0.,
                  "telemetry_complete": complete and bool(cases), "skill_selected": selected,
                  "error_count": errors,
                  **{key: totals[key] for key in ("total_tokens", "cost_usd", "duration_ms", "tool_calls",
                                                "permission_violations", "budget_violations", "required_gate_misses", "invalid_findings")}}
        if extended:
            result.update(additional)
            result["p01_recall"] = additional["p01_hits"]/additional["p01_total"] if additional["p01_total"] else None
            result["verification_timeout_rate"] = additional["verification_timeouts"]/additional["verification_attempts"] if additional["verification_attempts"] else 0.
        return result, utilities

    def compare(self, kind, baseline, candidate, cases, phase):
        before, before_utilities = self.run(kind, baseline, cases)
        after, after_utilities = self.run(kind, candidate, cases)
        gate = self.policy.compare(before, after, phase)
        try:
            interval = self.policy.paired_interval(before_utilities, after_utilities)
        except ValueError:
            interval = {"passed": False, "reason": "insufficient paired observations"}
        if kind == "skill":
            gate["gates"]["candidate_skill_selected"] = after["skill_selected"]
        ablation = None
        if kind == "skill" and phase == "validation":
            without, without_utilities = self.run("skill", {}, cases)
            ablation_gate = self.policy.compare(without, after, "validation")
            ablation_interval = self.policy.paired_interval(without_utilities, after_utilities) if len(cases) >= 2 else {"passed": False}
            ablation = {"without_skill": without, "with_skill": after,
                        "statistics": ablation_interval,
                        "passed": ablation_gate["passed"] and ablation_interval["passed"]}
            gate["gates"]["skill_independent_contribution"] = ablation["passed"]
        gate["gates"]["paired_non_regression"] = interval["passed"]
        gate["passed"] = gate["passed"] and all(gate["gates"].values())
        # Only aggregate metrics escape; no holdout identifiers, text, outputs or errors.
        return {**gate, "baseline": before, "candidate": after, "statistics": interval,
                "dataset_sha256": fingerprint(cases), "phase": phase, "skill_ablation": ablation}
