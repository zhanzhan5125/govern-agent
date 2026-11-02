export const roleNames={mapper:'需求映射',correctness:'正确性',security:'安全性',contract_test:'契约与测试',synthesizer:'结果综合',validator:'独立核验'};
export const statusNames={DRAFT:'候选已创建',STATIC_PASSED:'静态检查通过，待评测',VALIDATION_PASSED:'Validation通过，待Holdout',HOLDOUT_PASSED:'评测通过，待人工确认',HUMAN_APPROVED:'人工已确认，待发布',SHADOW:'旧流程记录，需重新生成评测',ACTIVE:'当前生效',SUPERSEDED:'已被后续版本替代',ROLLED_BACK:'激活后已撤回',REJECTED:'已拒绝'};
export function changedStages(c){return Object.keys(roleNames).filter(r=>(c.content?.files?.[`stages/${r}.md`]||'')!==(c.baseline?.content?.files?.[`stages/${r}.md`]||''));}
export function versionControls(c,head){
 const current=head?.candidate_id===c.id;
 return {current,publish:!current&&['HOLDOUT_PASSED','HUMAN_APPROVED'].includes(c.status),restore:!current&&['SUPERSEDED','ROLLED_BACK'].includes(c.status)&&!!c.reports?.release};
}
export function candidateActions(c,head){
 if(['SUPERSEDED','ROLLED_BACK'].includes(c.status))return c.reports?.release&&c.id!==head?.candidate_id?['rollback']:[];
 return ({STATIC_PASSED:['evaluate','reject'],VALIDATION_PASSED:['evaluate','reject'],HOLDOUT_PASSED:['approve','reject'],HUMAN_APPROVED:['activate','reject']}[c.status]||[]);
}
export function versionGroups(data){
 const candidates=data?.candidates||[],batches=data?.batches||[];
 const groups=batches.map(b=>({...b,candidates:candidates.filter(c=>c.lineage?.batch_id===b.id)}));
 const ids=new Set(batches.map(b=>b.id));
 for(const c of candidates.filter(c=>!ids.has(c.lineage?.batch_id)))groups.push({id:c.id,version:null,created_at:c.created_at,status:'HISTORICAL',candidates:[c],baseline:c.lineage?.source==='pre-governance-baseline'});
 return groups;
}
