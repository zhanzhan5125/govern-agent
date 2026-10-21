"""Application orchestration for governed Prompt and Skill release candidates.

Only this layer is exposed by HTTP. Clients submit content and decisions; evaluation
reports, policy, provenance and lifecycle transitions are always server-generated.
"""
import difflib
import re

from govern.policy.candidate_policy import CandidateConflict, CandidatePolicy, fingerprint
from govern.policy.candidate_builder import CandidateBuilder
from govern.policy.evolution import DEFAULT_PROMPT, EvolutionEngine
from govern.policy.evolution_replay import EvolutionReplay
from govern.policy.failure_diagnosis import FailureDiagnosis
from govern.policy.evolution_lease import replay_lease
from govern.skills.skill_evolution import SkillEvolutionEngine, validate_artifact


class EvolutionPipeline:
    def __init__(self, store, dataset, reviewer_factory=None, policy=None,
                 baseline_provider=None, generator=None, runtime_identity=None):
        self.store = store
        self.dataset = dataset
        self.reviewer_factory = reviewer_factory
        self.policy = policy or CandidatePolicy()
        self.baseline_provider = baseline_provider
        self.generator = generator
        self.runtime_identity = dict(runtime_identity or {"runtime": "evolution-governance-v1"})
        self.diagnosis = FailureDiagnosis(store)

    def _get(self, candidate_id, tenant_id, revision=None):
        candidate = self.store.get_evolution_candidate(candidate_id, tenant_id)
        if candidate is None:
            raise KeyError("candidate not found")
        if revision is not None and candidate["revision"] != revision:
            raise CandidateConflict("candidate revision changed")
        if candidate["policy"] != self.policy.snapshot():
            raise CandidateConflict("candidate policy changed; propose and evaluate a new candidate")
        if candidate["lineage"].get("dataset_sha256") != self.dataset.fingerprint:
            raise CandidateConflict("candidate dataset changed; propose and evaluate a new candidate")
        if candidate["lineage"].get("runtime_identity") != self.runtime_identity:
            raise CandidateConflict("model/runtime changed; propose and evaluate a new candidate")
        return candidate

    def _baseline(self, tenant, kind, name):
        head = self.store.evolution_release_head(tenant, kind, name)
        if head["candidate_id"]:
            active = self.store.get_evolution_candidate(head["candidate_id"], tenant)
            content = active["content"]
            legacy_version = None
            disabled = bool(active["lineage"].get("disabled"))
        else:
            legacy = self.baseline_provider(tenant, kind, name) if self.baseline_provider else None
            content = legacy["content"] if legacy else (
                {"prompt": ""} if kind == "prompt" else SkillEvolutionEngine.empty_artifact(name))
            legacy_version = legacy.get("version") if legacy else None
            disabled = kind == "skill" and legacy is None
        return {"candidate_id": head["candidate_id"], "generation": head["generation"],
                "content": content, "legacy_version": legacy_version, "disabled": disabled}

    @staticmethod
    def _normalize(kind, name, content):
        if kind == "skill":
            normalized = validate_artifact(content, name)
            if any(token in text.lower() for text in normalized["files"].values() for token in EvolutionEngine.FORBIDDEN):
                raise ValueError("Skill candidate failed safety policy")
            return normalized
        if kind != "prompt":
            raise ValueError("candidate kind must be prompt or skill")
        if isinstance(content, str):
            content = {"prompt": content}
        if not isinstance(content, dict) or set(content) != {"prompt"} or not isinstance(content["prompt"], str):
            raise ValueError("Prompt candidate must contain only a prompt string")
        prompt = content["prompt"].strip()
        if not prompt or len(prompt) > 12000:
            raise ValueError("Prompt must contain 1..12000 characters")
        lowered = prompt.lower()
        if any(token in lowered for token in EvolutionEngine.FORBIDDEN):
            raise ValueError("candidate failed safety policy")
        if any(token not in lowered for token in ("diff", "severity", "fix", "test", "json")):
            raise ValueError("candidate omits required review output rules")
        return {"prompt": prompt}

    @staticmethod
    def change_diff(kind, baseline, candidate):
        before = {"prompt.txt": baseline["prompt"]} if kind == "prompt" else baseline["files"]
        after = {"prompt.txt": candidate["prompt"]} if kind == "prompt" else candidate["files"]
        return "\n".join("".join(difflib.unified_diff(
            str(before.get(path, "")).splitlines(keepends=True),
            str(after.get(path, "")).splitlines(keepends=True),
            fromfile="baseline/" + path, tofile="candidate/" + path,
        )) for path in sorted(set(before) | set(after)))

    def propose(self, tenant_id, kind, name, content, actor, lineage=None):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}", name):
            raise ValueError("invalid candidate name")
        normalized = self._normalize(kind, name, content)
        baseline = self._baseline(tenant_id, kind, name)
        lineage = {**(lineage or {}), "dataset_sha256": self.dataset.fingerprint,
                   "runtime_identity": self.runtime_identity,
                   "change_diff": self.change_diff(kind, baseline["content"], normalized)}
        candidate = self.store.create_evolution_candidate(
            tenant_id, kind, name, normalized, baseline, self.policy.snapshot(), lineage, actor)
        if candidate["status"] == "DRAFT":
            candidate = self.store.transition_evolution_candidate(candidate["id"], tenant_id,
                candidate["revision"], "STATIC_PASSED", "verifier", "static package validation passed",
                {"passed": True, "content_sha256": fingerprint(normalized)})
        return candidate

    def evaluate(self, candidate_id, tenant_id, expected_revision):
        with replay_lease(self.store, candidate_id, tenant_id, expected_revision) as owner:
            return self._evaluate(candidate_id, tenant_id, expected_revision, owner)

    def _evaluate(self, candidate_id, tenant_id, expected_revision, owner):
        candidate = self._get(candidate_id, tenant_id, expected_revision)
        if candidate["status"] not in {"STATIC_PASSED", "VALIDATION_PASSED"}:
            raise ValueError("candidate is not waiting for replay")
        if self.reviewer_factory is None:
            raise ValueError("model-backed replay is not configured")
        for phase, source, target in (("validation", "STATIC_PASSED", "VALIDATION_PASSED"),
                                       ("holdout", "VALIDATION_PASSED", "HOLDOUT_PASSED")):
            if candidate["status"] != source:
                continue
            self.store.renew_evolution_replay(candidate_id, tenant_id, owner)
            self._get(candidate_id, tenant_id, candidate["revision"])
            replay = EvolutionReplay(self.reviewer_factory, self.policy)
            report = replay.compare(candidate["kind"], candidate["baseline"]["content"],
                                    candidate["content"], self.dataset.cases(phase), phase)
            candidate = self.store.transition_evolution_candidate(candidate_id, tenant_id,
                candidate["revision"], target if report["passed"] else "REJECTED", "verifier",
                phase + (" replay passed" if report["passed"] else " replay failed"), report, lease_owner=owner)
            if candidate["status"] == "REJECTED":
                break
        return candidate

    def approve(self, candidate_id, tenant_id, expected_revision, actor, reason):
        candidate = self._get(candidate_id, tenant_id, expected_revision)
        if candidate["status"] != "HOLDOUT_PASSED":
            raise ValueError("approval requires completed Validation and Holdout")
        return self.store.transition_evolution_candidate(candidate_id, tenant_id, expected_revision,
            "HUMAN_APPROVED", actor, reason, {"passed": True, "approved_by": actor,
                                            "content_sha256": candidate["content_sha256"]})

    def reject(self, candidate_id, tenant_id, expected_revision, actor, reason):
        self._get(candidate_id, tenant_id, expected_revision)
        return self.store.transition_evolution_candidate(candidate_id, tenant_id, expected_revision,
                                                         "REJECTED", actor, reason)

    def shadow(self, candidate_id, tenant_id, expected_revision):
        with replay_lease(self.store, candidate_id, tenant_id, expected_revision) as owner:
            return self._shadow(candidate_id, tenant_id, expected_revision, owner)

    def _shadow(self, candidate_id, tenant_id, expected_revision, owner):
        candidate = self._get(candidate_id, tenant_id, expected_revision)
        if candidate["status"] != "HUMAN_APPROVED":
            raise ValueError("shadow requires human approval")
        if self.reviewer_factory is None:
            raise ValueError("model-backed replay is not configured")
        if len(self.dataset.cases("shadow")) < self.policy.min_shadow_cases:
            raise ValueError("independent shadow dataset is not ready")
        report = EvolutionReplay(self.reviewer_factory, self.policy).compare(
            candidate["kind"], candidate["baseline"]["content"], candidate["content"],
            self.dataset.cases("shadow"), "shadow")
        report["production_ready"] = self.dataset.production_ready
        report["mode"] = "isolated-read-only-shadow-replay"
        return self.store.transition_evolution_candidate(candidate_id, tenant_id, expected_revision,
            "SHADOW" if report["passed"] else "REJECTED", "shadow-verifier", "independent shadow replay completed", report, lease_owner=owner)

    def activate(self, candidate_id, tenant_id, expected_revision, actor, reason):
        candidate = self._get(candidate_id, tenant_id, expected_revision)
        current = self._baseline(tenant_id, candidate["kind"], candidate["name"])
        if current != candidate["baseline"]:
            raise CandidateConflict("active baseline changed; candidate must be reevaluated")
        return self.store.publish_evolution_candidate(candidate_id, tenant_id, expected_revision,
            candidate["baseline"]["generation"], actor, reason)

    def rollback(self, candidate_id, tenant_id, expected_revision, expected_generation, actor, reason):
        # A previously published known-good version remains usable even if the active
        # evaluation dataset or policy has changed; do not regenerate it on rollback.
        return self.store.publish_evolution_candidate(candidate_id, tenant_id, expected_revision,
            expected_generation, actor, reason, rollback=True)

    def diagnose(self, tenant_id):
        return self.diagnosis.diagnose(tenant_id, self.dataset.cases("validation") + self.dataset.cases("holdout") + self.dataset.cases("shadow"))

    def propose_from_failures(self, tenant_id, name, actor, kind="prompt"):
        diagnosis = self.diagnose(tenant_id)
        signals = diagnosis["generation_signals"]
        if not signals or self.generator is None:
            return {"candidates": [], "diagnosis": diagnosis, "reason": "no verified cross-repository signal or generator unavailable"}
        baseline = self._baseline(tenant_id, kind, name)["content"]
        base = (baseline["prompt"] or DEFAULT_PROMPT) if kind == "prompt" else DEFAULT_PROMPT
        candidates = []
        # Each independent root-cause group produces a bounded difference proposal.
        for cluster in diagnosis["clusters"]:
            if cluster["target"] != "prompt_skill" or len(candidates) >= self.policy.max_candidates:
                continue
            members = [x for x in signals if x["signal_sha256"] in cluster["signal_ids"]]
            generation_input = [{"id": x["failure_id"], "task_id": x["task_id"], "category": x["category"],
                                 "payload": {"finding": {"rule_id": x["rule_id"], "path": x["path"],
                                                         "line": x["line"], "evidence": x["evidence"]}}} for x in members]
            generated = self.generator.generate(generation_input, base)
            for built in CandidateBuilder().build(kind, baseline, generated, self.policy.max_candidates - len(candidates)):
                candidates.append(self.propose(tenant_id, kind, name, built["content"], actor,
                    {"source": "verified-failure-cluster", "signals": members, "variant": built["variant"],
                     "generator": generated.get("generator", {}), "generation_usage": generated.get("generation", {})}))
        return {"candidates": candidates, "diagnosis": diagnosis}

    def compare_pool(self, tenant_id, candidate_ids):
        if not 1 <= len(candidate_ids) <= self.policy.max_candidates or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("pool requires one to three distinct candidates")
        candidates = [self._get(item, tenant_id) for item in candidate_ids]
        first = candidates[0]
        if any((x["kind"], x["name"], x["baseline_sha256"]) !=
               (first["kind"], first["name"], first["baseline_sha256"]) for x in candidates):
            raise ValueError("Pareto comparison requires the same target and baseline")
        measured = [{"id": item["id"], "passed": item["status"] in {"HOLDOUT_PASSED", "HUMAN_APPROVED", "SHADOW"},
                     "metrics": item["reports"].get("VALIDATION_PASSED", {}).get("candidate", {})} for item in candidates]
        return {"frontier": self.policy.pareto(measured), "compared": candidate_ids,
                "selection_split": "validation", "automatic_activation": False}
