"""Checkpointed review workflow powered by EvoAgent's own runtime."""
import threading
from typing import Any, Dict, Optional, TypedDict

from govern.runtime.artifacts import ArtifactOffloadHook, ArtifactStore
from govern.core.diff_parser import ParsedDiff, parse_unified_diff
from govern.runtime.evidence import EvidenceCaptureHook, EvidenceStore
from govern.runtime.goal_gate import ReviewGoalGate, RuntimeGoalNotMet
from govern.runtime.hooks import (
    ApprovalHook, HookContext, HookPipeline, HookPoint, HookRegistration,
    RuntimeApprovalRequired,
    ToolPermissionHook,
)
from govern.core.models import ChangedLine, Finding, ReviewReport, Severity, TaskState, TraceEvent
from govern.infrastructure.metrics import metrics
from govern.review.reviewer import Reviewer
from govern.runtime.runtime import (
    AgentRuntime, RuntimeBudgetExceeded, RuntimeCancelled, RuntimeNode,
)
from govern.storage.store import TaskStore, utc_now


ALLOWED = {
    TaskState.PENDING: {
        TaskState.PLANNING, TaskState.WAITING_APPROVAL,
        TaskState.FAILED, TaskState.CANCELLED,
    },
    TaskState.PLANNING: {
        TaskState.EXECUTING, TaskState.WAITING_APPROVAL,
        TaskState.FAILED, TaskState.CANCELLED,
    },
    TaskState.EXECUTING: {
        TaskState.REVIEWING, TaskState.WAITING_APPROVAL,
        TaskState.FAILED, TaskState.CANCELLED,
    },
    TaskState.REVIEWING: {
        TaskState.SUCCESS, TaskState.WAITING_APPROVAL,
        TaskState.FAILED, TaskState.CANCELLED,
    },
    TaskState.WAITING_APPROVAL: {
        TaskState.PLANNING, TaskState.EXECUTING, TaskState.REVIEWING,
        TaskState.FAILED, TaskState.CANCELLED,
    },
}


class RuntimeState(TypedDict, total=False):
    task_id: str
    repository: str
    pull_request: Optional[int]
    tenant_id: str
    diff: str
    parsed: Dict[str, Any]
    findings: list
    report: Dict[str, Any]


BudgetExceeded = RuntimeBudgetExceeded
TaskCancelled = RuntimeCancelled


class ReviewHarness:
    node_order = ("planning", "executing", "reviewing", "goal-gate")

    def __init__(
        self, store: TaskStore, reviewer: Reviewer, max_steps: int = 8,
        timeout_seconds: int = 120, node_retries: int = 2, observability=None,
        hooks: Optional[HookPipeline] = None, artifact_threshold_bytes: int = 32_768,
        artifact_store: Optional[ArtifactStore] = None,
    ):
        self.store = store
        self.reviewer = reviewer
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries
        self.observability = observability
        self.name = "evoagent-runtime"
        self._ctx = threading.local()
        self.artifact_threshold_bytes = max(1024, int(artifact_threshold_bytes))
        self.artifacts = artifact_store or ArtifactStore(store)
        self.evidence = EvidenceStore(self.artifacts)
        self.hooks = hooks or HookPipeline()
        if "runtime-tool-permission" not in self.hooks.names():
            self.hooks.register(HookRegistration(
                "runtime-tool-permission", ToolPermissionHook(),
                (HookPoint.TOOL_BEFORE,), priority=0, fail_open=False,
            ))
        if "runtime-approval" not in self.hooks.names():
            self.hooks.register(HookRegistration(
                "runtime-approval", ApprovalHook(
                    reader=getattr(store, "get_runtime_approval", None),
                    requester=getattr(store, "request_runtime_approval", None),
                ), (HookPoint.TOOL_BEFORE,),
                priority=10, fail_open=False,
            ))
        if "evidence-capture" not in self.hooks.names():
            self.hooks.register(HookRegistration(
                "evidence-capture", EvidenceCaptureHook(self.evidence),
                (HookPoint.TOOL_AFTER,), priority=150, fail_open=True,
            ))
        if "artifact-offload" not in self.hooks.names():
            self.hooks.register(HookRegistration(
                "artifact-offload",
                ArtifactOffloadHook(self.artifacts, self.artifact_threshold_bytes),
                (HookPoint.TOOL_AFTER,), priority=200, fail_open=True,
            ))
        self.goal_gate = ReviewGoalGate()
        self.runtime = AgentRuntime(
            max_steps, timeout_seconds, node_retries, hooks=self.hooks,
        )
        configure_runtime = getattr(self.reviewer, "configure_runtime", None)
        if configure_runtime:
            configure_runtime(self.hooks)
            if hasattr(self.reviewer, "artifact_store"):
                self.reviewer.artifact_store = self.artifacts

    def run(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
        tenant_id: str = "default",
    ) -> ReviewReport:
        task = self.store.get(task_id)
        if task and task.get("state") == TaskState.SUCCESS.value and task.get("report"):
            return self._report_from_dict(task["report"])
        state: RuntimeState = {
            "task_id": task_id, "repository": repository,
            "pull_request": pull_request, "diff": diff, "tenant_id": tenant_id,
        }
        self._ctx.step = max([item["step"] for item in (task or {}).get("trace", [])] or [0])
        self._ctx.task_id = task_id
        checkpoints = self.store.load_checkpoints(task_id)
        self._ctx.state = TaskState.PENDING
        if checkpoints.get("planning", {}).get("status") == "completed":
            self._ctx.state = TaskState.PLANNING
        if checkpoints.get("executing", {}).get("status") == "completed":
            self._ctx.state = TaskState.EXECUTING
        if checkpoints.get("reviewing", {}).get("status") == "completed":
            self._ctx.state = TaskState.REVIEWING
        try:
            with metrics.timer("runtime_harness_duration"):
                result = self.runtime.execute(
                    state,
                    [
                        RuntimeNode("planning", self._planning),
                        RuntimeNode("executing", self._executing),
                        RuntimeNode("reviewing", self._reviewing),
                        RuntimeNode(
                            "goal-gate", self._goal_check, retries=0,
                            counts_toward_budget=False,
                        ),
                    ],
                    task_id=task_id, checkpoint_store=self.store,
                    cancel_check=lambda: self.store.is_cancelled(task_id),
                    event_sink=self._runtime_event,
                    span_factory=self._span,
                )
            report = self._report_from_dict(result["report"])
            self._ctx.step += 1
            self.store.succeed(
                task_id, report,
                TraceEvent(self._ctx.step, TaskState.SUCCESS, "Review completed", utc_now()),
            )
            return report
        except TaskCancelled as exc:
            self._ctx.step += 1
            self.store.cancel(
                task_id, TraceEvent(self._ctx.step, TaskState.CANCELLED, str(exc), utc_now())
            )
            raise
        except RuntimeApprovalRequired as exc:
            metrics.inc("runtime_approval_pauses_total")
            self._ctx.step += 1
            self._ctx.state = TaskState.WAITING_APPROVAL
            self.store.transition(
                task_id, TraceEvent(
                    self._ctx.step, TaskState.WAITING_APPROVAL,
                    "Runtime paused for approval: %s" % exc, utc_now(),
                ),
            )
            raise
        except Exception as exc:
            self._ctx.step += 1
            self.store.fail(
                task_id, str(exc),
                TraceEvent(self._ctx.step, TaskState.FAILED, "Review failed: %s" % exc, utc_now()),
            )
            try:
                self.store.record_failure_case(
                    task_id, "execution_error", {"error": str(exc)[:1000]}
                )
            except Exception:
                pass
            raise

    def resume(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
        tenant_id: str = "default",
    ) -> ReviewReport:
        return self.run(task_id, repository, pull_request, diff, tenant_id)

    def _planning(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = parse_unified_diff(state["diff"])
        if not parsed.files and not parsed.added_lines:
            raise ValueError("diff does not contain a valid unified diff with added lines")
        self._transition(TaskState.PLANNING, "Input accepted; preparing review plan")
        return {"parsed": self._serialize_parsed(parsed)}

    def _executing(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = self._deserialize_parsed(state["parsed"])
        self._transition(
            TaskState.EXECUTING, "Reviewing %d changed files" % len(parsed.files)
        )
        contextual = getattr(self.reviewer, "review_with_context", None)
        findings = (
            contextual(
                state["task_id"], state["diff"], parsed,
                repository=state["repository"], tenant_id=state.get("tenant_id", "default"),
            )
            if contextual else self.reviewer.review(state["diff"], parsed)
        )
        return {"findings": [item.to_dict() for item in findings]}

    def _reviewing(self, state: RuntimeState) -> Dict[str, Any]:
        parsed = self._deserialize_parsed(state["parsed"])
        findings = [self._finding_from_dict(item) for item in state["findings"]]
        self._transition(
            TaskState.REVIEWING, "Validating and ranking %d findings" % len(findings)
        )
        risk = self._risk(findings)
        summary_reader = getattr(self.reviewer, "collaboration_summary", None)
        reviewer_summary = summary_reader(state["task_id"]) if summary_reader else {}
        if reviewer_summary and "run_mode" in reviewer_summary:
            collaboration = dict(reviewer_summary.get("collaboration") or {})
            run_mode = dict(reviewer_summary.get("run_mode") or {})
            components = list(reviewer_summary.get("components") or [])
            execution = dict(reviewer_summary.get("execution") or {})
            execution["gates"] = reviewer_summary.get("gates") or {}
            execution["rejected_findings"] = reviewer_summary.get("rejected_findings") or []
            execution["repository_context"] = reviewer_summary.get("repository_context") or {}
        else:
            collaboration = reviewer_summary or self._persisted_collaboration_summary(state["task_id"])
            run_mode, components, execution = {}, [], {}
        report = ReviewReport(
            repository=state["repository"], pull_request=state.get("pull_request"),
            summary=self._summary(findings, len(parsed.files), risk), risk=risk,
            findings=findings, files_reviewed=parsed.files, reviewer=self.reviewer.name,
            collaboration=collaboration,
            run_mode=run_mode, components=components, execution=execution,
        )
        return {"report": report.to_dict()}

    def _goal_check(self, state: RuntimeState) -> Dict[str, Any]:
        """Run deterministic completion checks after the model workflow stops."""
        task_id = state["task_id"]
        before_context = HookContext(
            HookPoint.GOAL_CHECK_BEFORE, task_id,
            str(state.get("tenant_id", "default")), str(state.get("repository", "")),
            node="goal-gate", payload={"report": dict(state.get("report") or {})},
        )
        before = self.hooks.dispatch(before_context)
        self.store.append_runtime_event(
            task_id, HookPoint.GOAL_CHECK_BEFORE.value, "goal-gate",
            detail={
                "hooks": list(before.executed),
                "degraded_hooks": list(before.degraded),
            },
        )
        candidate_state = dict(state)
        candidate_state["report"] = dict(
            before.context.payload.get("report") or state.get("report") or {}
        )
        result = self.goal_gate.evaluate(
            candidate_state, self.store.load_checkpoints(task_id),
        )
        after_context = HookContext(
            HookPoint.GOAL_CHECK_AFTER, task_id,
            str(state.get("tenant_id", "default")), str(state.get("repository", "")),
            node="goal-gate", payload={"result": result.to_dict()},
        )
        after = self.hooks.dispatch(after_context)
        # The independent gate is authoritative.  Observation hooks may record
        # or block the decision, but cannot rewrite an incomplete result to pass.
        result_payload = result.to_dict()
        self.store.append_runtime_event(
            task_id, HookPoint.GOAL_CHECK_AFTER.value, "goal-gate",
            detail={
                **result_payload, "hooks": list(after.executed),
                "degraded_hooks": list(after.degraded),
            },
        )
        if not result_payload.get("complete"):
            metrics.inc("runtime_goal_gate_failed_total")
            raise RuntimeGoalNotMet(result_payload.get("requirements") or ())
        metrics.inc("runtime_goal_gate_passed_total")
        report = dict(candidate_state["report"])
        execution = dict(report.get("execution") or {})
        execution["runtime_harness"] = {
            "schema_version": 2,
            "runtime": "event-driven-checkpoint-journal",
            "hooks": self.hooks.names(),
            "journal": {"enabled": True, "append_only": True},
            "checkpoint": {"node_level": True, "compatible_with_schema_v1": True},
            "side_effects": {"semantic_idempotency": True, "leased_intents": True},
            "artifacts": {
                "content_addressed": True,
                "offload_threshold_bytes": self.artifact_threshold_bytes,
            },
            "goal_gate": result_payload,
        }
        report["execution"] = execution
        return {"report": report, "goal_gate": result_payload}

    def _transition(self, target: TaskState, message: str) -> None:
        if target == self._ctx.state:
            return
        if target not in ALLOWED.get(self._ctx.state, set()):
            raise RuntimeError(
                "invalid state transition: %s -> %s" % (self._ctx.state.value, target.value)
            )
        self._ctx.step += 1
        self._ctx.state = target
        self.store.transition(
            self._ctx.task_id,
            TraceEvent(self._ctx.step, target, message, utc_now()),
        )

    def _span(self, name: str, attributes: Dict[str, Any]):
        if self.observability:
            return self.observability.span(
                name, str(attributes.get("task_id", "")), **attributes
            )
        from contextlib import nullcontext
        return nullcontext()

    @staticmethod
    def _runtime_event(event) -> None:
        metrics.inc("runtime_events_total")
        metrics.inc("runtime_%s_total" % str(event.kind).lower())
        if event.kind == HookPoint.CHECKPOINT_RESTORED.value:
            metrics.inc("runtime_checkpoint_restores_total")
        degraded = list((event.detail or {}).get("degraded_hooks") or [])
        if degraded:
            metrics.inc("runtime_hook_degradations_total", len(degraded))

    @staticmethod
    def _serialize_parsed(parsed: ParsedDiff) -> Dict[str, Any]:
        return {
            "files": parsed.files,
            "added_lines": [
                {"path": item.path, "line": item.line, "content": item.content}
                for item in parsed.added_lines
            ],
        }

    @staticmethod
    def _deserialize_parsed(value: Dict[str, Any]) -> ParsedDiff:
        return ParsedDiff(
            list(value["files"]), [ChangedLine(**item) for item in value["added_lines"]]
        )

    @staticmethod
    def _finding_from_dict(value: Dict[str, Any]) -> Finding:
        item = dict(value)
        item["severity"] = Severity(item["severity"])
        return Finding(**item)

    @classmethod
    def _report_from_dict(cls, value: Dict[str, Any]) -> ReviewReport:
        return ReviewReport(
            repository=value["repository"], pull_request=value.get("pull_request"),
            summary=value["summary"], risk=value["risk"],
            findings=[cls._finding_from_dict(item) for item in value.get("findings", [])],
            files_reviewed=list(value.get("files_reviewed", [])),
            reviewer=value.get("reviewer", "unknown"),
            collaboration=dict(value.get("collaboration", {})),
            run_mode=dict(value.get("run_mode", {})),
            components=list(value.get("components", [])),
            execution=dict(value.get("execution", {})),
        )

    @staticmethod
    def _risk(findings) -> str:
        severities = {item.severity for item in findings}
        if Severity.CRITICAL in severities or Severity.HIGH in severities:
            return "high"
        if Severity.MEDIUM in severities:
            return "medium"
        return "low"

    @staticmethod
    def _summary(findings, file_count: int, risk: str) -> str:
        if not findings:
            return "Reviewed %d file(s); no actionable issue was detected in added lines." % file_count
        return "Reviewed %d file(s); found %d actionable issue(s). Overall risk: %s." % (
            file_count, len(findings), risk,
        )

    def _persisted_collaboration_summary(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get(task_id) or {}
        messages = task.get("collaboration", [])
        if not messages:
            return {}
        kinds = [item.get("kind", "") for item in messages]
        roles = sorted({
            value for item in messages
            for value in (item.get("sender", ""), item.get("recipient", ""))
            if value and value not in {"all", "review-report"}
        })
        rounds = [
            int((item.get("content") or {}).get("round", 0))
            for item in messages
            if isinstance(item.get("content"), dict)
        ]
        final = next((
            item.get("content") or {} for item in reversed(messages)
            if item.get("kind") == "arbitration_decision"
        ), {})
        return {
            "protocol": "plan-challenge-revise-evidence-verify-arbitrate",
            "roles": roles,
            "planned_assignments": kinds.count("assignment"),
            "dialogue_rounds": max(rounds or [1]),
            "messages": len(messages),
            "retries": kinds.count("retry_request"),
            "handoffs": kinds.count("assignment_handoff"),
            "approved_findings": len(final.get("approved_findings", [])),
            "rejected_findings": len(final.get("rejected_findings", [])),
        }
