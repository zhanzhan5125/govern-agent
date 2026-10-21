"""Append-only runtime journal and semantic side-effect execution helpers."""
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Dict, Optional


def semantic_call_key(
    task_id: str, action: str, arguments: Any = None,
    node: str = "", role: str = "", version: str = "1",
) -> str:
    """Return a stable key shared by retries and process restarts."""
    payload = {
        "task_id": str(task_id), "action": str(action), "arguments": arguments,
        "node": str(node), "role": str(role), "version": str(version),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EffectResult:
    result: Any
    reused: bool
    semantic_key: str


class RuntimeEffectInProgress(RuntimeError):
    """Another worker currently owns the same side-effect lease."""


class EffectExecutor:
    """Execute a side effect once after atomically claiming its semantic key.

    A committed result is returned on replay.  Failed or expired intents can be
    reclaimed by the persistence backend.  External operations should still use
    their native idempotency mechanism (for example an upsert marker), closing the
    unavoidable gap between a remote commit and the local COMMITTED record.
    """

    def __init__(self, store, lease_seconds: int = 120):
        self.store = store
        self.lease_seconds = max(1, int(lease_seconds))

    def execute_once(
        self, task_id: str, action: str, arguments: Dict[str, Any],
        operation: Callable[[], Any], node: str = "", role: str = "",
        version: str = "1",
    ) -> EffectResult:
        key = semantic_call_key(task_id, action, arguments, node, role, version)
        claim = self.store.claim_runtime_effect(
            task_id, key, action, arguments, self.lease_seconds
        )
        if claim["status"] == "COMMITTED":
            from govern.infrastructure.metrics import metrics
            metrics.inc("runtime_side_effects_reused_total")
            return EffectResult(claim.get("result"), True, key)
        if not claim.get("claimed"):
            raise RuntimeEffectInProgress(
                "side effect %s is already in progress" % action
            )
        try:
            result = operation()
        except Exception as exc:
            self.store.fail_runtime_effect(task_id, key, str(exc))
            from govern.infrastructure.metrics import metrics
            metrics.inc("runtime_side_effects_failed_total")
            raise
        self.store.commit_runtime_effect(task_id, key, result)
        from govern.infrastructure.metrics import metrics
        metrics.inc("runtime_side_effects_committed_total")
        return EffectResult(result, False, key)
