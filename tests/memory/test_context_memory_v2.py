import json
import os
import tempfile
import unittest

from govern.runtime.artifacts import ArtifactStore
from govern.evaluation.compression_eval import CompressionCase, CompressionEvaluation
from govern.memory.context_manager import ContextManager, estimate_tokens
from govern.runtime.evidence import EvidenceCaptureHook, EvidenceStore
from govern.runtime.hooks import HookPipeline, HookPoint, HookRegistration
from govern.memory.memory import MemoryManager
from govern.memory.memory_governance import (
    MemoryStatus, MemoryTransitionError, MemoryVersionConflict,
)
from govern.runtime.runtime import AgentTool, ToolRegistry
from govern.storage.store import TaskStore


DIFF = """--- a/src/auth.py
+++ b/src/auth.py
@@ -1,2 +1,4 @@
 def authorize(user):
+    token = user.token
+    return eval(token)
"""


def large_diff():
    values = []
    for index in range(18):
        path = "src/module_%02d.py" % index
        changed = "value = normalize(input_%d)" % index
        if index == 13:
            path = "src/auth.py"
            changed = "return eval(user.token)"
        values.extend([
            "--- a/%s" % path, "+++ b/%s" % path,
            "@@ -1,3 +1,24 @@", " context = True", "+%s" % changed,
            *["+trace_%d_%d = normalize(value)" % (index, line) for line in range(20)],
        ])
    return "\n".join(values) + "\n"


class ContextMemoryV2Tests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_evidence_is_immutable_tenant_scoped_and_diff_verifiable(self):
        self.store.create("task", "org/repo", 1, {}, "tenant-a")
        artifacts = ArtifactStore(self.store, preview_chars=300)
        evidence = EvidenceStore(artifacts)
        pipeline = HookPipeline([HookRegistration(
            "evidence", EvidenceCaptureHook(evidence),
            (HookPoint.TOOL_AFTER,), priority=10, fail_open=False,
        )])
        registry = ToolRegistry([
            AgentTool(
                "changed_line", "Read one changed line.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}, "line": {"type": "integer"},
                    },
                    "required": ["path", "line"], "additionalProperties": False,
                },
                lambda path, line: {
                    "evidence_id": "legacy-line", "tool": "changed_line",
                    "output": {"path": path, "line": line, "content": "return eval(token)"},
                },
            )
        ], hooks=pipeline, runtime_context={
            "task_id": "task", "tenant_id": "tenant-a", "repository": "org/repo",
            "role": "security", "node": "executing",
        }, store=self.store)

        result = registry.invoke("changed_line", {"path": "src/auth.py", "line": 3})
        record = result["evidence_record"]

        self.assertTrue(result["evidence_id"].startswith("ev-"))
        self.assertEqual("legacy-line", result["source_evidence_id"])
        self.assertEqual("src/auth.py", record["path"])
        self.assertEqual([3], record["added_lines"])
        self.assertTrue(evidence.verify(record, "tenant-a", DIFF)["verified"])
        with self.assertRaises(KeyError):
            evidence.read(record["artifact_ref"], "tenant-b")

    def test_four_level_compaction_preserves_evidence_and_offloads_full_diff(self):
        self.store.create("task", "org/repo", 1, {}, "tenant-a")
        artifacts = ArtifactStore(self.store, preview_chars=256)
        manager = ContextManager(
            context_window_tokens=4096, input_token_budget=2200,
            diff_token_budget=750, observation_token_budget=260,
            recent_observations=0, artifact_store=artifacts,
            artifact_threshold_bytes=1024,
            model_summarizer=lambda _value, _budget: "bounded conversation summary",
        )
        diff = large_diff()
        payload = manager.compress_diff(
            diff, "task", "security", focus_files=["src/auth.py"],
            risk_domains=["authorization"], tenant_id="tenant-a",
        )
        evidence_record = {
            "evidence_id": "ev-test", "artifact_ref": "artifact://abc",
            "artifact_sha256": "sha", "path": "src/auth.py", "added_lines": [3],
            "excerpt_hash": "excerpt", "tool": "changed_line", "created_by": "security",
        }
        observations = [{
            "step": index, "tool": "changed_line", "ok": True,
            "result": {
                "evidence_id": "ev-test", "evidence_record": evidence_record,
                "output": {"content": "x" * 5000},
            },
        } for index in range(4)]
        compact, stats = manager.compact_observations(observations, 260)

        self.assertIsNotNone(payload["artifact"])
        self.assertEqual(diff, artifacts.get(payload["artifact"]["artifact_id"], "tenant-a"))
        self.assertIn("src/auth.py", {item["path"] for item in payload["selected_hunks"]})
        self.assertGreater(stats["summarized"], 0)
        self.assertIn("ev-test", json.dumps(compact))
        self.assertLess(estimate_tokens(compact), estimate_tokens(observations))

        task = json.dumps({
            "phase": "review", "conversation": "z" * 15000,
            "evidence_record": evidence_record,
        })
        managed, managed_stats = manager.build_managed_context(
            task, [], observations, 2000, 30, system_prompt="rules", max_output_tokens=512,
        )
        self.assertLessEqual(
            managed_stats["estimated_input_tokens_after"], managed_stats["input_token_limit"],
        )
        self.assertIn("ev-test", managed["task"])

        seen = []
        manager.model_summarizer = lambda value, _budget: seen.append(value) or "summary"
        summarized = manager._model_summary_task(task, 900)
        self.assertTrue(seen)
        self.assertIn("conversation", seen[0])
        self.assertNotIn("evidence", json.dumps(seen[0]))
        self.assertIn("ev-test", summarized)

    def test_memory_lifecycle_conflict_feedback_and_progressive_disclosure(self):
        memory = MemoryManager(self.store, recall_limit=4)
        first = memory.remember(
            "tenant-a", "org/repo", "semantic", "review_feedback",
            "Authentication wrapper permits dynamic eval of user token. " + "detail " * 150,
            status=MemoryStatus.PROVISIONAL.value, source_evidence=["ev-auth"],
            importance=0.95,
        )
        self.assertEqual([], memory.recall("tenant-a", "org/repo", "authentication eval"))

        first = memory.record_outcome(first["id"], first["version"], True)
        first = memory.record_outcome(first["id"], first["version"], True)
        first = memory.transition(
            first["id"], MemoryStatus.VERIFIED.value, first["version"], "reviewer", "confirmed",
        )
        first = memory.transition(
            first["id"], MemoryStatus.PROMOTED.value, first["version"], "reviewer", "repeated",
        )
        recalled = memory.recall("tenant-a", "org/repo", "authentication eval token")
        self.assertEqual(first["id"], recalled[0]["id"])
        catalog = ContextManager.format_memories(recalled)
        self.assertEqual("catalog-preview", catalog["disclosure"])
        self.assertLess(len(catalog["items"][0]["content"]), len(first["content"]))
        self.assertEqual(first["content"], memory.get(first["id"], "tenant-a", "org/repo")["content"])
        self.assertIsNone(memory.get(first["id"], "tenant-b", "org/repo"))

        with self.assertRaises(MemoryVersionConflict):
            memory.record_outcome(first["id"], first["version"] - 1, True)
        rejected = memory.record_outcome(
            first["id"], first["version"], False, contradicted=True, note="counterexample",
        )
        self.assertEqual(MemoryStatus.REJECTED.value, rejected["status"])
        self.assertEqual([], memory.recall("tenant-a", "org/repo", "authentication eval token"))

        other = memory.remember(
            "tenant-b", "org/repo", "semantic", "rule", "different tenant rule",
        )
        with self.assertRaises(MemoryTransitionError):
            memory.mark_conflict(rejected["id"], other["id"], rejected["version"])

    def test_paired_compression_evaluation_meets_quality_and_cost_gates(self):
        manager = ContextManager(
            input_token_budget=2400, diff_token_budget=700,
            observation_token_budget=220, recent_observations=0,
        )
        observation = {
            "step": 1, "tool": "changed_line", "ok": True,
            "result": {
                "evidence_id": "ev-benchmark",
                "evidence_record": {
                    "evidence_id": "ev-benchmark", "artifact_ref": "artifact://benchmark",
                    "artifact_sha256": "sha", "path": "src/auth.py", "added_lines": [3],
                    "excerpt_hash": "excerpt", "tool": "changed_line", "created_by": "security",
                },
                "output": {"content": "trace" * 3000},
            },
        }
        report = CompressionEvaluation(manager).run([
            CompressionCase(
                "auth-risk", large_diff(), required_paths=["src/auth.py"],
                risk_domains=["authorization", "token"],
                observations=[observation, observation, observation],
            )
        ], repeats=2)

        self.assertTrue(report["passed"], report)
        self.assertGreaterEqual(report["token_reduction_ratio"], 0.30)
        self.assertEqual(1.0, report["required_path_recall"])
        self.assertEqual(1.0, report["evidence_reference_recall"])


if __name__ == "__main__":
    unittest.main()
