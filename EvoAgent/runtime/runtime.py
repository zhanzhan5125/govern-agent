"""EvoAgent's dependency-free durable workflow runtime and tool registry.

The runtime deliberately separates orchestration from agent behaviour:

* ``AgentRuntime`` executes named nodes with budgets, retry policy, cancellation
  checks and application-owned checkpoints.
Tool-using model loops live in ``agentic_core.BoundedRole``. Persistence remains
in the application store so a worker restart does not depend on a
framework-owned checkpoint format.
"""
from contextlib import nullcontext
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from evoagent.runtime.hooks import HookContext, HookPipeline, HookPoint
from evoagent.runtime.journal import EffectExecutor, semantic_call_key


class RuntimeBudgetExceeded(RuntimeError):
    """The configured step or wall-clock budget was exhausted."""


class RuntimeCancelled(RuntimeError):
    """The owning task requested cancellation."""


class ToolProtocolError(RuntimeError):
    """A tool request does not match the registered tool contract."""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    side_effect: bool = False
    requires_approval: bool = False
    version: str = "1"

    def catalog_entry(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Explicit tool catalog with JSON-schema-like argument validation."""

    def __init__(
        self, tools: Iterable[AgentTool] = (), hooks: Optional[HookPipeline] = None,
        runtime_context: Optional[Dict[str, Any]] = None, store=None,
    ):
        self._tools: Dict[str, AgentTool] = {}
        self.hooks = hooks or HookPipeline()
        self.runtime_context = dict(runtime_context or {})
        self.store = store
        for tool in tools:
            self.register(tool)

    def configure_runtime(
        self, runtime_context: Optional[Dict[str, Any]] = None,
        hooks: Optional[HookPipeline] = None, store=None,
    ) -> "ToolRegistry":
        self.runtime_context.update(runtime_context or {})
        if hooks is not None:
            self.hooks = hooks
        if store is not None:
            self.store = store
        return self

    def register(self, tool: AgentTool) -> None:
        if not tool.name or tool.name in self._tools:
            raise ValueError("tool names must be non-empty and unique")
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return sorted(self._tools)

    def catalog(self) -> List[Dict[str, Any]]:
        return [self._tools[name].catalog_entry() for name in self.names()]

    def _append_event(
        self, kind: str, semantic_key: str = "", detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        task_id = str(self.runtime_context.get("task_id", ""))
        writer = getattr(self.store, "append_runtime_event", None)
        if writer is not None and task_id:
            writer(
                task_id, kind, str(self.runtime_context.get("node", "agent-loop")),
                int(self.runtime_context.get("step", 0) or 0),
                int(self.runtime_context.get("attempt", 0) or 0),
                semantic_key, dict(detail or {}),
            )
            from evoagent.infrastructure.metrics import metrics
            metrics.inc("runtime_events_total")
            metrics.inc("runtime_%s_total" % kind.lower())
            degraded = list((detail or {}).get("degraded_hooks") or [])
            if degraded:
                metrics.inc("runtime_hook_degradations_total", len(degraded))

    def dispatch_model(
        self, point: HookPoint, payload: Dict[str, Any], role: str = "",
        step: int = 0,
    ) -> Dict[str, Any]:
        if point not in {HookPoint.MODEL_BEFORE, HookPoint.MODEL_AFTER, HookPoint.MODEL_ERROR}:
            raise ValueError("invalid model hook point")
        task_id = str(self.runtime_context.get("task_id", ""))
        key = semantic_call_key(
            task_id, "model", {"step": step},
            str(self.runtime_context.get("node", "agent-loop")),
            role or str(self.runtime_context.get("role", "")),
        )
        context = HookContext(
            point, task_id, str(self.runtime_context.get("tenant_id", "default")),
            str(self.runtime_context.get("repository", "")),
            role or str(self.runtime_context.get("role", "")),
            str(self.runtime_context.get("node", "agent-loop")), step,
            int(self.runtime_context.get("attempt", 0) or 0), key,
            dict(payload), {"operation": "model"},
        )
        dispatch = self.hooks.dispatch(context)
        self._append_event(point.value, key, {
            "role": context.role, "hooks": list(dispatch.executed),
            "degraded_hooks": list(dispatch.degraded),
        })
        return dict(dispatch.context.payload)

    def invoke(
        self, name: str, arguments: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolProtocolError("unknown agent tool: %s" % name)
        self._validate(tool.parameters, arguments)
        runtime = dict(self.runtime_context)
        runtime.update(context or {})
        task_id = str(runtime.get("task_id", ""))
        node = str(runtime.get("node", "agent-loop"))
        role = str(runtime.get("role", ""))
        key = semantic_call_key(task_id, name, arguments, node, role, tool.version)
        metadata = {
            "operation": "tool", "tool": name,
            "side_effect": tool.side_effect,
            "requires_approval": tool.requires_approval,
            "approval": dict(runtime.get("approval") or {}),
            "allowed_tools": self.names(),
            "durable_effect_tracking": bool(
                task_id and self.store is not None
                and getattr(self.store, "claim_runtime_effect", None)
            ),
        }
        hook_context = HookContext(
            HookPoint.TOOL_BEFORE, task_id,
            str(runtime.get("tenant_id", "default")),
            str(runtime.get("repository", "")), role, node,
            int(runtime.get("step", 0) or 0), int(runtime.get("attempt", 0) or 0),
            key, {"arguments": dict(arguments)}, metadata,
        )
        try:
            before = self.hooks.dispatch(hook_context)
            effective_arguments = dict(before.context.payload.get("arguments") or {})
            self._validate(tool.parameters, effective_arguments)
            self._append_event(HookPoint.TOOL_BEFORE.value, key, {
                "tool": name, "role": role, "hooks": list(before.executed),
                "degraded_hooks": list(before.degraded),
            })

            def call_handler():
                return tool.handler(**effective_arguments)

            if tool.side_effect and self.store is not None and task_id:
                effect = EffectExecutor(self.store).execute_once(
                    task_id, name, effective_arguments, call_handler,
                    node=node, role=role, version=tool.version,
                )
                value = effect.result
                reused = effect.reused
            else:
                value, reused = call_handler(), False
            after_context = HookContext(
                HookPoint.TOOL_AFTER, task_id, hook_context.tenant_id,
                hook_context.repository, role, node, hook_context.step,
                hook_context.attempt, key,
                {"arguments": effective_arguments, "result": value}, metadata,
            )
            after = self.hooks.dispatch(after_context)
            value = after.context.payload.get("result")
            detail = {
                "tool": name, "role": role, "reused": reused,
                "hooks": list(after.executed),
                "degraded_hooks": list(after.degraded),
            }
            artifact = after.context.payload.get("artifact")
            if artifact:
                detail["artifact"] = artifact
            evidence = after.context.payload.get("evidence")
            if evidence:
                detail["evidence"] = evidence
            self._append_event(HookPoint.TOOL_AFTER.value, key, detail)
            return value
        except Exception as exc:
            error_context = HookContext(
                HookPoint.TOOL_ERROR, task_id, hook_context.tenant_id,
                hook_context.repository, role, node, hook_context.step,
                hook_context.attempt, key,
                {"arguments": dict(arguments), "error": str(exc)[:1000]}, metadata,
            )
            try:
                failed = self.hooks.dispatch(error_context)
                hook_detail = {
                    "hooks": list(failed.executed),
                    "degraded_hooks": list(failed.degraded),
                }
            except Exception as hook_exc:
                hook_detail = {"error_hook_failure": str(hook_exc)[:1000]}
            self._append_event(HookPoint.TOOL_ERROR.value, key, {
                "tool": name, "role": role, "error": str(exc)[:1000], **hook_detail,
            })
            raise

    @staticmethod
    def _validate(schema: Dict[str, Any], arguments: Dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolProtocolError("tool arguments must be an object")
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        missing = required.difference(arguments)
        if missing:
            raise ToolProtocolError(
                "missing required tool arguments: %s" % ", ".join(sorted(missing))
            )
        if schema.get("additionalProperties", False) is False:
            unknown = set(arguments).difference(properties)
            if unknown:
                raise ToolProtocolError(
                    "unknown tool arguments: %s" % ", ".join(sorted(unknown))
                )
        expected_types = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "object": dict, "array": list,
        }
        for key, value in arguments.items():
            spec = properties.get(key) or {}
            expected = expected_types.get(spec.get("type"))
            if expected and (not isinstance(value, expected) or (
                spec.get("type") in {"integer", "number"} and isinstance(value, bool)
            )):
                raise ToolProtocolError(
                    "tool argument %s must be %s" % (key, spec.get("type"))
                )
            if isinstance(value, (int, float)):
                if "minimum" in spec and value < spec["minimum"]:
                    raise ToolProtocolError("tool argument %s is below minimum" % key)
                if "maximum" in spec and value > spec["maximum"]:
                    raise ToolProtocolError("tool argument %s exceeds maximum" % key)


@dataclass(frozen=True)
class RuntimeNode:
    name: str
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    retries: Optional[int] = None
    checkpoint: bool = True
    counts_toward_budget: bool = True


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    node: str
    step: int
    attempt: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    semantic_key: str = ""


class AgentRuntime:
    """Execute a bounded node graph without a third-party orchestration engine."""

    def __init__(
        self, max_steps: int = 8, timeout_seconds: int = 120,
        node_retries: int = 0, hooks: Optional[HookPipeline] = None,
    ):
        if max_steps < 1:
            raise ValueError("runtime max_steps must be at least 1")
        if timeout_seconds < 1:
            raise ValueError("runtime timeout_seconds must be at least 1")
        if node_retries < 0:
            raise ValueError("runtime node_retries cannot be negative")
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries
        self.hooks = hooks or HookPipeline()

    def execute(
        self, initial_state: Dict[str, Any], nodes: Iterable[RuntimeNode],
        task_id: str = "", checkpoint_store=None,
        cancel_check: Optional[Callable[[], bool]] = None,
        event_sink: Optional[Callable[[RuntimeEvent], None]] = None,
        span_factory: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        non_retryable: Tuple[type, ...] = (ValueError, RuntimeCancelled, RuntimeBudgetExceeded),
        hooks: Optional[HookPipeline] = None,
    ) -> Dict[str, Any]:
        state = dict(initial_state)
        started = time.monotonic()
        steps = 0
        checkpoints = (
            checkpoint_store.load_checkpoints(task_id)
            if checkpoint_store is not None and task_id else {}
        )

        pipeline = hooks or self.hooks

        def emit(
            kind: str, node: str, attempt: int = 0, semantic_key: str = "", **detail
        ) -> None:
            sequence = 0
            writer = getattr(checkpoint_store, "append_runtime_event", None)
            if writer is not None and task_id:
                sequence = int(writer(
                    task_id, kind, node, steps, attempt, semantic_key, detail,
                ) or 0)
            if event_sink:
                event_sink(RuntimeEvent(
                    kind, node, steps, attempt, detail, sequence, semantic_key,
                ))

        def dispatch(
            point: HookPoint, node: str, attempt: int = 0,
            payload: Optional[Dict[str, Any]] = None,
            semantic_key: str = "", **metadata,
        ):
            context = HookContext(
                point, task_id, str(state.get("tenant_id", "default")),
                str(state.get("repository", "")), str(metadata.pop("role", "")),
                node, steps, attempt, semantic_key, dict(payload or {}), metadata,
            )
            result = pipeline.dispatch(context)
            return result

        def guard(node: str, counts_toward_budget: bool = True) -> None:
            if cancel_check and cancel_check():
                emit("RUNTIME_CANCELLED", node)
                raise RuntimeCancelled("Task was cancelled")
            if (
                (counts_toward_budget and steps >= self.max_steps)
                or time.monotonic() - started > self.timeout_seconds
            ):
                emit("RUNTIME_BUDGET_EXHAUSTED", node)
                raise RuntimeBudgetExceeded("task execution budget exceeded")

        started_hooks = dispatch(
            HookPoint.RUN_START, "runtime", payload={"state": dict(state)},
        )
        state.update(dict(started_hooks.context.payload.get("state") or state))
        emit(
            HookPoint.RUN_START.value, "runtime",
            hooks=list(started_hooks.executed),
            degraded_hooks=list(started_hooks.degraded),
        )

        try:
            for node in nodes:
                cached = checkpoints.get(node.name) if node.checkpoint else None
                if cached and cached.get("status") == "completed":
                    output = dict(cached.get("state") or {})
                    state.update(output)
                    restored = dispatch(
                        HookPoint.CHECKPOINT_RESTORED, node.name,
                        int(cached.get("attempt", 0)), payload={"state": output},
                    )
                    emit(
                        HookPoint.CHECKPOINT_RESTORED.value, node.name,
                        int(cached.get("attempt", 0)),
                        hooks=list(restored.executed),
                        degraded_hooks=list(restored.degraded),
                    )
                    continue

                retries = self.node_retries if node.retries is None else node.retries
                previous_attempt = int((cached or {}).get("attempt", 0))
                last_error: Optional[Exception] = None
                for offset in range(1, retries + 2):
                    guard(node.name, node.counts_toward_budget)
                    if node.counts_toward_budget:
                        steps += 1
                    attempt = previous_attempt + offset
                    key = semantic_call_key(task_id, "runtime-node", {}, node.name)
                    before = dispatch(
                        HookPoint.NODE_BEFORE, node.name, attempt,
                        payload={"state": dict(state)}, semantic_key=key,
                    )
                    handler_state = dict(before.context.payload.get("state") or state)
                    emit(
                        HookPoint.NODE_BEFORE.value, node.name, attempt, key,
                        hooks=list(before.executed),
                        degraded_hooks=list(before.degraded),
                    )
                    try:
                        attrs = {
                            "task_id": task_id, "node": node.name,
                            "attempt": attempt, "runtime_step": steps,
                            "semantic_key": key,
                        }
                        context = (
                            span_factory("runtime.%s" % node.name, attrs)
                            if span_factory else nullcontext()
                        )
                        with context:
                            output = node.handler(handler_state) or {}
                        if not isinstance(output, dict):
                            raise TypeError("runtime node %s must return a dict" % node.name)
                        after = dispatch(
                            HookPoint.NODE_AFTER, node.name, attempt,
                            payload={"output": output}, semantic_key=key,
                        )
                        output = dict(after.context.payload.get("output") or {})
                        state.update(output)
                        if checkpoint_store is not None and task_id and node.checkpoint:
                            checkpoint_store.save_checkpoint(
                                task_id, node.name, output, "completed", attempt
                            )
                            saved = dispatch(
                                HookPoint.CHECKPOINT_SAVED, node.name, attempt,
                                payload={"state": output, "status": "completed"},
                                semantic_key=key,
                            )
                            emit(
                                HookPoint.CHECKPOINT_SAVED.value, node.name, attempt, key,
                                status="completed", hooks=list(saved.executed),
                                degraded_hooks=list(saved.degraded),
                            )
                        emit(
                            HookPoint.NODE_AFTER.value, node.name, attempt, key,
                            output_keys=sorted(output), hooks=list(after.executed),
                            degraded_hooks=list(after.degraded),
                        )
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        error_hooks = dispatch(
                            HookPoint.NODE_ERROR, node.name, attempt,
                            payload={"error": str(exc)[:1000]}, semantic_key=key,
                            retrying=offset <= retries,
                        )
                        if checkpoint_store is not None and task_id and node.checkpoint:
                            checkpoint_store.save_checkpoint(
                                task_id, node.name, {}, "failed", attempt, str(exc)
                            )
                        emit(
                            HookPoint.NODE_ERROR.value, node.name, attempt, key,
                            error=str(exc)[:1000], will_retry=offset <= retries,
                            hooks=list(error_hooks.executed),
                            degraded_hooks=list(error_hooks.degraded),
                        )
                        if isinstance(exc, non_retryable):
                            raise
                if last_error is not None:
                    raise last_error
        except Exception as exc:
            stopped = dispatch(
                HookPoint.RUN_STOP, "runtime", payload={
                    "state": dict(state), "ok": False, "error": str(exc)[:1000],
                },
            )
            emit(
                HookPoint.RUN_STOP.value, "runtime", ok=False,
                error=str(exc)[:1000], hooks=list(stopped.executed),
                degraded_hooks=list(stopped.degraded),
            )
            raise
        stopped = dispatch(
            HookPoint.RUN_STOP, "runtime", payload={"state": dict(state), "ok": True},
        )
        state.update(dict(stopped.context.payload.get("state") or state))
        emit(
            HookPoint.RUN_STOP.value, "runtime", ok=True,
            hooks=list(stopped.executed), degraded_hooks=list(stopped.degraded),
        )
        return state
