type J=Record<string,any>;
const aliases:Record<string,string>={authentication:'authentication',authorization:'authorization',authn:'authentication',authz:'authorization',data_exposure:'data_exposure',test_coverage:'test_discovery',interface_contract:'call_chain',concurrency:'concurrency',evidence_collection:'budget',context_excerpt:'context_excerpt'};
export function coverageIdentity(value:any,text:string):J{
 const dimension=String(value?.dimension||'').toLowerCase().replace(/-/g,'_');
 const source=/^(correctness|security|contract_test|validator)[:.](.+)/.exec(dimension)||/^(correctness|security|contract_test|validator)[:.]([^:]+):/.exec(text);
 const raw=source?source[2]:dimension;
 let category=aliases[raw]||raw;
 const t=text.toLowerCase();
 if((!category||category==='baseline')&&/stage.*failed|model.*response.*fail|模型.*(?:失败|错误)|审查.*失败/.test(t))category='reviewer_failure';
 if(/xss|v-html|模板.*渲染|template.*render/.test(t)&&(!category||category==='injection'))category='xss_rendering';
 if(!category){
  if(/apiV1|apiv1|interceptor|拦截器|token.*(?:attachment|injection)|认证.*机制/.test(t))category='authentication';
  else if(/后端.*授权|backend.*authoriz|endpoint.*authoriz/.test(t))category='authorization';
  else if(/前端测试.*(?:存在|检索|文件)|(?:front.?end|component).*test.*(?:exist|file)|\.spec.*\.test/.test(t))category='test_discovery';
  else if(/调用链|依赖候选|caller|callee|call.chain|dependency.*candidate/.test(t))category='call_chain';
  else if(/并发|竞态|concurrency|race.*window/.test(t))category='concurrency';
  else if(/未执行.*测试|测试.*未运行|tests?.*(?:not run|not executed)|未执行.*(?:jest|vitest)/.test(t))category='test_execution';
 }
 const refs=Array.isArray(value?.evidence_refs)?value.evidence_refs:[];
 const paths=[...new Set([value?.path,...refs.map((r:any)=>{
  const s=typeof r==='string'?r:r?.evidence_ref||r?.ref||r?.path||'';
  return /^code:\/\/[^/]+\/(?:base|head)\/(.+)#L\d/.exec(s)?.[1]||(/\.(vue|py|tsx?|jsx?)(?::\d+)?$/.test(s)?s.replace(/:\d+$/,''):null);
 })].filter(Boolean))].sort();
 const sides=[...new Set(refs.flatMap((r:any)=>{const s=typeof r==='string'?r:r?.ref||r?.evidence_ref||'';const m=/^code:\/\/[^/]+\/(base|head)\//.exec(s);return m?[m[1]]:[]}))].sort();
 const namedFiles=[...new Set(text.match(/[\w.-]+\.(?:vue|py|tsx?|jsx?)/g)||[])].sort();
 const condition=/tool_limit|turn_limit|budget.*exhaust|预算.*耗尽/.test(t)?'budget':/not run|not executed|未执行|未运行/.test(t)?'not_run':/tests?.*fail|测试.*失败/.test(t)?'failed':'unverified';
 return {category,source_stage:value?.source_stage||source?.[1],slice_id:value?.slice_id,paths,sides,namedFiles,condition};
}
export function compatibleCoverage(a:J,b:J):boolean{
 if(!a.category||a.category!==b.category||a.condition!==b.condition)return false;
 if(a.paths.length&&b.paths.length&&JSON.stringify(a.paths)!==JSON.stringify(b.paths))return false;
 if(a.sides.length&&b.sides.length&&JSON.stringify(a.sides)!==JSON.stringify(b.sides))return false;
 const fileNames=(x:J)=>x.paths.length?x.paths.map((p:string)=>p.split('/').pop()).sort():x.namedFiles;
 const af=fileNames(a),bf=fileNames(b);
 if(af.length&&bf.length&&!af.some((p:string)=>bf.includes(p)))return false;
 return true;
}
