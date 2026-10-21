"""Release monitoring with isolated stores; fixtures do not prove model quality."""
import os
import tempfile
import unittest
from unittest.mock import Mock

from govern.policy.candidate_policy import CandidatePolicy
from govern.policy.evolution_monitor import EvolutionMonitor
from govern.runtime.runtime import RuntimeCancelled
from govern.api.service import ReviewService
from govern.storage.store import TaskStore


class EvolutionMonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TaskStore(os.path.join(temporary.name, "monitor.db"))
        self.monitor = EvolutionMonitor(self.store, minimum_samples=2, maximum_failure_rate=.5)

    def release(self, text):
        head = self.store.evolution_release_head("a", "prompt", "llm-review")
        baseline = {**head, "content": {"prompt": "original"}}
        candidate = self.store.create_evolution_candidate(
            "a", "prompt", "llm-review", {"prompt": text}, baseline,
            CandidatePolicy().snapshot(), {"source": "controlled-test"}, "tester")
        for stage in ("STATIC_PASSED", "VALIDATION_PASSED", "HOLDOUT_PASSED", "HUMAN_APPROVED", "SHADOW"):
            candidate = self.store.transition_evolution_candidate(
                candidate["id"], "a", candidate["revision"], stage, "tester", "test fixture only",
                {"passed": True, "production_ready": True})
        return self.store.publish_evolution_candidate(
            candidate["id"], "a", candidate["revision"], head["generation"], "tester", "test fixture only")

    def task(self, task_id, candidate, tenant="a"):
        self.store.create(task_id, "org/repo", 1, {}, tenant)
        self.store.save_checkpoint(task_id, "evolution-release-context", {
            "tenant_id": tenant, "prompt_candidate_id": candidate["id"], "skill_candidate_ids": []})

    def test_first_release_restores_archived_configuration_and_deduplicates(self):
        candidate = self.release("new")
        self.task("one", candidate)
        self.task("two", candidate)
        for _ in range(3):
            outcome = self.monitor.record("one", "a", "human_feedback", False)[0]
            self.assertEqual((1, 1, False), (outcome["samples"], outcome["failures"], outcome["rolled_back"]))
        outcome = self.monitor.record("two", "a", "human_feedback", False)[0]
        self.assertTrue(outcome["rolled_back"])
        restored = self.store.get_evolution_candidate(outcome["restored_candidate_id"], "a")
        self.assertEqual({"prompt": "original"}, restored["content"])
        self.assertTrue(restored["reports"]["release"]["rollback"])
        self.assertEqual("ROLLED_BACK", self.store.get_evolution_candidate(candidate["id"], "a")["status"])
        self.assertFalse(self.monitor.record("two", "a", "human_feedback", False)[0]["rolled_back"])
        self.assertEqual(2, self.store.evolution_release_head("a", "prompt", "llm-review")["generation"])

    def test_negative_feedback_dominates_and_sources_are_separate(self):
        candidate = self.release("new")
        self.task("one", candidate)
        self.monitor.record("one", "a", "human_feedback", True)
        self.monitor.record("one", "a", "human_feedback", False)
        human = self.monitor.record("one", "a", "human_feedback", True)[0]
        execution = self.monitor.record("one", "a", "execution", True)[0]
        self.assertEqual((1, 1), (human["samples"], human["failures"]))
        self.assertEqual((1, 0), (execution["samples"], execution["failures"]))

    def test_late_feedback_cannot_rollback_newer_release(self):
        old = self.release("old")
        self.task("one", old)
        self.task("two", old)
        newer = self.release("newer")
        self.monitor.record("one", "a", "execution", False)
        self.assertFalse(self.monitor.record("two", "a", "execution", False)[0]["rolled_back"])
        self.assertEqual(newer["id"], self.store.evolution_release_head("a", "prompt", "llm-review")["candidate_id"])

    def test_threshold_is_strict_and_requires_minimum_sample_count(self):
        candidate = self.release("new")
        self.task("one", candidate)
        self.task("two", candidate)
        self.assertFalse(self.monitor.record("one", "a", "execution", False)[0]["rolled_back"])
        self.assertFalse(self.monitor.record("two", "a", "execution", True)[0]["rolled_back"])

    def test_cross_tenant_and_missing_snapshot_cannot_supply_outcomes(self):
        candidate = self.release("new")
        self.task("one", candidate)
        with self.assertRaises(PermissionError):
            self.monitor.record("one", "b", "execution", False)
        self.store.create("plain", "org/repo", 2, {}, "a")
        self.assertEqual([], self.monitor.record("plain", "a", "execution", False))
        self.assertEqual(candidate["id"], self.store.evolution_release_head("a", "prompt", "llm-review")["candidate_id"])

    def test_user_cancellation_is_not_release_failure(self):
        service = ReviewService.__new__(ReviewService)
        service.harness = Mock()
        service.harness.run.side_effect = RuntimeCancelled("user cancelled")
        service._monitor_evolution_outcome = Mock()
        with self.assertRaises(RuntimeCancelled):
            service._run_review("task", "org/repo", 1, "diff", "a")
        service._monitor_evolution_outcome.assert_not_called()
