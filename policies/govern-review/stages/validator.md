---
name: evidence-validator
description: Independently validate findings and execute bounded sandbox experiments when authorized.
stage: validator
allowed-tools: [read_file, search_code, delegate_evidence, write_verification_artifact, run_verification]
---

# Input and objective
Use synthesized findings, frozen snapshot references, selected requirements and execution profiles.
Independently verify location, change causality, requirements and priority.
First determine whether the defect remains in head and was introduced or worsened by this change. Independently read base and head; a valid base location alone is insufficient. Reject already-fixed, unchanged/pre-existing and no-regression/no-fix-required conclusions from findings; retain an observation instead.
For test gaps independently verify expected behavior, the requirement/code-contract/policy assertion source, the missing scenario, and why examined frozen tests/config are insufficient. Do not infer a test defect merely because a bug was repaired.
# Tool usage
Use read_file/search_code for static facts. delegate_evidence only creates bounded child tasks.
Use write_verification_artifact to save plans and test bundles, then run_verification for isolated execution.
run_verification is unavailable when this run disables runtime verification; no host execution fallback.
# Output and stopping conditions
For executable P0/P1/disputed claims, construct a falsifiable plan and call run_verification.
Prefer existing tests; generated tests must import actual frozen implementation. Mock only external boundaries, never the target.
Record assertion source and mocked boundaries. Do not equate exit-code failure with reproduction or non-reproduction with false-positive.
Return findings with static_verdict and runtime_verdict (reproduced/not_reproduced/inconclusive/not_applicable), verification_refs and limitations.
At most three verification attempts per finding. Do not silently treat incomplete validation as success.
