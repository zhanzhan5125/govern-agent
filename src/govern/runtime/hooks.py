"""Ordered runtime hooks for deterministic cross-cutting policy enforcement.

Hooks are deliberately small and framework-owned.  Agent prompts may explain a
policy, but only hooks can block or pause an operation before the handler runs.
"""
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


class HookPoint(str, Enum):
    RUN_START = "RUN_START"
    RUN_STOP = "RUN_STOP"
    NODE_BEFORE = "NODE_BEFORE"
    NODE_AFTER = "NODE_AFTER"
    NODE_ERROR = "NODE_ERROR"
    MODEL_BEFORE = "MODEL_BEFORE"
    MODEL_AFTER = "MODEL_AFTER"
    MODEL_ERROR = "MODEL_ERROR"
    TOOL_BEFORE = "TOOL_BEFORE"
    TOOL_AFTER = "TOOL_AFTER"
    TOOL_ERROR = "TOOL_ERROR"
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    CHECKPOINT_RESTORED = "CHECKPOINT_RESTORED"
    GOAL_CHECK_BEFORE = "GOAL_CHECK_BEFORE"
    GOAL_CHECK_AFTER = "GOAL_CHECK_AFTER"


class HookAction(str, Enum):
    CONTINUE = "continue"
    MODIFY = "modify"
    BLOCK = "block"
    PAUSE_FOR_APPROVAL = "pause_for_approval"


class RuntimeHookError(RuntimeError):
    """A fail-closed hook could not enforce its policy."""


class RuntimeHookBlocked(RuntimeHookError):
    """A hook explicitly denied an operation."""


class RuntimeApprovalRequired(RuntimeHookError):
    """A hook paused an operation until an external approval is recorded."""

    def __init__(self, message: str, approval: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.approval = dict(approval or {})


@dataclass(frozen=True)
class HookContext:
    point: HookPoint
    task_id: str = ""
    tenant_id: str = "default"
    repository: str = ""
    role: str = ""
    node: str = ""
    step: int = 0
    attempt: int = 0
    semantic_key: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def with_payload(self, payload: Dict[str, Any]) -> "HookContext":
        return replace(self, payload=dict(payload))


@dataclass(frozen=True)
class HookResult:
    action: HookAction = HookAction.CONTINUE
    payload: Optional[Dict[str, Any]] = None
    reason: str = ""
    approval: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookRegistration:
    name: str
    callback: Callable[[HookContext], Optional[HookResult]]
    points: Tuple[HookPoint, ...]
    priority: int = 100
    fail_open: bool = False


@dataclass(frozen=True)
class HookDispatch:
    context: HookContext
    executed: Tuple[str, ...] = ()
    degraded: Tuple[Dict[str, str], ...] = ()


class HookPipeline:
    """Run registered hooks in stable priority/name order.

    Read-only enrichment hooks may opt into ``fail_open``.  Policy hooks are
    fail-closed by default, so a broken guard cannot silently permit a write.
    """

    def __init__(self, registrations: Iterable[HookRegistration] = ()):
        self._registrations: List[HookRegistration] = []
        for registration in registrations:
            self.register(registration)

    def register(self, registration: HookRegistration) -> None:
        if not registration.name.strip():
            raise ValueError("hook name is required")
        if not registration.points:
            raise ValueError("hook must subscribe to at least one point")
        if any(item.name == registration.name for item in self._registrations):
            raise ValueError("hook names must be unique")
        self._registrations.append(registration)
        self._registrations.sort(key=lambda item: (item.priority, item.name))

    def names(self) -> List[str]:
        return [item.name for item in self._registrations]

    def dispatch(self, context: HookContext) -> HookDispatch:
        current = context
        executed: List[str] = []
        degraded: List[Dict[str, str]] = []
        for registration in self._registrations:
            if current.point not in registration.points:
                continue
            try:
                result = registration.callback(current) or HookResult()
                if not isinstance(result, HookResult):
                    raise TypeError("hook must return HookResult or None")
            except Exception as exc:
                if registration.fail_open:
                    degraded.append({
                        "hook": registration.name,
                        "error": str(exc)[:1000],
                    })
                    continue
                raise RuntimeHookError(
                    "hook %s failed closed: %s" % (registration.name, exc)
                ) from exc
            executed.append(registration.name)
            if result.action == HookAction.BLOCK:
                raise RuntimeHookBlocked(
                    result.reason or "hook %s blocked the operation" % registration.name
                )
            if result.action == HookAction.PAUSE_FOR_APPROVAL:
                raise RuntimeApprovalRequired(
                    result.reason or "hook %s requires approval" % registration.name,
                    result.approval,
                )
            if result.action == HookAction.MODIFY:
                if result.payload is None or not isinstance(result.payload, dict):
                    raise RuntimeHookError(
                        "hook %s returned MODIFY without an object payload"
                        % registration.name
                    )
                current = current.with_payload(result.payload)
        return HookDispatch(current, tuple(executed), tuple(degraded))


class ApprovalHook:
    """Block marked operations unless an application-owned approval is present."""

    def __init__(self, reader=None, requester=None):
        self.reader = reader
        self.requester = requester

    def __call__(self, context: HookContext) -> HookResult:
        if not bool(context.metadata.get("requires_approval")):
            return HookResult()
        approval = dict(context.metadata.get("approval") or {})
        if not approval and self.reader is not None:
            approval = dict(self.reader(context.task_id, context.semantic_key) or {})
        status = str(approval.get("status", "")).upper()
        if approval.get("approved") is True or status == "APPROVED":
            return HookResult()
        if status == "DENIED":
            return HookResult(
                HookAction.BLOCK,
                reason="operation approval was denied: %s"
                % str(approval.get("reason", "policy decision")),
            )
        request = {
            "semantic_key": context.semantic_key,
            "tool": context.metadata.get("tool", ""),
            "arguments": dict(context.payload.get("arguments") or {}),
        }
        if self.requester is not None and context.task_id:
            self.requester(context.task_id, context.semantic_key, request)
        return HookResult(
            HookAction.PAUSE_FOR_APPROVAL,
            reason="operation requires explicit approval",
            approval=request,
        )


class ToolPermissionHook:
    """Enforce catalog membership and durable tracking for mutating tools."""

    def __call__(self, context: HookContext) -> HookResult:
        tool = str(context.metadata.get("tool", ""))
        allowed = set(context.metadata.get("allowed_tools") or ())
        if allowed and tool not in allowed:
            return HookResult(
                HookAction.BLOCK, reason="tool is not allowed for this runtime role"
            )
        if context.metadata.get("side_effect") and not context.metadata.get(
            "durable_effect_tracking"
        ):
            return HookResult(
                HookAction.BLOCK,
                reason="side-effecting tool requires a task-scoped durable effect ledger",
            )
        return HookResult()
