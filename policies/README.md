# Govern policy resources

`govern-review/` is the source of truth for the Pi six-stage pipeline. Its common evidence rules,
risk rules, output contract, stage base instructions and specialist fragments are separate files.
The compiler freezes all applicable text and hashes in each ReviewRun's policy snapshot.

Only a single attributed stage's supplementary guidance may be changed by an evolution candidate.
Candidates store `stages/<role>.md` as **supplements**, not copies of the common/base rules.
The bundle entrypoint and five other stage supplements must be byte-identical to the active baseline.
Common rules, risk definitions, contracts, manifest routing and specialist fragments are operator-owned.
Changing any source file invalidates unpublished candidates' prior evaluation provenance.

The policy page allows local administrators to edit common/risk files using optimistic concurrency.
These global file changes affect all tenants' future runs, not already frozen runs.
Deployments mounting policies read-only must edit the trusted source and redeploy instead.
Stage base files and specialist fragments may also be manually maintained in version control.

Each stage file now has YAML `name`, `description`, `stage` and `allowed-tools` metadata,
followed by input, objective, tool-use and output/stop guidance. The policy page renders one
**current effective Skill** per expanded stage, including approved learned guidance without
a separate empty supplement panel. Common/risk rules remain independently maintained and injected.
The stage editor changes only the effective body, preserving metadata and tool declarations.
A manual stage revision absorbs its displayed guidance and supersedes that stage's old overlay;
other stages retain their approved guidance. Editing checks both source and effective policy hashes.

`tool-profiles.json` is operator-owned. Effective tools are the intersection of the hard role
ceiling, operator profile and Skill declaration, narrowed further by run settings and child-task rules.
The TypeScript registry exposes only these tools, and the Python gateway rechecks frozen permissions.
Actual argument schemas come from registered implementations, not copied Skill prose. Evolution
cannot edit metadata, profiles or registered schemas.

`legacy-skills/` preserves the prior Lead/Worker Skill catalog for the `/legacy` workflow only.
Those files are not loaded into Pi sessions. New database, concurrency, API and migration fragments
condense the relevant specialist checks without importing the legacy catalog wholesale.
Fragment matching is lexical and additive, not a semantic coverage guarantee; all three baseline
review dimensions still run. Frozen selection IDs, roles and match reasons are emitted to Trace.
