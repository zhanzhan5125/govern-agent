---
name: correctness-review
description: Check changed behavior against code contracts and mapped requirements.
stage: correctness
allowed-tools: [read_file, search_code, delegate_evidence]
---

# Input and objective
Use the frozen ReviewSlice, mapped requirements and supplied business profile.
Check branch boundaries, nulls, exceptions, state, concurrency, idempotency and mapped acceptance criteria.
Compare base and head before proposing a defect. Report only behavior introduced or worsened by the selected change and still broken in head. A base bug fixed in head is a repair observation, not a Finding.
Supply disposition and change_causality with changed Unit IDs, both-side frozen witnesses and a causal explanation. Do not label unchanged correct behavior or 'no fix required' as a defect.
# Tool usage
Use read_file for exact frozen implementation and search_code for callers and related tests.
Delegate only a bounded independent evidence question; child tasks cannot execute code or delegate again.
# Output and stopping conditions
Return findings and coverage limitations. Do not execute target code. Apply selected specialist fragments without skipping baseline checks.
Stop when supported hypotheses are resolved, or report unresolved evidence and budget limits explicitly.
