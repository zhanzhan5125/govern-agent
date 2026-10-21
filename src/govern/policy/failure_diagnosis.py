"""Evidence-grounded failure routing; model-facing output never contains holdout data."""
from collections import defaultdict
import hashlib
import re

from govern.policy.candidate_policy import fingerprint
from govern.core.diff_parser import parse_unified_diff


RULE = re.compile(r"^[A-Z][A-Z0-9_-]{1,79}$")
CATEGORIES = {"false_positive", "missed_issue", "bad_fix", "execution_error"}
ERROR_TARGETS = {"tool_unavailable": "tool", "permission_denied": "tool",
                 "budget_exhausted": "workflow", "role_timeout": "workflow",
                 "invalid_label": "dataset"}


class FailureDiagnosis:
    def __init__(self, store):
        self.store = store

    def diagnose(self, tenant_id, protected_cases=()):
        """protected_cases is evaluator-owned and is never returned to the generator.

        Matching protected repositories AND exact diff hashes excludes copied holdout
        examples even when their task metadata has been relabelled as training data.
        """
        protected_repositories = {str(x["repository"]) for x in protected_cases if x.get("repository")}
        protected_hashes = {hashlib.sha256(str(x.get("diff", "")).encode("utf-8")).hexdigest() for x in protected_cases}
        signals, pending = [], []
        for failure in self.store.list_failure_cases(True, 100, tenant_id):
            task = self.store.get(failure["task_id"], tenant_id)
            if not task:
                continue
            task_input = task.get("input") or {}
            source = task_input.get("source") or {}
            repository = str(task.get("repository") or "")
            diff = self.store.get_task_payload(failure["task_id"]) or ""
            diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
            if (task_input.get("split") == "holdout" or
                (isinstance(source, dict) and source.get("split") == "holdout") or
                repository in protected_repositories or diff_hash in protected_hashes):
                # Do not return the id, count, category, repository or reason for excluded data.
                continue
            payload = failure.get("payload") or {}
            category = failure.get("category")
            if category not in CATEGORIES:
                continue
            finding = payload.get("finding") or {}
            rule_id = str(finding.get("rule_id", ""))
            path = str(finding.get("path", ""))
            evidence = str(finding.get("evidence", "")).strip()
            try:
                line = int(finding.get("line", 0))
                matching = next((x.content.strip() for x in parse_unified_diff(diff).added_lines
                                 if x.path == path and x.line == line), "")
            except (ValueError, TypeError):
                matching, line = "", 0
            error_code = str(payload.get("error_code", ""))
            if category == "execution_error":
                target = ERROR_TARGETS.get(error_code)
                if not target:
                    pending.append({"failure_id": failure["id"], "status": "needs_human_label",
                                    "reason": "execution failure needs a verified error category"})
                    continue
            else:
                if not RULE.fullmatch(rule_id) or not matching or not evidence or evidence not in matching:
                    pending.append({"failure_id": failure["id"], "status": "needs_human_label",
                                    "reason": "finding requires a valid rule and matching added-line evidence"})
                    continue
                target = "memory"
            signal = {
                "failure_id": failure["id"], "task_id": failure["task_id"], "repository": repository,
                "category": category, "target": target, "rule_id": rule_id if RULE.fullmatch(rule_id) else "",
                "path": path, "line": line, "evidence": evidence[:240] if matching else "",
                "diff_sha256": diff_hash, "error_code": error_code if error_code in ERROR_TARGETS else "",
                "baseline_version": task_input.get("prompt_version"),
                "source": "tenant-scoped-feedback-and-task-diff",
            }
            signal["signal_sha256"] = fingerprint(signal)
            signals.append(signal)
        groups = defaultdict(list)
        for signal in signals:
            groups[(signal["category"], signal["rule_id"], signal["target"])].append(signal)
        clusters = []
        for (category, rule, target), members in sorted(groups.items()):
            if target == "memory" and len({m["repository"] for m in members if m["repository"]}) >= 2:
                target = "prompt_skill"
            for member in members:
                member["target"] = target
                member["signal_sha256"] = fingerprint({k: v for k, v in member.items() if k != "signal_sha256"})
            clusters.append({"category": category, "rule_id": rule, "target": target,
                             "signal_ids": [m["signal_sha256"] for m in members],
                             "independent_tasks": len({m["task_id"] for m in members}),
                             "independent_repositories": len({m["repository"] for m in members if m["repository"]})})
        return {"signals": signals, "clusters": clusters, "needs_human_label": pending,
                "generation_signals": [x for x in signals if x["target"] == "prompt_skill"]}
