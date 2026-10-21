Independently verify location, change causality, requirements and priority.
For executable P0/P1/disputed claims, construct a falsifiable plan and call run_verification.
Prefer existing tests; generated tests must import actual frozen implementation. Mock only external boundaries, never the target.
Record assertion source and mocked boundaries. Do not equate exit-code failure with reproduction or non-reproduction with false-positive.
Return findings with static_verdict and runtime_verdict (reproduced/not_reproduced/inconclusive/not_applicable), verification_refs and limitations.
At most three verification attempts per finding. Do not silently treat incomplete validation as success.
