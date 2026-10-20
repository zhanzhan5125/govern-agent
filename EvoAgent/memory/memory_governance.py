"""Lifecycle and optimistic-concurrency rules for durable agent memories."""
from enum import Enum
from typing import Any, Dict, Iterable, Optional


class MemoryStatus(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    VERIFIED = "VERIFIED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


RECALLABLE_STATUSES = {MemoryStatus.VERIFIED.value, MemoryStatus.PROMOTED.value}
TERMINAL_STATUSES = {
    MemoryStatus.REJECTED.value,
    MemoryStatus.SUPERSEDED.value,
    MemoryStatus.EXPIRED.value,
}

ALLOWED_TRANSITIONS = {
    MemoryStatus.PROVISIONAL.value: {
        MemoryStatus.VERIFIED.value,
        MemoryStatus.REJECTED.value,
        MemoryStatus.EXPIRED.value,
    },
    MemoryStatus.VERIFIED.value: {
        MemoryStatus.PROMOTED.value,
        MemoryStatus.REJECTED.value,
        MemoryStatus.SUPERSEDED.value,
        MemoryStatus.EXPIRED.value,
    },
    MemoryStatus.PROMOTED.value: {
        MemoryStatus.REJECTED.value,
        MemoryStatus.SUPERSEDED.value,
        MemoryStatus.EXPIRED.value,
    },
    MemoryStatus.REJECTED.value: set(),
    MemoryStatus.SUPERSEDED.value: set(),
    MemoryStatus.EXPIRED.value: set(),
}


class MemoryTransitionError(ValueError):
    pass


class MemoryVersionConflict(RuntimeError):
    pass


class MemoryGovernance:
    """Apply explicit lifecycle gates while leaving persistence to the store."""

    def __init__(self, store, promotion_successes: int = 2):
        self.store = store
        self.promotion_successes = max(2, int(promotion_successes))

    def transition(
        self, memory_id: str, target: str, expected_version: int,
        actor: str = "system", reason: str = "",
    ) -> Dict[str, Any]:
        current = self.store.get_agent_memory(memory_id)
        if current is None:
            raise KeyError("agent memory not found")
        source = str(current.get("status") or MemoryStatus.VERIFIED.value).upper()
        target = str(target).upper()
        if target not in {status.value for status in MemoryStatus}:
            raise MemoryTransitionError("unsupported memory status: %s" % target)
        if target not in ALLOWED_TRANSITIONS.get(source, set()):
            raise MemoryTransitionError("invalid memory transition: %s -> %s" % (source, target))
        if target == MemoryStatus.PROMOTED.value:
            evidence = list(current.get("source_evidence") or [])
            if current.get("conflicts_with"):
                raise MemoryTransitionError("promotion requires resolved memory conflicts")
            if int(current.get("success_count", 0)) < self.promotion_successes:
                raise MemoryTransitionError("promotion requires repeated successful use")
            if int(current.get("failure_count", 0)):
                raise MemoryTransitionError("promotion is blocked by negative feedback")
            if not evidence and not bool((current.get("metadata") or {}).get("human_verified")):
                raise MemoryTransitionError("promotion requires source evidence or human verification")
        metadata = dict(current.get("metadata") or {})
        history = list(metadata.get("lifecycle_history") or [])
        history.append({"from": source, "to": target, "actor": actor, "reason": reason[:500]})
        metadata["lifecycle_history"] = history[-50:]
        updated = self.store.update_agent_memory(
            memory_id, expected_version,
            {"status": target, "metadata": metadata},
        )
        if updated is None:
            raise MemoryVersionConflict("agent memory version changed")
        return updated

    def record_outcome(
        self, memory_id: str, expected_version: int, successful: bool,
        contradicted: bool = False, note: str = "",
    ) -> Dict[str, Any]:
        current = self.store.get_agent_memory(memory_id)
        if current is None:
            raise KeyError("agent memory not found")
        metadata = dict(current.get("metadata") or {})
        feedback = list(metadata.get("feedback") or [])
        feedback.append({
            "successful": bool(successful), "contradicted": bool(contradicted),
            "note": note[:500],
        })
        updates = {
            "success_count": int(current.get("success_count", 0)) + int(bool(successful)),
            "failure_count": int(current.get("failure_count", 0)) + int(not successful),
            "metadata": {**metadata, "feedback": feedback[-100:]},
        }
        if contradicted:
            updates["status"] = MemoryStatus.REJECTED.value
        updated = self.store.update_agent_memory(memory_id, expected_version, updates)
        if updated is None:
            raise MemoryVersionConflict("agent memory version changed")
        return updated

    def mark_conflict(
        self, memory_id: str, other_id: str, expected_version: int,
        supersedes: bool = False,
    ) -> Dict[str, Any]:
        current = self.store.get_agent_memory(memory_id)
        other = self.store.get_agent_memory(other_id)
        if current is None or other is None:
            raise KeyError("conflicting agent memory not found")
        if current.get("tenant_id") != other.get("tenant_id") or current.get("repository") != other.get("repository"):
            raise MemoryTransitionError("memory relations cannot cross tenant or repository")
        conflicts = list(current.get("conflicts_with") or [])
        if other_id not in conflicts:
            conflicts.append(other_id)
        updates: Dict[str, Any] = {"conflicts_with": conflicts[-100:]}
        if supersedes:
            updates["supersedes"] = other_id
        updated = self.store.update_agent_memory(memory_id, expected_version, updates)
        if updated is None:
            raise MemoryVersionConflict("agent memory version changed")
        return updated
