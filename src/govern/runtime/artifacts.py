"""Content-addressed storage for large runtime and tool results."""
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Optional

from govern.runtime.hooks import HookAction, HookContext, HookPoint, HookResult


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    preview: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_ref": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "preview": self.preview,
            "truncated": True,
        }


class ArtifactStore:
    def __init__(self, store, preview_chars: int = 2000):
        self.store = store
        self.preview_chars = max(200, int(preview_chars))

    @staticmethod
    def serialize(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def put(
        self, task_id: str, tenant_id: str, kind: str, value: Any,
        media_type: str = "application/json",
    ) -> ArtifactReference:
        content = self.serialize(value)
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        artifact_id = hashlib.sha256(
            ("%s\0%s\0%s\0%s" % (task_id, tenant_id, kind, digest)).encode("utf-8")
        ).hexdigest()
        self.store.save_runtime_artifact({
            "id": artifact_id, "task_id": task_id, "tenant_id": tenant_id,
            "kind": kind, "media_type": media_type, "sha256": digest,
            "size_bytes": len(encoded), "content": content,
        })
        return ArtifactReference(
            artifact_id, "artifact://%s" % artifact_id, digest, len(encoded),
            media_type, content[:self.preview_chars],
        )

    def get(self, artifact_id: str, tenant_id: Optional[str] = None) -> Any:
        value = self.store.get_runtime_artifact(artifact_id, tenant_id)
        if value is None:
            raise KeyError("runtime artifact not found")
        content = str(value["content"])
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != value["sha256"]:
            raise ValueError("runtime artifact integrity check failed")
        if value.get("media_type") == "application/json":
            return json.loads(content)
        return content


class ArtifactOffloadHook:
    """Replace only a large tool output with a recoverable reference."""

    def __init__(self, artifacts: ArtifactStore, threshold_bytes: int = 32_768):
        self.artifacts = artifacts
        self.threshold_bytes = max(1024, int(threshold_bytes))

    def __call__(self, context: HookContext) -> HookResult:
        if context.point != HookPoint.TOOL_AFTER or "result" not in context.payload:
            return HookResult()
        result = context.payload["result"]
        serialized = self.artifacts.serialize(result)
        if len(serialized.encode("utf-8")) <= self.threshold_bytes:
            return HookResult()
        reference = self.artifacts.put(
            context.task_id, context.tenant_id,
            "tool-result:%s" % context.metadata.get("tool", "unknown"), result,
        )
        from govern.infrastructure.metrics import metrics
        metrics.inc("runtime_artifacts_offloaded_total")
        metrics.inc("runtime_artifacts_offloaded_bytes_total", reference.size_bytes)
        if isinstance(result, dict) and "output" in result:
            compact = dict(result)
            compact["output"] = reference.to_dict()
        else:
            compact = reference.to_dict()
        payload = dict(context.payload)
        payload["result"] = compact
        payload["artifact"] = reference.to_dict()
        return HookResult(HookAction.MODIFY, payload=payload)
