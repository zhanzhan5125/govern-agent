# Govern convergence

The Jira/Slice workflow, six-stage pipeline, Pi runtime and Validator verification
remain unchanged in responsibility. No active policy is migrated or published.

## Memory

Govern reuses MemoryManager, its repository/tenant isolation, retrieval and lifecycle.
Recall catalogs are checkpointed per review. Task summaries and explicit feedback
are stored through the existing APIs; evaluator tasks never recall or write product
experience. Recall/storage failures are observable but do not fail an otherwise
complete review. Historical memory disclosure has a separate, per-run consent,
default off; ordinary code disclosure consent does not imply memory disclosure.
Memory is historical context, not authority or current code evidence. Human feedback
does not prove a runtime reproduction. No automatic memory promotion is introduced.

## Context

Product review uses the Python ContextManager observation compressor through an
internal Pi host callback. It is not exposed as a model tool. The TypeScript fallback
remains for standalone/runtime compatibility tests. Initial instructions and Slice
inputs are protected; old complete assistant/tool rounds may be summarized together.
Original observations and folded history are local Artifacts. References remain in
the summary; current facts must be reread from frozen base/head code. Oversized
initial inputs fail explicitly rather than silently losing coverage. This is bounded
structural compression, not a claim of lossless or model-semantic summarization.

## Feedback and evaluation

Stage generation shares legacy engineering error routes. Repository-specific rules
go to memory; other user-labelled feedback is grouped by stage/type/reason. Frozen
locations are checked when snapshots exist; legacy records are identified as such.
Cross-repository support is not mandatory for the personal workflow. Bounded examples,
protected evaluation data exclusion, consumption tracking and human release remain.
The bundled corpus remains 80 Validation / 20 Holdout. With no P0/P1 labels, that
metric is not applicable, never a claim of demonstrated business-risk quality. With
labels, population equality and non-regression remain mandatory. All other quality,
cost, permission, telemetry and human approval gates continue to apply.

## GitHub audit

The Vue settings page accepts a write-only GitHub Token, blank preserves it and
explicit removal clears it. Saved settings override the legacy environment token.
The read-only REST client refuses writes, non-GitHub hosts and redirects; errors
never expose provider bodies. The application-owned PR cache fetch uses the same
credential through process-only Git configuration, not argv, URL or a Git file.
Credential helpers, interactive prompts and inherited Git tracing are disabled.
The cache fixes base/head SHAs and rechecks both before accepting a snapshot.
Public repositories can use anonymous reads. Private repositories require an
authorized token with Contents/Pull requests read permissions for the repository.
The settings connection check reads repository metadata; actual PR capture also
checks source access. Offline regressions cover private authentication plumbing,
not a claim that a user's private repository credential has been live-tested.

In the loopback-published Docker deployment, settings writes accept the exact
default gateway only when explicitly enabled by Compose, with a loopback Host and
same-origin browser Origin. Other bridge peers and missing/cross-site Origins are
rejected. Native loopback tooling retains the existing behavior.
