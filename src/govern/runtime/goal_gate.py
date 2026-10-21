"""Independent completion criteria for the PR review workflow."""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from govern.core.diff_parser import ParsedDiff


class RuntimeGoalNotMet(RuntimeError):
    def __init__(self, requirements: Iterable[str]):
        self.requirements = tuple(str(item) for item in requirements)
        super().__init__("runtime goal is incomplete: %s" % "; ".join(self.requirements))


@dataclass(frozen=True)
class GoalGateResult:
    complete: bool
    checks: Dict[str, Any]
    requirements: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "complete": self.complete,
            "checks": dict(self.checks),
            "requirements": list(self.requirements),
        }


class ReviewGoalGate:
    """Validate persisted workflow state and final report without an LLM judge."""

    required_checkpoints = ("planning", "executing", "reviewing")

    def evaluate(
        self, state: Dict[str, Any], checkpoints: Dict[str, Dict[str, Any]],
    ) -> GoalGateResult:
        requirements: List[str] = []
        report = state.get("report") or {}
        parsed_value = state.get("parsed") or {}
        try:
            parsed = ParsedDiff(
                list(parsed_value.get("files") or []),
                [],
            )
        except Exception:
            parsed = ParsedDiff([], [])
        valid_locations = {
            (str(item.get("path", "")), int(item.get("line", 0) or 0)):
            str(item.get("content", ""))
            for item in parsed_value.get("added_lines") or []
            if isinstance(item, dict)
        }

        completed = {
            name for name in self.required_checkpoints
            if (checkpoints.get(name) or {}).get("status") == "completed"
        }
        missing_checkpoints = sorted(set(self.required_checkpoints).difference(completed))
        if missing_checkpoints:
            requirements.append(
                "missing completed checkpoints: %s" % ", ".join(missing_checkpoints)
            )
        if not parsed.files or not valid_locations:
            requirements.append("parsed diff has no changed file or added-line evidence")
        if not isinstance(report, dict) or not report.get("summary"):
            requirements.append("final report and summary are required")
        if report.get("repository") != state.get("repository"):
            requirements.append("report repository does not match the task")

        invalid_findings = []
        invalid_evidence_refs = []
        high_risk_unverified = []
        for index, finding in enumerate(report.get("findings") or []):
            if not isinstance(finding, dict):
                invalid_findings.append(index)
                continue
            location = (
                str(finding.get("path", "")), int(finding.get("line", 0) or 0),
            )
            required = (
                str(finding.get("rule_id", "")).strip(),
                str(finding.get("title", "")).strip(),
                str(finding.get("explanation", "")).strip(),
                str(finding.get("evidence", "")).strip(),
            )
            evidence = required[-1]
            exact_line = bool(
                evidence and evidence in str(valid_locations.get(location, ""))
            )
            evidence_refs = [
                item for item in finding.get("evidence_refs") or []
                if isinstance(item, dict)
            ]
            immutable_claimed = any(
                reference.get("artifact_ref") or reference.get("artifact_sha256")
                or reference.get("excerpt_hash")
                for reference in evidence_refs
            )
            valid_structured_refs = []
            for reference in evidence_refs:
                ref_path = str(reference.get("path", ""))
                ref_lines = {
                    int(line) for line in reference.get("added_lines") or []
                    if str(line).isdigit()
                }
                immutable = bool(
                    reference.get("artifact_ref") and reference.get("artifact_sha256")
                    and reference.get("excerpt_hash")
                )
                location_consistent = (
                    not ref_path or (
                        ref_path == location[0]
                        and (not ref_lines or location[1] in ref_lines)
                    )
                )
                if immutable and location_consistent:
                    valid_structured_refs.append(reference)
            if immutable_claimed and not valid_structured_refs:
                invalid_evidence_refs.append(index)
            structured_evidence = bool(
                valid_structured_refs or (evidence_refs and not immutable_claimed)
                or finding.get("call_chain")
            )
            if (
                location not in valid_locations or not all(required)
                or (not exact_line and not structured_evidence)
            ):
                invalid_findings.append(index)
            severity = str(finding.get("severity", "")).lower()
            gate = finding.get("gate") or {}
            if severity in {"high", "critical"} and (
                not str(finding.get("fix", "")).strip()
                or not str(finding.get("test", "")).strip()
                or (gate and gate.get("passed") is not True)
            ):
                high_risk_unverified.append(index)
        if invalid_findings:
            requirements.append(
                "findings with invalid required evidence: %s"
                % ", ".join(str(item) for item in invalid_findings)
            )
        if invalid_evidence_refs:
            requirements.append(
                "findings with mutable or location-inconsistent evidence references: %s"
                % ", ".join(str(item) for item in invalid_evidence_refs)
            )
        if high_risk_unverified:
            requirements.append(
                "high-risk findings without completed release/evidence gate: %s"
                % ", ".join(str(item) for item in high_risk_unverified)
            )

        checks = {
            "checkpoint_count": len(completed),
            "required_checkpoint_count": len(self.required_checkpoints),
            "changed_files": len(parsed.files),
            "added_line_locations": len(valid_locations),
            "findings": len(report.get("findings") or []),
            "invalid_findings": len(invalid_findings),
            "invalid_evidence_refs": len(invalid_evidence_refs),
            "high_risk_unverified": len(high_risk_unverified),
        }
        return GoalGateResult(not requirements, checks, requirements)

    def require_complete(
        self, state: Dict[str, Any], checkpoints: Dict[str, Dict[str, Any]],
    ) -> GoalGateResult:
        result = self.evaluate(state, checkpoints)
        if not result.complete:
            raise RuntimeGoalNotMet(result.requirements)
        return result
