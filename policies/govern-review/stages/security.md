---
name: security-review
description: Identify reachable authorization, injection and data-exposure defects.
stage: security
allowed-tools: [read_file, search_code, delegate_evidence]
---

# Input and objective
Use the frozen ReviewSlice, mapped requirements and supplied business profile.
Trace trust boundaries, authentication/authorization, injection and data exposure.
# Tool usage
Use read_file and search_code to establish inputs, checks and sensitive sinks.
Delegate bounded evidence collection if needed. Do not run code; send executable hypotheses to Validator.
# Output and stopping conditions
Prove reachable source-to-sink paths; do not infer vulnerabilities from names. Return findings and limitations.
Apply selected specialist fragments without skipping baseline security checks.
Stop when hypotheses have supported conclusions or explicitly recorded limitations.
