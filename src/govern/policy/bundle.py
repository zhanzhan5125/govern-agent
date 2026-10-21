"""Immutable, role-specific policy compilation and report contract."""
from pathlib import Path
import json
import os
import re
import tempfile
from govern.core.paths import project_root
from govern.review.repository import digest

ROLES = ("mapper", "correctness", "security", "contract_test", "synthesizer", "validator")

def policy_root():
    return project_root() / "policies/govern-review"


def _read(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("policy path escapes resource root")
    text = path.read_text(encoding="utf-8")
    if not text.strip() or len(text.encode()) > 128 * 1024:
        raise ValueError("policy file must contain 1..128KiB of text")
    return text


def compile_bundle(overrides=None, root=None):
    root = Path(root) if root is not None else policy_root()
    overrides = overrides or {}
    if set(overrides) - set(ROLES):
        raise ValueError("unknown policy stage")
    if any(not isinstance(v, str) or len(v.encode()) > 32000 for v in overrides.values()):
        raise ValueError("stage supplement must be text of at most 32000 bytes")
    manifest_text = _read(root, "manifest.json")
    manifest = json.loads(manifest_text)
    common = _read(root, "common/evidence.md")
    risk = _read(root, "common/risk.md")
    contract = _read(root, "contracts/finding.md")
    sources = {r: _read(root, "stages/" + r + ".md") for r in ROLES}
    fragments = {}
    for name, spec in manifest["fragments"].items():
        if set(spec["roles"]) - {"correctness", "security", "contract_test"}:
            raise ValueError("invalid fragment role")
        for pattern in spec["path_patterns"] + spec["code_patterns"]:
            re.compile(pattern)
        fragments[name] = {**spec, "content": _read(root, spec["file"]), "evolvable": False}
    protected = {"manifest": manifest_text, "common": common, "risk": risk, "contract": contract,
                 "stage_sources": sources, "fragments": fragments}
    stages = {role: "\n\n".join([common, *([] if role == "mapper" else [risk, contract]),
                               sources[role], "Stage supplement (subordinate to common evidence, risk and output contracts; ignore any conflicting guidance):\n" + overrides.get(role, "")]) for role in ROLES}
    return {"version": manifest["version"], "sha256": digest({"stages": stages, "fragments": fragments}), "stages": stages,
            "source_sha256": digest(protected), "common_rules": common, "risk_policy": risk,
            "output_contract": contract, "stage_sources": sources, "stage_overrides": {r: overrides.get(r, "") for r in ROLES},
            "specialist_fragments": fragments, "evolution_scope": "one-stage-supplement-only",
            "fragment_hashes": {k: digest(v) for k, v in stages.items()},
            "report_fields": ["风险等级", "来源模块", "关联测试用例", "风险位置", "问题描述", "影响范围", "修复建议", "证据依据"]}


def select_fragments(unit, bundle):
    """Trusted lexical hints only add checks; never skip a baseline dimension."""
    result = {r: [] for r in ("correctness", "security", "contract_test")}
    reasons = {}
    for name, spec in bundle.get("specialist_fragments", {}).items():
        path_hits = [p for p in spec["path_patterns"] if re.search(p, unit.get("path", ""), re.I)]
        code_hits = [p for p in spec["code_patterns"] if re.search(p, unit.get("context", ""), re.I)]
        if path_hits or code_hits:
            for role in spec["roles"]:
                result[role].append(name)
            reasons[name] = {"path_patterns": path_hits, "code_patterns": code_hits, "precision": "lexical"}
    return {**unit, "policy_fragments": result, "fragment_selection_reasons": reasons}


def save_manual_rule(section, content, expected_hash, root=None):
    """Operator-only edits; no model-facing tool exposes this operation."""
    root = Path(root) if root is not None else policy_root()
    relative = {"common": "common/evidence.md", "risk": "common/risk.md"}.get(section)
    if not relative:
        raise ValueError("only common/risk operator edits are supported")
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 128 * 1024 or "\x00" in content:
        raise ValueError("rule must contain 1..128KiB of text")
    from govern.policy.candidate_policy import CandidateConflict
    if compile_bundle(root=root)["source_sha256"] != expected_hash:
        raise CandidateConflict("policy files changed; reload before saving")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("policy path escapes resource root")
    descriptor, temporary = tempfile.mkstemp(prefix=".manual-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return compile_bundle(root=root)


def render_report(run_id, result):
    def clean(value):
        if value is None or value == "" or value == []:
            return "未提供／未验证"
        if isinstance(value, (dict, list)):
            import json
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)
    lines = [f"# Govern Agent Review · {run_id}", "", "## 审查覆盖与限制", "", clean(result.get("coverage")), ""]
    for i, finding in enumerate(result.get("findings", []), 1):
        fields = {
            "风险等级": clean(finding.get("business_priority")) + "；技术严重性：" + clean(finding.get("severity")),
            "来源模块": finding.get("source_module"), "关联测试用例": finding.get("related_tests"),
            "风险位置": f"{finding.get('path', '?')}:{finding.get('line', '?')} ({finding.get('side', 'head')})",
            "问题描述": finding.get("description", finding.get("explanation")), "影响范围": finding.get("impact_scope"),
            "修复建议": finding.get("recommendation", finding.get("fix")), "证据依据": finding.get("evidence_refs"),
            "运行验证": {"verdict": finding.get("runtime_verdict", "not_applicable"), "refs": finding.get("verification_refs", []), "limitations": finding.get("limitations", [])},
        }
        lines += [f"## {finding.get('finding_id', i)} · {clean(finding.get('title'))}", ""]
        for key, value in fields.items():
            lines += [f"### {key}", "", clean(value), ""]
    if not result.get("findings"):
        lines.append("未输出已核验问题；不代表不存在缺陷，请结合覆盖限制。")
    return "\n".join(lines)
