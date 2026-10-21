# Business priority policy

P0: evidenced financial loss/compensation, blocked core transaction/business flow,
unauthorized access/security incident/data loss, core page crash/blank screen.
P1: severely degraded core functionality, blocked non-core function, wrong/stale core data,
batch/rollout/client consistency errors with material impact.
P2: localized non-blocking functionality/data/display issues, incorrect feedback despite completed operation,
or calls/analytics violating an explicit requirement/convention even with no functional impact.
Do not infer core business from names. Cite the supplied business profile/requirement; otherwise leave priority null/pending.
Technical severity and confidence are independent of business priority. Do not mechanically translate critical/high/medium/low to P0/P1/P2.
Reviewers propose priority and its impact evidence; Synthesizer reconciles it; Validator independently checks it.
Priority never controls whether a baseline review dimension runs.
