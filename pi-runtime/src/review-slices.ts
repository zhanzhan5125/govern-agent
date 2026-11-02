import {createHash} from 'node:crypto';
type J=Record<string,any>;
const hash=(x:any)=>createHash('sha256').update(JSON.stringify(x)).digest('hex').slice(0,20);
const isTest=(u:J)=>/(^|\/)(tests?|__tests__)\/|(^|\/)(test_|.*\.(test|spec)\.)/.test(u.path)||/^test_/.test(u.symbol?.name||'');
const tokens=(u:J)=>new Set<string>((u.path.split('/').pop()+' '+(u.symbol?.name||'')+' '+
 ((u.context||'').match(/[A-Za-z_][A-Za-z_0-9]*|["'`]\/[^"'`\s]+/g)?.join(' ')||'')).replace(/([a-z])([A-Z])/g,'$1 $2').toLowerCase()
 .split(/[^a-z0-9]+/).map((t:string)=>t.endsWith('s')?t.slice(0,-1):t).filter((t:string)=>t.length>=4&&!['test','spec','file','load','view','current','uses','this','const','return','function','assert','true','false','none','self','content','undefined','null'].includes(t)));

/** Deterministic behavior grouping, not a sound call graph or an LLM inference. */
export function buildReviewSlices(units:J[],mappings:J[],snapshot:string,maxChars=16000,maxUnits=8):J[]{
 const groups:J[]=[];
 for(const u of units){
  if(!u.path||/[\r\n]/.test(u.path)||typeof u.context!=='string')throw Error('invalid slice source');
  if(u.context_end!==undefined&&u.context_start>u.context_end)throw Error('invalid source context range');
  if(u.changed_lines?.length&&!u.context&&!u.context_blank)throw Error('changed source context is empty');
  const name=u.symbol?.name||'<file>',key=u.path+'::'+name;
  let group=groups.find(g=>g.key===key&&g.units.some((a:J)=>Math.abs((a.context_start||1)-(u.context_start||1))<160));
  if(!group&&u.symbol?.kind==='lexical_declaration')group=groups.find(g=>g.units.some((a:J)=>
   a.path===u.path&&a.side===u.side&&a.symbol?.kind!=='lexical_declaration'&&
   a.symbol?.start<=u.symbol.start&&a.symbol?.end>=u.symbol.end));
  if(!group){group={key,units:[],basis:['same-file-symbol-and-nearby-range'],test:isTest(u)};groups.push(group);}
  group.units.push(u);
 }
 // Join changed definitions with changed callers only when a frozen reference lands
 // inside the other changed symbol. These remain lexical candidate edges, not proof.
 for(let i=0;i<groups.length;i++){
  const a=groups[i];if(a.test)continue;
  for(let j=i+1;j<groups.length;){
   const b=groups[j];
   const links=(from:J,to:J)=>from.units.some((u:J)=>{
    const name=(u.symbol?.name||'').split('.').pop();
    return name.length>=4&&!['load','main','render','setup'].includes(name)&&
     (u.reference_candidates||[]).some((r:J)=>to.units.some((v:J)=>v.path!==u.path&&v.path===r.path&&v.side==='head'&&
      v.symbol?.start<=r.line&&v.symbol?.end>=r.line));
   });
   if(!b.test&&(links(a,b)||links(b,a))){a.units.push(...b.units);a.basis.push('changed-symbol-reference-candidate');groups.splice(j,1);j=i+1;}
   else j++;
  }
 }
 // Attach changed tests to the strongest matching production behavior; ties stay separate.
 for(const g of [...groups].filter(g=>g.test)){
  const testTokens=new Set(g.units.flatMap((u:J)=>[...tokens(u)]));
  const ranked=groups.filter(p=>!p.test).map(p=>{
   const terms=new Set(p.units.flatMap((u:J)=>[...tokens(u)]));
   return {p,score:[...terms].filter(t=>testTokens.has(t)).length};
  }).sort((a,b)=>b.score-a.score||a.p.key.localeCompare(b.p.key));
  if(ranked[0]?.score>=2&&ranked[0].score>(ranked[1]?.score||0)){
   ranked[0].p.units.push(...g.units);ranked[0].p.basis.push('changed-test-lexical-behavior-overlap');
   groups.splice(groups.indexOf(g),1);
  }
 }
 const slices:J[]=[];
 for(const g of groups){
  let batch:J[]=[],chars=0;
  const flush=()=>{
   if(!batch.length)return;
   const ids=batch.map(u=>u.change_unit_id),sid='slice-'+hash([snapshot,ids]);
   const windows=batch.filter(u=>!batch.some(a=>a!==u&&a.path===u.path&&a.side===u.side&&
    a.context_start<=u.context_start&&a.context_end>=u.context_end&&a.context.length>u.context.length));
   const evidence=windows.map(u=>{
    const text=u.context.slice(0,6000),start=u.context_start||1;
    return {evidence_id:'ev-'+hash([snapshot,u.path,u.side||'head',start,text]),snapshot_id:snapshot,
     path:u.path,side:u.side||'head',start_line:start,end_line:start+text.split('\n').length-1,
     content:text,excerpt_truncated:!!u.context_truncated||text.length<u.context.length};
   });
   const profiles:Record<string,string[]>={};
   for(const role of ['correctness','security','contract_test'])profiles[role]=[...new Set(batch.flatMap(u=>u.policy_fragments?.[role]||[]))];
   slices.push({slice_id:sid,behavior_key:g.key,unit_ids:ids,grouping_basis:g.basis,grouping_precision:'heuristic',
    units:batch.map(({context,hunks,reference_candidates,...metadata})=>metadata),evidence_pack:evidence,
    mappings:mappings.filter(m=>ids.includes(m.change_unit_id)),
    dependency_candidates:batch.flatMap(u=>u.reference_candidates||[]).slice(0,12),policy_fragments:profiles,
    limitations:evidence.some(e=>e.excerpt_truncated)?['Evidence excerpt is bounded; additional frozen source must be read or coverage marked incomplete.']:[],
    related_slice_ids:[]});
   batch=[];chars=0;
  };
  for(const u of g.units){const size=Math.min(6000,u.context.length);if(batch.length&&(batch.length>=maxUnits||chars+size>maxChars))flush();batch.push(u);chars+=size;}
  flush();
 }
 for(const a of slices)for(const b of slices)if(a!==b&&(a.behavior_key===b.behavior_key||a.dependency_candidates.some((r:J)=>b.units.some((u:J)=>r.path===u.path))))a.related_slice_ids.push(b.slice_id);
 const covered=slices.flatMap(s=>s.unit_ids);
 if(covered.length!==units.length||new Set(covered).size!==covered.length)throw Error('slice coverage is not one-to-one');
 return slices;
}

export function mapperBatches(units:J[],maxChars=14000):J[][]{
 const batches:J[][]=[];let batch:J[]=[],size=0;
 for(const u of units){
  const item={change_unit_id:u.change_unit_id,path:u.path,side:u.side,symbol:u.symbol,changed_lines:u.changed_lines,
   context_start:u.context_start,context:u.context.slice(0,1600),context_excerpt:u.context.length>1600,
   reference_candidates:(u.reference_candidates||[]).slice(0,4)};
  const n=JSON.stringify(item).length;
  if(batch.length&&(size+n>maxChars||batch.length>=12)){batches.push(batch);batch=[];size=0;}
  batch.push(item);size+=n;
 }
 if(batch.length)batches.push(batch);return batches;
}
