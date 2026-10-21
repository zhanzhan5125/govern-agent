"""Tenant-aware working, episodic and semantic memory for review agents."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from govern.memory.memory_governance import MemoryGovernance, MemoryStatus
from govern.memory.memory_retrieval import HybridMemoryRetriever
from govern.storage.store import utc_now


TOKEN = re.compile(r"[A-Za-z0-9_./:-]{2,}")
VALID_SCOPES = {"working", "episodic", "semantic", "procedural"}


def _tokens(value: str) -> set:
    return {item.lower() for item in TOKEN.findall(value)}


class MemoryManager:
    """Persist and retrieve bounded memories without hiding store side effects."""

    def __init__(
        self, store, enabled: bool = True, recall_limit: int = 6,
        working_ttl_seconds: int = 86400, retriever=None, artifact_store=None,
    ):
        self.store = store
        self.enabled = enabled
        self.recall_limit = max(1, recall_limit)
        self.working_ttl_seconds = max(60, working_ttl_seconds)
        self.retriever = retriever or HybridMemoryRetriever()
        self.governance = MemoryGovernance(store)
        self.artifact_store = artifact_store

    def remember(
        self, tenant_id: str, repository: str, scope: str, kind: str,
        content: str, metadata: Optional[Dict[str, Any]] = None,
        task_id: str = "", agent: str = "", importance: float = 0.5,
        ttl_seconds: Optional[int] = None, status: Optional[str] = None,
        source_evidence: Sequence[str] = (), supersedes: str = "",
        conflicts_with: Sequence[str] = (),
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled or not content.strip():
            return None
        if scope not in VALID_SCOPES:
            raise ValueError("unsupported memory scope: %s" % scope)
        importance = max(0.0, min(1.0, float(importance)))
        metadata = dict(metadata or {})
        normalized = content.strip()[:8000]
        fingerprint = json.dumps({
            "tenant": tenant_id, "repository": repository, "scope": scope,
            "kind": kind, "content": normalized, "metadata": metadata,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        memory_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        ttl = self.working_ttl_seconds if scope == "working" and ttl_seconds is None else ttl_seconds
        expires_at = None
        if ttl:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl)))
            ).isoformat()
        record = {
            "id": memory_id, "tenant_id": tenant_id or "default",
            "repository": repository, "task_id": task_id, "agent": agent,
            "scope": scope, "kind": kind, "content": normalized,
            "keywords": sorted(_tokens(normalized) | _tokens(kind)),
            "metadata": metadata, "importance": importance,
            "created_at": utc_now(), "expires_at": expires_at,
            "updated_at": utc_now(),
            "status": str(status or (
                MemoryStatus.PROVISIONAL.value if scope == "working"
                else MemoryStatus.VERIFIED.value
            )).upper(),
            "version": 1,
            "content_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "source_evidence": list(dict.fromkeys(str(value) for value in source_evidence if value)),
            "supersedes": str(supersedes or ""),
            "conflicts_with": list(dict.fromkeys(str(value) for value in conflicts_with if value)),
            "success_count": 0, "failure_count": 0, "use_count": 0,
            "last_used_at": None,
        }
        return self.store.save_agent_memory(record)

    def recall(
        self, tenant_id: str, repository: str, query: str,
        scopes: Sequence[str] = ("semantic", "episodic"),
        limit: Optional[int] = None, task_id: str = "",
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        purge = getattr(self.store, "purge_expired_agent_memories", None)
        if purge:
            purge()
        selected_scopes = tuple(scope for scope in scopes if scope in VALID_SCOPES)
        if not selected_scopes:
            return []
        candidates = self.store.list_agent_memories(
            tenant_id or "default", repository, selected_scopes, 200
        )
        if task_id:
            candidates = [
                item for item in candidates if str(item.get("task_id", "")) == str(task_id)
            ]
        size = max(1, limit or self.recall_limit)
        from govern.infrastructure.metrics import metrics
        with metrics.timer("memory_retrieval"):
            ranked = self.retriever.rank(
                query, candidates, size,
                allow_provisional=bool(task_id and selected_scopes == ("working",)),
            )
        metrics.inc("memory_retrieval_calls_total")
        metrics.inc("memory_retrieval_candidates_total", len(candidates))
        metrics.inc("memory_retrieval_results_total", len(ranked))
        recorder = getattr(self.store, "record_agent_memory_recall", None)
        if recorder:
            recorder([item.get("id") for item in ranked])
        return ranked

    def get(
        self, memory_id: str, tenant_id: str, repository: str,
    ) -> Optional[Dict[str, Any]]:
        """Progressively disclose one full memory after scope authorization."""
        if not self.enabled:
            return None
        return self.store.get_agent_memory(memory_id, tenant_id or "default", repository)

    def transition(
        self, memory_id: str, target: str, expected_version: int,
        actor: str = "system", reason: str = "",
    ) -> Dict[str, Any]:
        return self.governance.transition(memory_id, target, expected_version, actor, reason)

    def read_for_task(self, memory_id, task_id, tenant_id, repository):
        if not self.enabled:
            return None
        return self.store.read_agent_memory_for_task(memory_id, task_id, tenant_id, repository)

    def feedback_used(self, task_id, tenant_id, successful):
        if not self.enabled:
            return []
        return self.store.record_used_memory_feedback(task_id, tenant_id, successful)

    def record_outcome(
        self, memory_id: str, expected_version: int, successful: bool,
        contradicted: bool = False, note: str = "",
    ) -> Dict[str, Any]:
        return self.governance.record_outcome(
            memory_id, expected_version, successful, contradicted, note,
        )

    def mark_conflict(
        self, memory_id: str, other_id: str, expected_version: int,
        supersedes: bool = False,
    ) -> Dict[str, Any]:
        return self.governance.mark_conflict(
            memory_id, other_id, expected_version, supersedes,
        )

    def recall_working(
        self, tenant_id: str, repository: str, task_id: str, query: str = "",
        limit: Optional[int] = None, agent: str = "",
    ) -> List[Dict[str, Any]]:
        """Recall only the transient observations belonging to one review task."""
        if not task_id:
            return []
        values = self.recall(
            tenant_id, repository, query, scopes=("working",),
            # Filter by agent after the store query; request the bounded
            # candidate set first so another role's higher-importance entries
            # cannot hide this role's own observations.
            limit=200 if agent else limit,
            task_id=task_id,
        )
        selected = [
            item for item in values
            if not agent or str(item.get("agent", "")) == str(agent)
        ]
        return selected[:max(1, limit or self.recall_limit)]

    def remember_observation(
        self, tenant_id: str, repository: str, task_id: str, agent: str,
        observation: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Store a bounded, factual tool observation for later roles in this task."""
        result = observation.get("result")
        evidence_id = result.get("evidence_id", "") if isinstance(result, dict) else ""
        output = result.get("output") if isinstance(result, dict) else result
        try:
            rendered = json.dumps(output, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = str(output)
        content = "Agent %s tool %s at step %s: %s. Evidence: %s. Output: %s" % (
            agent, observation.get("tool", "unknown"), observation.get("step", 0),
            "ok" if observation.get("ok") else "failed", evidence_id,
            rendered[:4000] if observation.get("ok") else str(observation.get("error", ""))[:1000],
        )
        return self.remember(
            tenant_id, repository, "working", "tool_observation", content,
            {
                "tool": str(observation.get("tool", "")),
                "step": observation.get("step", 0), "ok": bool(observation.get("ok")),
                "evidence_id": evidence_id,
            }, task_id=task_id, agent=agent,
            importance=0.5 if observation.get("ok") else 0.3,
            status=MemoryStatus.PROVISIONAL.value,
            source_evidence=[evidence_id] if evidence_id else (),
        )

    def remember_finding(
        self, tenant_id: str, repository: str, task_id: str,
        finding: Dict[str, Any], approved: bool, reasons: Iterable[str] = (),
    ) -> Optional[Dict[str, Any]]:
        decision = "approved" if approved else "rejected"
        content = (
            "%s finding %s at %s:%s. Evidence: %s. Explanation: %s. "
            "Fix: %s. Decision reasons: %s"
        ) % (
            decision, finding.get("rule_id", "unknown"), finding.get("path", ""),
            finding.get("line", 0), finding.get("evidence", ""),
            finding.get("explanation", ""), finding.get("fix", ""),
            "; ".join(str(item) for item in reasons),
        )
        return self.remember(
            tenant_id, repository, "episodic", "finding_%s" % decision,
            content, {"finding": finding, "approved": approved}, task_id=task_id,
            importance=0.8 if approved else 0.45,
            status=MemoryStatus.VERIFIED.value,
            source_evidence=[
                str(item.get("evidence_id"))
                for item in finding.get("evidence_refs") or []
                if isinstance(item, dict) and item.get("evidence_id")
            ],
        )

    def remember_feedback(
        self, tenant_id: str, repository: str, task_id: str, category: str,
        finding: Optional[Dict[str, Any]], note: str,
    ) -> Optional[Dict[str, Any]]:
        finding = dict(finding or {})
        content = "Feedback %s for %s at %s:%s. Note: %s" % (
            category, finding.get("rule_id", "task"), finding.get("path", ""),
            finding.get("line", 0), note,
        )
        return self.remember(
            tenant_id, repository, "semantic", "review_feedback", content,
            {"category": category, "finding": finding}, task_id=task_id,
            importance=0.95 if category in {"false_positive", "missed_issue", "bad_fix"} else 0.7,
            status=MemoryStatus.PROVISIONAL.value,
            source_evidence=[
                str(item.get("evidence_id"))
                for item in finding.get("evidence_refs") or []
                if isinstance(item, dict) and item.get("evidence_id")
            ],
        )

    def forget_working(self, task_id: str) -> int:
        if not self.enabled:
            return 0
        return self.store.delete_agent_memories(task_id=task_id, scope="working")

    def consolidate_task(
        self, tenant_id: str, repository: str, task_id: str,
        summary: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Archive a compact task episode, then release transient working memory."""
        if not self.enabled or not task_id:
            return None
        content = "Review task %s completed: %s" % (
            task_id,
            json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        archived = self.remember(
            tenant_id, repository, "episodic", "task_summary", content,
            metadata={"summary": dict(summary)}, task_id=task_id,
            agent="agent-runtime", importance=0.65,
            status=MemoryStatus.VERIFIED.value,
        )
        self.forget_working(task_id)
        return archived
