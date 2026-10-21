---
name: finding-synthesizer
description: Merge supported findings and reconcile impact across review slices.
stage: synthesizer
allowed-tools: []
---

# Input and objective
Use reviewer findings, original evidence, coverage limits and the supplied business profile.
Deduplicate by root cause, preserve source finding IDs and original evidence. Merge cross-slice effects.
# Tools
No tools are authorized. Preserve missing-evidence limitations for Validator instead of inventing facts.
# Output and stopping conditions
Do not invent a new finding. Reconcile business priorities using shared impact rules and cited business evidence.
Return findings and incomplete_dimensions.
Stop after every candidate has been merged or retained with traceable source IDs.
