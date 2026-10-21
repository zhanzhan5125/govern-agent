import os
import tempfile
import unittest

from govern.runtime.artifacts import ArtifactOffloadHook, ArtifactStore
from govern.review.agentic_core import BoundedRole
from govern.runtime.goal_gate import ReviewGoalGate, RuntimeGoalNotMet
from govern.runtime.harness import ReviewHarness
from govern.runtime.hooks import (
    ApprovalHook, HookAction, HookPipeline, HookPoint, HookRegistration,
    HookResult, RuntimeApprovalRequired, RuntimeHookBlocked, RuntimeHookError,
)
from govern.runtime.journal import EffectExecutor
from govern.core.models import Finding, Severity
from govern.review.reviewer import LocalRuleReviewer
from govern.runtime.runtime import AgentRuntime, AgentTool, RuntimeNode, ToolRegistry
from govern.storage.store import TaskStore
from govern.infrastructure.telemetry import ExecutionLedger


DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"


class RuntimeHarnessV2Tests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def create_task(self, task_id="task", tenant_id="tenant-a"):
        self.store.create(task_id, "org/repo", 1, {}, tenant_id)

    def test_hook_pipeline_is_ordered_modifiable_and_fail_closed(self):
        order = []

        def first(context):
            order.append("first")
            return HookResult(HookAction.MODIFY, {
                "state": {**context.payload["state"], "hooked": True},
            })

        def degraded(_context):
            order.append("degraded")
            raise RuntimeError("optional enrichment unavailable")

        pipeline = HookPipeline([
            HookRegistration(
                "second", degraded, (HookPoint.NODE_BEFORE,),
                priority=20, fail_open=True,
            ),
            HookRegistration(
                "first", first, (HookPoint.NODE_BEFORE,), priority=10,
            ),
        ])
        runtime = AgentRuntime(max_steps=2, timeout_seconds=5, hooks=pipeline)
        result = runtime.execute(
            {}, [RuntimeNode("node", lambda state: {"seen": state["hooked"]})],
        )

        self.assertTrue(result["seen"])
        self.assertEqual(["first", "degraded"], order)

        blocker = HookPipeline([HookRegistration(
            "guard", lambda _context: HookResult(
                HookAction.BLOCK, reason="policy denied"
            ), (HookPoint.NODE_BEFORE,),
        )])
        with self.assertRaisesRegex(RuntimeHookBlocked, "policy denied"):
            AgentRuntime(2, 5, hooks=blocker).execute(
                {}, [RuntimeNode("node", lambda _state: {})]
            )

        broken = HookPipeline([HookRegistration(
            "broken-policy", lambda _context: 1 / 0,
            (HookPoint.NODE_BEFORE,), fail_open=False,
        )])
        with self.assertRaisesRegex(RuntimeHookError, "failed closed"):
            AgentRuntime(2, 5, hooks=broken).execute(
                {}, [RuntimeNode("node", lambda _state: {})]
            )

    def test_runtime_journal_is_append_only_and_resume_does_not_repeat_nodes(self):
        self.create_task()
        calls = []
        runtime = AgentRuntime(max_steps=3, timeout_seconds=5)
        nodes = [
            RuntimeNode("plan", lambda _state: calls.append("plan") or {"value": 2}),
            RuntimeNode(
                "execute",
                lambda state: calls.append("execute") or {"result": state["value"] * 2},
            ),
        ]

        first = runtime.execute({}, nodes, "task", self.store)
        first_events = self.store.list_runtime_events("task")
        second = runtime.execute({}, nodes, "task", self.store)
        all_events = self.store.list_runtime_events("task")

        self.assertEqual(4, first["result"])
        self.assertEqual(4, second["result"])
        self.assertEqual(["plan", "execute"], calls)
        self.assertEqual(
            list(range(1, len(all_events) + 1)),
            [item["sequence"] for item in all_events],
        )
        self.assertGreater(len(all_events), len(first_events))
        self.assertEqual(
            2,
            sum(item["kind"] == HookPoint.CHECKPOINT_RESTORED.value for item in all_events),
        )
        with self.store._connect() as conn:
            with self.assertRaises(Exception):
                conn.execute(
                    "UPDATE runtime_journal SET kind='MUTATED' WHERE task_id='task'"
                )

    def test_effect_executor_reuses_commit_and_retries_failure(self):
        self.create_task()
        calls = []
        executor = EffectExecutor(self.store)

        first = executor.execute_once(
            "task", "publish", {"target": "pr"},
            lambda: calls.append("publish") or {"id": 7},
        )
        second = executor.execute_once(
            "task", "publish", {"target": "pr"},
            lambda: calls.append("duplicate") or {"id": 8},
        )

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual({"id": 7}, second.result)
        self.assertEqual(["publish"], calls)

        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return "ok"

        with self.assertRaisesRegex(RuntimeError, "transient"):
            executor.execute_once("task", "flaky", {}, flaky)
        recovered = executor.execute_once("task", "flaky", {}, flaky)
        self.assertEqual("ok", recovered.result)
        self.assertEqual(2, len(attempts))

    def test_approval_hook_blocks_side_effect_before_handler(self):
        self.create_task()
        calls = []
        pipeline = HookPipeline([HookRegistration(
            "approval", ApprovalHook(), (HookPoint.TOOL_BEFORE,), priority=1,
        )])
        tool = AgentTool(
            "write", "write", {"type": "object", "properties": {},
                                  "additionalProperties": False},
            lambda: calls.append("write") or "done",
            side_effect=True, requires_approval=True,
        )
        registry = ToolRegistry(
            [tool], pipeline,
            {"task_id": "task", "tenant_id": "tenant-a"}, self.store,
        )
        with self.assertRaises(RuntimeApprovalRequired):
            registry.invoke("write", {})
        self.assertEqual([], calls)

        registry.configure_runtime({"approval": {"approved": True}})
        self.assertEqual("done", registry.invoke("write", {}))
        self.assertEqual("done", registry.invoke("write", {}))
        self.assertEqual(["write"], calls)

    def test_persisted_approval_pauses_and_resumes_harness(self):
        calls = []
        store = self.store

        class ApprovalReviewer:
            name = "approval-reviewer"

            def configure_runtime(self, hooks=None):
                self.hooks = hooks

            def review_with_context(
                self, task_id, diff, parsed, repository="", tenant_id="default",
            ):
                registry = ToolRegistry([
                    AgentTool(
                        "publish", "publish",
                        {"type": "object", "properties": {},
                         "additionalProperties": False},
                        lambda: calls.append("publish") or {"published": True},
                        side_effect=True, requires_approval=True,
                    )
                ], self.hooks, {
                    "task_id": task_id, "tenant_id": tenant_id,
                    "repository": repository, "node": "executing", "role": "reviewer",
                }, store)
                registry.invoke("publish", {})
                return LocalRuleReviewer().review(diff, parsed)

        self.create_task()
        harness = ReviewHarness(self.store, ApprovalReviewer(), node_retries=0)
        with self.assertRaises(RuntimeApprovalRequired):
            harness.run("task", "org/repo", 1, DIFF, "tenant-a")
        task = self.store.get("task", "tenant-a")
        self.assertEqual("WAITING_APPROVAL", task["state"])
        approvals = self.store.list_runtime_approvals("task", "tenant-a")
        self.assertEqual(1, len(approvals))
        self.assertEqual("PENDING", approvals[0]["status"])
        self.assertEqual([], calls)

        self.store.decide_runtime_approval(
            "task", approvals[0]["semantic_key"], True,
            "admin", "reviewed", "tenant-a",
        )
        report = harness.resume("task", "org/repo", 1, DIFF, "tenant-a")
        self.assertEqual("high", report.risk)
        self.assertEqual(["publish"], calls)
        self.assertEqual("SUCCESS", self.store.get("task", "tenant-a")["state"])

    def test_large_tool_result_is_offloaded_and_integrity_checked(self):
        self.create_task()
        artifacts = ArtifactStore(self.store, preview_chars=200)
        pipeline = HookPipeline([HookRegistration(
            "offload", ArtifactOffloadHook(artifacts, threshold_bytes=1024),
            (HookPoint.TOOL_AFTER,), fail_open=True,
        )])
        registry = ToolRegistry([
            AgentTool(
                "read", "read", {"type": "object", "properties": {},
                                    "additionalProperties": False},
                lambda: {
                    "evidence_id": "read:1", "tool": "read",
                    "output": {"text": "x" * 5000},
                },
            )
        ], pipeline, {"task_id": "task", "tenant_id": "tenant-a"}, self.store)

        result = registry.invoke("read", {})
        reference = result["output"]
        self.assertTrue(reference["truncated"])
        self.assertLess(len(reference["preview"]), 5000)
        original = artifacts.get(reference["artifact_id"], "tenant-a")
        self.assertEqual(5000, len(original["output"]["text"]))
        with self.assertRaises(KeyError):
            artifacts.get(reference["artifact_id"], "tenant-b")

        with self.store._connect() as conn:
            conn.execute(
                "UPDATE runtime_artifacts SET content='tampered' WHERE id=?",
                (reference["artifact_id"],),
            )
        with self.assertRaisesRegex(ValueError, "integrity"):
            artifacts.get(reference["artifact_id"], "tenant-a")

    def test_bounded_role_emits_model_and_tool_hook_events(self):
        self.create_task()

        class ToolThenFinalClient:
            provider = "fake"
            model = "fake"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _role, _prompt, _user, ledger=None, max_tokens=None):
                self.calls += 1
                if ledger:
                    ledger.record_model("security", "fake", "fake", {
                        "prompt_tokens": 1, "completion_tokens": 1,
                    }, 1)
                if self.calls == 1:
                    return {"action": "tool", "tool": "lookup", "arguments": {}}
                return {"action": "final", "findings": []}

        registry = ToolRegistry([
            AgentTool(
                "lookup", "lookup",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda: {"evidence_id": "lookup:1", "tool": "lookup", "output": "ok"},
            )
        ], runtime_context={
            "task_id": "task", "tenant_id": "tenant-a",
            "repository": "org/repo", "node": "executing",
        }, store=self.store)
        result = BoundedRole(
            "security", "review", ToolThenFinalClient(), 1000, 5,
        ).run("{}", registry, ExecutionLedger("agentic"))

        self.assertEqual("final", result["action"])
        kinds = [item["kind"] for item in self.store.list_runtime_events("task")]
        self.assertEqual(2, kinds.count(HookPoint.MODEL_BEFORE.value))
        self.assertEqual(2, kinds.count(HookPoint.MODEL_AFTER.value))
        self.assertEqual(1, kinds.count(HookPoint.TOOL_BEFORE.value))
        self.assertEqual(1, kinds.count(HookPoint.TOOL_AFTER.value))

    def test_goal_gate_blocks_invalid_final_report(self):
        class InvalidReviewer:
            name = "invalid"

            def review(self, _diff, _parsed):
                return [Finding(
                    "SEC-X", Severity.HIGH, "bad", "bad", "x.py", 999,
                    "not in diff", "fix it", "test it",
                )]

        self.create_task()
        with self.assertRaises(RuntimeGoalNotMet):
            ReviewHarness(self.store, InvalidReviewer(), node_retries=0).run(
                "task", "org/repo", 1, DIFF, "tenant-a"
            )
        task = self.store.get("task")
        self.assertEqual("FAILED", task["state"])
        goal_events = [
            item for item in task["runtime_journal"]
            if item["kind"] == HookPoint.GOAL_CHECK_AFTER.value
        ]
        self.assertEqual(1, len(goal_events))
        self.assertFalse(goal_events[0]["detail"]["complete"])

    def test_successful_harness_persists_goal_gate_and_runtime_metadata(self):
        self.create_task()
        report = ReviewHarness(self.store, LocalRuleReviewer()).run(
            "task", "org/repo", 1, DIFF, "tenant-a"
        )
        metadata = report.execution["runtime_harness"]
        self.assertTrue(metadata["goal_gate"]["complete"])
        self.assertTrue(metadata["journal"]["append_only"])
        self.assertEqual("completed", self.store.load_checkpoints("task")["goal-gate"]["status"])
        kinds = {item["kind"] for item in self.store.list_runtime_events("task")}
        self.assertTrue({
            HookPoint.RUN_START.value, HookPoint.NODE_BEFORE.value,
            HookPoint.CHECKPOINT_SAVED.value, HookPoint.GOAL_CHECK_AFTER.value,
            HookPoint.RUN_STOP.value,
        }.issubset(kinds))


if __name__ == "__main__":
    unittest.main()
