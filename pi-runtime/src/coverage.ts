import {coverageText} from './finding-gate.js';
import {createHash} from 'node:crypto';
import {coverageIdentity,compatibleCoverage} from './coverage-identity.js';
type J=Record<string,any>;
const roles='correctness|security|contract_test|synthesizer|validator|pipeline';
const missing=/(未包含|未找到|缺少|不完整|未审查|未评估|无法.*(?:核验|验证|评估)|not (?:loaded|found|included|available|reviewed|verified)|unverified|missing|insufficient|incomplete)/i;
export function coverageStatus(value:any):string{
 const text=coverageText(value),status=String(value?.status||'').toLowerCase();
 if(['incomplete','unverified','blocked','unknown'].includes(status))return 'incomplete';
 if(['covered','not_applicable'].includes(status)&&value?.reason&&!missing.test(value.reason))return status;
 if(/声明为已覆盖|^covered\b/i.test(text)&&!missing.test(text))return 'reported_covered';
 if(/does not introduce new|unchanged from base|无回归|未引入新/i.test(text)&&!missing.test(text))return 'observation';
 return 'incomplete';
}
export function missingEvidence(result:J):boolean{
 if(Array.isArray(result.evidence_requests)&&result.evidence_requests.length)return true;
 return [...(result.incomplete_dimensions||[]),...(result.limitations||[])].some(v=>
  coverageStatus(v)==='incomplete'&&missing.test(coverageText(v))&&/源码|代码|函数|方法|测试|调用|后端|拦截器|source|implementation|caller|callee|test|handler|backend|endpoint|interceptor|apiV1/i.test(coverageText(v)));
}
export function canFollowup(result:J,state:{attempts:number;formatAttempt:number;closed:boolean;turns:number;turnLimit:number;tools:number;toolLimit:number}):boolean{
 return missingEvidence(result)&&state.attempts<1&&state.formatAttempt===0&&!state.closed&&state.turns<state.turnLimit-1&&state.tools<state.toolLimit-1;
}
export function rangeCovered(path:string,side:string,start:number,end:number,windows:J[]):boolean{
 let next=start;
 for(const w of windows.filter(w=>w.path===path&&w.side===side).sort((a,b)=>a.start_line-b.start_line)){
  if(w.start_line>next)return false;
  next=Math.max(next,w.end_line+1);if(next>end)return true;
 }
 return false;
}
export class CoverageLedger{
 readonly incomplete:string[]=[];readonly details:J[]=[];readonly observations:J[]=[];readonly statuses:J[]=[];
 private seen=new Set<string>();
 add(role:string,slice:string,values:any[]){
  for(const value of values){
   const inherited=value?.coverage_id&&this.statuses.find(s=>s.coverage_id===value.coverage_id);
   if(inherited){(inherited.variants||=[]).push({source_stage:role,slice_id:slice,value});continue;}
   const status=coverageStatus(value);
   if(status!=='incomplete'){this.observations.push({source_stage:role,slice_id:slice,status,value});this.statuses.push({source_stage:role,slice_id:slice,status,dimension:value?.dimension||null});continue;}
   let text=coverageText(value),originRole=role,originSlice=slice;
   // A synthesizer can repeat inherited limitations, but must not acquire ownership.
   const prefix=new RegExp('^(global|slice-[^:]+):('+roles+'):');
   let match;let n=0;
   while((match=prefix.exec(text))&&n++<5){originSlice=match[1];originRole=match[2];text=text.slice(match[0].length);}
   const identity=coverageIdentity(value,text);
   if(['correctness','security','contract_test','validator','pipeline'].includes(identity.source_stage))originRole=identity.source_stage;
   if(typeof identity.slice_id==='string')originSlice=identity.slice_id;
   const matches=this.statuses.filter(s=>s.status==='incomplete'&&s.source_stage===originRole&&
     (s.slice_id===originSlice||(originSlice==='global'&&role!==originRole))&&compatibleCoverage(s.identity||{},identity));
   if(matches.length===1){
    const existing=matches[0];(existing.variants||=[]).push({source_stage:role,slice_id:slice,value});
    if(!existing.identity.paths.length&&identity.paths.length)existing.identity=identity;
    continue;
   }
   const key=[originSlice,originRole,text.replace(/（如[^）]*）|\(e\.g\.[^)]*\)/gi,'').replace(/\s+/g,'').replace(/[：]/g,':').toLowerCase()].join('|');
   if(this.seen.has(key))continue;this.seen.add(key);
   const id='coverage-'+createHash('sha256').update(key).digest('hex').slice(0,20),rendered=originSlice+':'+originRole+':'+text;
   this.incomplete.push(rendered);
   if(value&&typeof value==='object')this.details.push({value,source_stage:originRole,slice_id:originSlice,coverage_id:id});
   this.statuses.push({coverage_id:id,rendered,source_stage:originRole,slice_id:originSlice,status:'incomplete',dimension:value?.dimension||null,reason:text,identity,variants:[]});
  }
 }
 resolve(values:J[],hasEvidence:(ref:any)=>boolean){
  for(const resolution of values){
   const entry=this.statuses.find(s=>s.coverage_id===resolution.coverage_id&&s.status==='incomplete');
   if(!entry||entry.dimension==='evidence_collection'||!['covered','not_applicable'].includes(resolution.status)||!resolution.reason||!Array.isArray(resolution.evidence_refs)||!resolution.evidence_refs.length||!resolution.evidence_refs.every(hasEvidence))continue;
   const index=this.incomplete.indexOf(entry.rendered);if(index>=0)this.incomplete.splice(index,1);
   for(let i=this.details.length-1;i>=0;i--)if(this.details[i].coverage_id===entry.coverage_id)this.details.splice(i,1);
   Object.assign(entry,{status:resolution.status,resolution_reason:resolution.reason,evidence_refs:resolution.evidence_refs});
   this.observations.push({source_stage:'validator',coverage_id:entry.coverage_id,status:resolution.status,value:resolution});
  }
 }
}
