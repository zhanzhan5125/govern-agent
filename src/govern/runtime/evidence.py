"""Immutable evidence records backed by content-addressed runtime artifacts."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from govern.runtime.artifacts import ArtifactStore
from govern.core.diff_parser import parse_unified_diff
from govern.runtime.hooks import HookAction, HookContext, HookPoint, HookResult
from govern.storage.store import utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _walk(value: Any) -> Iterable[Tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _locations(arguments: Dict[str, Any], output: Any) -> Tuple[str, Tuple[int, ...]]:
    paths: List[str] = []
    lines: List[int] = []
    for key, value in list(arguments.items()) + list(_walk(output)):
        lowered = key.lower()
        if lowered in {"path", "file", "filename"} and isinstance(value, str):
            paths.append(value.replace("\\", "/"))
        elif lowered in {"line", "line_number", "added_line"}:
            try:
                lines.append(int(value))
            except (TypeError, ValueError):
                pass
        elif lowered in {"lines", "added_lines"} and isinstance(value, (list, tuple)):
            for item in value:
                try:
                    lines.append(int(item))
                except (TypeError, ValueError):
                    pass
    return (paths[0] if paths else "", tuple(sorted({line for line in lines if line > 0})))


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    artifact_ref: str
    artifact_sha256: str
    path: str = ""
    added_lines: Tuple[int, ...] = ()
    excerpt_hash: str = ""
    tool: str = ""
    created_by: str = ""
    verified_by: Tuple[str, ...] = ()
    created_at: str = ""
    source_evidence_id: str = ""
    integrity: str = "content-addressed"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["added_lines"] = list(self.added_lines)
        value["verified_by"] = list(self.verified_by)
        return value


class EvidenceStore:
    """Create, disclose and verify evidence without rewriting its raw payload."""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def capture(
        self, task_id: str, tenant_id: str, tool: str, arguments: Dict[str, Any],
        result: Any, created_by: str = "",
    ) -> EvidenceRecord:
        source_id = str(result.get("evidence_id", "")) if isinstance(result, dict) else ""
        output = result.get("output") if isinstance(result, dict) and "output" in result else result
        artifact = self.artifacts.put(
            task_id, tenant_id, "evidence:%s" % (tool or "unknown"),
            {"tool": tool, "arguments": dict(arguments), "result": result},
        )
        path, lines = _locations(arguments, output)
        fingerprint = "%s\0%s\0%s\0%s" % (
            task_id, artifact.sha256, path, ",".join(str(line) for line in lines)
        )
        evidence_id = "ev-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return EvidenceRecord(
            evidence_id=evidence_id,
            artifact_ref=artifact.uri,
            artifact_sha256=artifact.sha256,
            path=path,
            added_lines=lines,
            excerpt_hash=_content_hash(output),
            tool=tool,
            created_by=created_by,
            created_at=utc_now(),
            source_evidence_id=source_id,
        )

    def read(self, artifact_ref: str, tenant_id: str) -> Any:
        prefix = "artifact://"
        if not str(artifact_ref).startswith(prefix):
            raise ValueError("invalid evidence artifact reference")
        return self.artifacts.get(str(artifact_ref)[len(prefix):], tenant_id)

    def verify(
        self, record: Dict[str, Any], tenant_id: str, diff: str = "",
        verifier: str = "evidence-verifier",
    ) -> Dict[str, Any]:
        raw = self.read(str(record.get("artifact_ref", "")), tenant_id)
        artifact_id = str(record["artifact_ref"]).split("artifact://", 1)[1]
        stored = self.artifacts.store.get_runtime_artifact(artifact_id, tenant_id)
        integrity_ok = bool(stored and stored.get("sha256") == record.get("artifact_sha256"))
        output = raw.get("result") if isinstance(raw, dict) else raw
        if isinstance(output, dict) and "output" in output:
            output = output["output"]
        excerpt_ok = _content_hash(output) == str(record.get("excerpt_hash", ""))
        location_ok: Optional[bool] = None
        path = str(record.get("path", ""))
        lines = {int(line) for line in record.get("added_lines") or []}
        if diff and path and lines:
            parsed = parse_unified_diff(diff)
            valid = {(item.path, item.line) for item in parsed.added_lines}
            location_ok = all((path, line) in valid for line in lines)
        verified = integrity_ok and excerpt_ok and location_ok is not False
        result = dict(record)
        result["verified"] = verified
        result["verification"] = {
            "integrity_ok": integrity_ok,
            "excerpt_hash_ok": excerpt_ok,
            "location_ok": location_ok,
            "verified_by": verifier,
        }
        return result


class EvidenceCaptureHook:
    """Persist factual tool outputs before any model-facing truncation occurs."""

    def __init__(self, evidence: EvidenceStore):
        self.evidence = evidence

    def __call__(self, context: HookContext) -> HookResult:
        if context.point != HookPoint.TOOL_AFTER or "result" not in context.payload:
            return HookResult()
        result = context.payload["result"]
        if not isinstance(result, dict) or not result.get("evidence_id"):
            return HookResult()
        if result.get("evidence_record"):
            return HookResult()
        tool = str(context.metadata.get("tool", result.get("tool", "unknown")))
        if tool in {"read_memory", "read_artifact"}:
            return HookResult()
        record = self.evidence.capture(
            context.task_id, context.tenant_id, tool,
            dict(context.metadata.get("arguments") or context.payload.get("arguments") or {}),
            result, context.role,
        )
        enriched = dict(result)
        enriched["source_evidence_id"] = str(result.get("evidence_id", ""))
        enriched["evidence_id"] = record.evidence_id
        enriched["evidence_record"] = record.to_dict()
        payload = dict(context.payload)
        payload["result"] = enriched
        payload["evidence"] = record.to_dict()
        from govern.infrastructure.metrics import metrics
        metrics.inc("evidence_records_captured_total")
        return HookResult(HookAction.MODIFY, payload=payload)
