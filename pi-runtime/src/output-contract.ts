export const FORMAT_RETRIES=2;
export const REVIEW_ROLES=['correctness','security','contract_test'];

/** Transport/runtime contract: not an evolvable Skill and cannot grant tools. */
export function outputContract(role:string,child=false):string{
 const shape=child?
  '{"status":"completed|partial|failed","evidence_refs":[],"facts":[],"limitations":[]}':role==='mapper'?
  '{"mappings":[{"change_unit_id":"...","requirement_id":null,"criterion_id":null,"status":"confirmed|partial|ambiguous|unmapped","evidence_refs":[]}],"limitations":[]}':role==='evolution'?
  'a JSON object satisfying the candidate policy contract provided in the task':
  '{"findings":[],"incomplete_dimensions":[],"limitations":[]}';
 return '\n\nGOVERN OUTPUT CONTRACT (mandatory):\n'+
  'After evidence collection, return exactly one JSON object. No prose, preamble, markdown, or trailing commentary. '+
  'Do not narrate intended next actions as the final answer. Required shape: '+shape+'. '+
  (child||role==='mapper'||role==='evolution'?'':
   'Every finding requires path (frozen repository-relative), line (positive integer), title, severity (critical/high/medium/low), evidence_refs (array), related_tests (array). '+
   'Preserve finding_id across synthesis/validation. Include source_module, problem description/explanation, impact_scope, fix/recommendation and evidence when supported. Business priority is P0/P1/P2 or null with supporting basis. '+
   'Every finding requires disposition: actionable|fixed_in_head|pre_existing|no_regression|informational|needs_evidence. Only actionable issues still requiring a change in head belong in findings; put non-defects in observations instead. '+
   'An actionable finding requires change_causality:{status:introduced|worsened|test_gap|unproven,change_unit_ids:[],head_evidence_refs:[],base_evidence_refs:[],reasoning:"..."}. '+
   'Cite existing ChangeUnit IDs and frozen code://SNAPSHOT/head/PATH#Lx-Ly references; compare base evidence when a base implementation exists. A deleted-line anchor is allowed only with a head witness proving a surviving broken caller/contract. '+
   'A bug in base that head fixes is not a new defect. No-regression/no-fix-required statements are observations. '+
   'For status=test_gap independently provide test_gap:{expected_behavior,missing_scenario,assertion_source,why_insufficient,existing_test_evidence_refs:[]}. assertion_source must be a selected requirement/criterion ID, frozen head code contract reference, or policy://POLICY_SHA. Cite frozen head tests/config already examined. A repaired bug alone does not prove a missing-test defect. ')+
  'Never invent evidence, tests, requirements, findings or successful coverage to satisfy the schema. '+
  'If evidence is insufficient, explicitly record limitations and incomplete coverage; an empty findings array does not assert that unreviewed code is safe. '+
  'Before saying source is absent, search the frozen repository including unchanged files on base/head and read the full relevant definitions. search_code is literal; locate test filenames using path_glob and query="". '+
  'Report missing facts as evidence_requests:[{tool:"search_code|read_file",arguments:{...},reason:"..."}] when further lookup is needed. '+
  'Use coverage_statuses:[{dimension:"...",status:"covered|not_applicable|incomplete",reason:"...",evidence_refs:[]}] for assessed dimensions; incomplete_dimensions contains only unresolved checks, never covered/not-applicable statements. Do not mark covered without supporting facts. '+
  (role==='synthesizer'?'Inherited limitations carry stable coverage_id values: echo {coverage_id} instead of translating or paraphrasing them. New limitations require source_stage, slice_id, canonical dimension, target path/side and reason. Never invent coverage IDs.':'')+
  (role==='validator'?'You receive coverage.coverage_statuses with stable coverage_id values. If you independently resolve an inherited limitation, return coverage_resolutions:[{coverage_id,status:"covered|not_applicable",reason:"...",evidence_refs:[]}]. Cite concrete frozen code ranges actually provided/read; do not clear arbitrary limitations or budget exhaustion.':'');
}

export function parseObject(text:string):Record<string,any>{
 const trimmed=text.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
 const value=JSON.parse(trimmed);
 if(!value||Array.isArray(value)||typeof value!=='object')throw Error('stage must return a JSON object');
 return value;
}
