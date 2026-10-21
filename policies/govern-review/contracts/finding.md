# Finding output contract (program-owned, not evolvable)

Every finding must contain title, rule_id, severity, business_priority (P0/P1/P2 or null),
priority_reason, source_module, related_tests (array), path, line, side (base/head), description,
impact_scope, recommendation, evidence_refs (array), category and confidence. Missing evidence is explicit.
For business_priority also return priority_basis: [{source: requirement, id: selected issue/criterion ID},
{source: business_profile, path: JSON pointer without initial slash, e.g. core_flows/0},
or {source: code, path, line}] and impact_kind. Code-only priority basis is accepted only for
unauthorized_access/security_incident/data_loss; core business claims need supplied business/requirement evidence.
