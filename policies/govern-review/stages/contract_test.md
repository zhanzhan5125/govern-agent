---
name: contract-test-review
description: Review compatibility and test adequacy without executing target code.
stage: contract_test
allowed-tools: [read_file, search_code, delegate_evidence]
---

# Input and objective
Use the frozen ReviewSlice, mapped requirements, callers and test candidates.
Check API/schema compatibility, callers, migration safety and regression-test adequacy.
# Tool usage
Use search_code to find consumers and tests; read_file to verify their exact behavior.
Delegate a focused evidence question if needed. Actual execution belongs to Validator.
# Output and stopping conditions
related_tests must distinguish existing associated tests from missing/suggested tests.
Do not execute tests; return findings and limitations. Apply selected specialist fragments without skipping baseline checks.
Stop after examining relevant contracts and tests, or explicitly record missing context.
