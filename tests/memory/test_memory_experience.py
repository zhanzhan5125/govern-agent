"""Shared backend contract for actual-use attribution, not catalog exposure."""
import concurrent.futures
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from govern.memory.memory import MemoryManager
from govern.memory.memory_governance import MemoryTransitionError
from govern.core.models import TaskState, TraceEvent
from govern.storage.store import TaskStore, utc_now


class MemoryExperienceContract:
    def memory(self, content="evidence for auth", **kwargs):
        return MemoryManager(self.store).remember("a", "org/repo", "semantic", "rule", content,
                                                 source_evidence=["fixture-evidence"], **kwargs)

    def task(self, name, complete=True):
        self.store.create(name, "org/repo", 1, {}, "a")
        if complete:
            self.store.transition(name, TraceEvent(1, TaskState.SUCCESS, "controlled completed task", utc_now()))

    def test_recall_does_not_receive_feedback_but_read_does(self):
        used, unused = self.memory(), self.memory("other auth guidance")
        self.task("one")
        manager = MemoryManager(self.store)
        self.assertTrue(manager.recall("a", "org/repo", "auth"))
        self.assertEqual([], manager.feedback_used("one", "a", True))
        self.assertEqual(used["id"], manager.read_for_task(used["id"], "one", "a", "org/repo")["id"])
        manager.feedback_used("one", "a", True)
        self.assertEqual(1, self.store.get_agent_memory(used["id"])["success_count"])
        self.assertEqual(0, self.store.get_agent_memory(unused["id"])["success_count"])

    def test_duplicate_feedback_negative_dominance_and_cross_task_counts(self):
        memory = self.memory()
        manager = MemoryManager(self.store)
        for task in ("one", "two"):
            self.task(task)
            manager.read_for_task(memory["id"], task, "a", "org/repo")
            manager.read_for_task(memory["id"], task, "a", "org/repo")
            manager.feedback_used(task, "a", True)
            manager.feedback_used(task, "a", True)
        current = self.store.get_agent_memory(memory["id"])
        self.assertEqual((2, 0), (current["success_count"], current["failure_count"]))
        self.assertEqual("VERIFIED", current["status"])  # Never auto-promote from counts alone.
        manager.feedback_used("one", "a", False)
        self.assertFalse(manager.feedback_used("one", "a", True)[0]["changed"])
        current = self.store.get_agent_memory(memory["id"])
        self.assertEqual((1, 1), (current["success_count"], current["failure_count"]))

    def test_concurrent_duplicate_feedback_has_one_increment(self):
        memory = self.memory()
        self.task("one")
        self.store.read_agent_memory_for_task(memory["id"], "one", "a", "org/repo")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.record_used_memory_feedback("one", "a", True), range(8)))
        self.assertEqual(1, sum(result[0]["changed"] for result in results))
        self.assertEqual(1, self.store.get_agent_memory(memory["id"])["success_count"])

    def test_scope_state_and_expired_memory_guards(self):
        memory = self.memory()
        self.task("one", complete=False)
        with self.assertRaises(PermissionError):
            self.store.read_agent_memory_for_task(memory["id"], "one", "b", "org/repo")
        with self.assertRaises(PermissionError):
            self.store.read_agent_memory_for_task(memory["id"], "one", "a", "org/other")
        with self.assertRaises(ValueError):
            self.store.record_used_memory_feedback("one", "a", True)
        with self.assertRaises(PermissionError):
            self.store.record_used_memory_feedback("one", "b", True)
        rejected = self.memory("rejected content", status="REJECTED")
        provisional = self.memory("provisional content", status="PROVISIONAL")
        self.store.update_agent_memory(memory["id"], memory["version"], {"expires_at": "2000-01-01T00:00:00+00:00"})
        for value in (memory, rejected, provisional):
            self.assertIsNone(self.store.read_agent_memory_for_task(value["id"], "one", "a", "org/repo"))

    def test_conflicts_block_promotion_even_with_successes(self):
        memory, other = self.memory(), self.memory("conflicting auth rule")
        manager = MemoryManager(self.store)
        for name in ("one", "two"):
            self.task(name)
            manager.read_for_task(memory["id"], name, "a", "org/repo")
            manager.feedback_used(name, "a", True)
        current = self.store.get_agent_memory(memory["id"])
        current = manager.mark_conflict(memory["id"], other["id"], current["version"])
        with self.assertRaisesRegex(MemoryTransitionError, "conflicts"):
            manager.transition(memory["id"], "PROMOTED", current["version"])

    def test_service_feedback_updates_only_used_memory(self):
        from govern.api.service import ReviewService
        memory = self.memory()
        self.task("one")
        self.store.succeed("one", SimpleNamespace(to_dict=lambda: {"findings": []}),
                           TraceEvent(2, TaskState.SUCCESS, "controlled report", utc_now()))
        service = ReviewService.__new__(ReviewService)
        service.store = self.store
        service.memory = MemoryManager(self.store)
        service._monitor_evolution_outcome = Mock(return_value=[])
        service.memory.read_for_task(memory["id"], "one", "a", "org/repo")
        for _ in range(2):
            self.assertEqual({"recorded": True, "category": "accepted"},
                             service.record_feedback("one", "accepted", None, "confirmed", "a"))
        self.assertEqual(1, self.store.get_agent_memory(memory["id"])["success_count"])


class MemoryExperienceTests(MemoryExperienceContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(os.path.join(directory.name, "memory.db"))
