type J=Record<string,any>;
const dispositions=['actionable','fixed_in_head','pre_existing','no_regression','informational','needs_evidence'];
const statuses=['introduced','worsened','test_gap','fixed_in_head','pre_existing','no_regression','unproven'];

export function validateCausality(f:J){
 if(!dispositions.includes(f.disposition))throw Error('finding disposition required');
 if(f.disposition!=='actionable')return;
 const c=f.change_causality;
 if(!c||!statuses.includes(c.status)||typeof c.reasoning!=='string'||!Array.isArray(c.change_unit_ids)||!Array.isArray(c.head_evidence_refs)||!Array.isArray(c.base_evidence_refs))throw Error('actionable finding requires structured change_causality');
}

/** Conservative classification, not a substitute for Validator code reasoning. */
export function nonDefectReason(f:J):string|null{
 const disposition=f.disposition,status=f.change_causality?.status;
 if(disposition&&disposition!=='actionable')return disposition;
 if(['fixed_in_head','pre_existing','no_regression','unproven'].includes(status))return status;
 if(['refuted','not_a_defect','fixed'].includes(f.static_verdict))return 'not_a_defect';
 if(status==='test_gap')return null; // A test gap has its own, independent burden of proof.
 if(/确认无(?:.*)?回归|无需修改|no regression|no fix required/i.test(f.title||''))return 'no_regression';
 if(f.side==='base'&&/head\s*版本修复方案正确|head.*(?:已修复|修复方案正确)|head.*fix.*correct/i.test(f.recommendation||f.fix||''))return 'fixed_in_head';
 if(!f.change_causality)return 'needs_evidence';
 if(!['introduced','worsened'].includes(status))return 'needs_evidence';
 if(!f.change_causality.reasoning.trim()||!f.change_causality.change_unit_ids.length||!f.change_causality.head_evidence_refs.length)return 'needs_evidence';
 return null;
}

export function screenFindings(findings:J[],stage:string,audit:J[]):J[]{
 return findings.filter(f=>{
  let reason=nonDefectReason(f);
  if(f.change_causality?.status==='test_gap'){
   const gap=f.test_gap;
   if(!gap||!gap.expected_behavior||!gap.missing_scenario||!gap.assertion_source||!gap.why_insufficient||!gap.existing_test_evidence_refs?.length)reason='unsupported_test_gap';
  }
  if(reason){audit.push({finding_id:f.finding_id,source_stage:stage,reason,title:f.title,finding:f});return false;}
  return true;
 });
}

export function coverageText(value:any):string{
 if(typeof value==='string')return value;
 if(value&&typeof value==='object'){
  const label=[value.dimension,value.reason||value.description].filter(Boolean).map(v=>typeof v==='string'?v:JSON.stringify(v)).join(': ');
  return label?label+(value.evidence_refs?.length?' | evidence: '+JSON.stringify(value.evidence_refs):''):JSON.stringify(value);
 }
 return String(value);
}
