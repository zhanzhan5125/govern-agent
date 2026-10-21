"""Idempotent release outcomes and threshold-based rollback of the current head."""
from govern.policy.candidate_policy import CandidateConflict


class EvolutionMonitor:
    def __init__(self, store, minimum_samples=10, maximum_failure_rate=.2):
        if minimum_samples < 1 or not 0 <= maximum_failure_rate <= 1:
            raise ValueError("invalid release monitoring threshold")
        self.store = store
        self.minimum_samples = minimum_samples
        self.maximum_failure_rate = maximum_failure_rate

    def record(self, task_id, tenant_id, source, successful):
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise PermissionError("release feedback requires a tenant-owned task")
        snapshot = self.store.load_checkpoints(task_id).get("evolution-release-context", {}).get("state") or {}
        if snapshot.get("tenant_id") != tenant_id:
            return []
        ids = ([snapshot["prompt_candidate_id"]] if snapshot.get("prompt_candidate_id") else [])
        ids += snapshot.get("skill_candidate_ids") or []
        results = []
        for candidate_id in dict.fromkeys(ids):
            summary = self.store.record_evolution_outcome(candidate_id, task_id, tenant_id, source, successful)
            summary["candidate_id"] = candidate_id
            summary["rolled_back"] = False
            if summary["samples"] >= self.minimum_samples and summary["failures"] / summary["samples"] > self.maximum_failure_rate:
                current = self.store.get_evolution_candidate(candidate_id, tenant_id)
                head = self.store.evolution_release_head(tenant_id, current["kind"], current["name"])
                previous_id = current["reports"].get("release", {}).get("previous_candidate_id")
                if head["candidate_id"] == candidate_id and previous_id:
                    previous = self.store.get_evolution_candidate(previous_id, tenant_id)
                    try:
                        restored = self.store.publish_evolution_candidate(previous_id, tenant_id, previous["revision"],
                            head["generation"], "release-monitor", "%s failure rate %d/%d exceeded threshold" % (
                                source, summary["failures"], summary["samples"]), rollback=True)
                        summary.update(rolled_back=True, restored_candidate_id=restored["id"])
                    except CandidateConflict:
                        summary["reason"] = "release changed concurrently; newer head preserved"
            results.append(summary)
        return results
