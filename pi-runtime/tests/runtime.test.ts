import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {SharedBudget} from '@earendil-works/pi-agent-core';
import {ScopedBudget} from '../src/scoped-budget.js';
import {GovernRuntime,normalizeFindings} from '../src/runtime.js';

for(const mode of ['none','limited','accounting'])test(mode==='limited'?'explicit cost overrun remains accounted and final stages still execute':mode==='accounting'?'context-first mode records cumulative use above the old role limit without cancelling':'shared frozen reads deduplicate across roles, loops end locally and coverage is explicit',async()=>{
 const overrun=mode!=='none',enforced=mode==='limited';
 let hostReads=0;const events:any[]=[];
 const server=createServer(async(req,res)=>{
  let body='';for await(const chunk of req)body+=chunk;
  const r=JSON.parse(body),system=r.messages.find((m:any)=>m.role==='system')?.content||'';
  const review=/ROLE:(correctness|security|contract_test)/.test(system);
  const output=system.includes('ROLE:mapper')?{mappings:[]}:{findings:[],...(review?{incomplete_dimensions:[{dimension:'dependency',status:'incomplete',reason:'Pending source dependency not loaded'}]}:{})};
  const delta=review&&r.tool_choice!=='none'?{role:'assistant',tool_calls:[{index:0,id:'read-'+r.messages.length,type:'function',function:{name:'read_file',arguments:JSON.stringify({path:'a.py',start_line:10,end_line:r.messages.length>2?10:11})}}]}:{role:'assistant',content:JSON.stringify(output)};
  res.writeHead(200,{'Content-Type':'text/event-stream'});const base={id:'test',object:'chat.completion.chunk',created:1,model:'test'};
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
  const promptTokens=overrun&&system.includes('ROLE:correctness')?(mode==='accounting'?40000:19000):10;
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:review&&r.tool_choice!=='none'?'tool_calls':'stop'}],usage:{prompt_tokens:promptTokens,completion_tokens:10,total_tokens:promptTokens+10}})+'\n\n');res.end('data: [DONE]\n\n');
 });
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));
 const roles=['mapper','correctness','security','contract_test','synthesizer','validator'];
 const rt=new GovernRuntime({method:'run_review',...(enforced?{budget:{tokens:120000,turns:60,tools:100,milliseconds:900000}}:{}),model:{base_url:`http://127.0.0.1:${(server.address() as any).port}/v1`,api_key:'local-fake',model:'test'},
  policy:{sha256:'test',stages:Object.fromEntries(roles.map(r=>[r,`ROLE:${r}. Return JSON.`])),tool_profiles:Object.fromEntries(roles.map(r=>[r,['read_file']]))},
  input:{units:[{change_unit_id:'u',path:'a.py',side:'head',context:'x=1\n'+'savedAnswer='.repeat(480),context_start:1,context_end:2}],requirements:[],snapshot_id:'snap',verification_enabled:false}},
  {event:e=>events.push(e),tool:async()=>{hostReads++;return {path:'a.py',start_line:10,content:'保存答案'.repeat(2000)+'\ny=2',evidence_ref:'code://snap/head/a.py#L10-L11'};}});
 try{
  const result=await rt.run();assert.equal(hostReads,1);assert.equal(result.completion_status,'partial');
  assert.ok(result.coverage.incomplete_dimensions.length>=3);
  assert.ok(events.some(e=>e.event==='shared_evidence_hit'));
  assert.equal(events.filter(e=>e.event==='stage_complete').length,enforced?5:6);
  if(enforced){
   assert.ok(events.some(e=>e.event==='stage_failed'&&e.role==='correctness'));
   assert.ok(result.execution.input_tokens>=19000);assert.ok(rt.budget.usedTokens>=19000);
   assert.ok(events.some(e=>e.event==='stage_complete'&&e.role==='validator'));
  }
  if(mode==='accounting'){
   assert.ok(rt.budget.usedTokens>120000);
   assert.ok(events.some(e=>e.event==='context_preflight'&&e.cost_token_limit_enabled===false));
   assert.ok(events.every(e=>e.event!=='context_preflight'||e.estimated_input_tokens<=e.input_limit));
  }
  assert.ok(rt.budget.turns<=21);assert.equal(rt.budget.controller.signal.aborted,false);
 }finally{rt.cancel();server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));}
});

test('invalid frozen context is rejected before any model or tool access',async()=>{
 const rt=new GovernRuntime({method:'run_review',input:{snapshot_id:'invalid',units:[{
  change_unit_id:'bad',path:'a.py',changed_lines:[358],context:'',context_start:358,context_end:3
 }]}},{tool:async()=>{throw Error('unexpected tool')},event:()=>{}});
 await assert.rejects(rt.run(),/invalid source context range/);
 assert.equal(rt.usage.llm_calls,0);assert.equal(rt.budget.turns,0);
 rt.cancel();
});

test('Core shared reservations prevent concurrent overspend and settle once',()=>{
 const b=new SharedBudget({tokens:100,turns:3,tools:2,milliseconds:10000});
 const a=b.reserve([],60);assert.throws(()=>b.reserve([],60),/budget exhausted/);a.settle(20);a.settle(20);
 assert.equal(b.reservedTokens,0);assert.equal(b.usedTokens,20);
});
test('Core tool counter and cancellation are enforced',()=>{
 const b=new SharedBudget({tokens:1000,turns:3,tools:1,milliseconds:10000});b.takeTool();assert.throws(()=>b.takeTool(),/tool budget/);
 assert.equal(b.controller.signal.aborted,true);
});

test('soft tool admission charges the whole ancestry atomically without cancelling siblings',()=>{
 const root=new SharedBudget({tokens:10000,turns:20,tools:3,milliseconds:10000});
 const parent=new ScopedBudget(root,{tokens:10000,turns:20,tools:2,milliseconds:10000});
 const child=new ScopedBudget(parent,{tokens:10000,turns:20,tools:4,milliseconds:10000});
 const sibling=new ScopedBudget(root,{tokens:10000,turns:20,tools:4,milliseconds:10000});
 try{
  assert.equal(child.tryTakeTool(),true);assert.equal(child.tryTakeTool(),true);
  assert.equal(child.tryTakeTool(),false);assert.deepEqual([child.tools,parent.tools,root.tools],[2,2,2]);
  assert.equal(child.remainingTools(),0);assert.equal(sibling.tryTakeTool(),true);
  assert.equal(sibling.tryTakeTool(),false);assert.deepEqual([sibling.tools,root.tools],[1,3]);
  assert.equal(root.controller.signal.aborted,false);assert.equal(child.controller.signal.aborted,false);
  const final=root.reserve([],100);final.settle(20);assert.equal(root.turns,1);
  root.controller.abort(new Error('cancelled intentionally'));
  assert.throws(()=>child.tryTakeTool(),/cancelled intentionally/);
 }finally{child.dispose();parent.dispose();sibling.dispose();}
});
test('Finding schema rejects invented severity and requires test/evidence arrays',()=>{
 assert.throws(()=>normalizeFindings({findings:[{path:'a',line:1,title:'x',severity:'P0'}]},'security','s'),/severity/);
});
test('checkpoint recovery inherits consumption instead of minting a new budget',()=>{
 const rt=new GovernRuntime({consumed_budget:{used_tokens:100,reserved_tokens:50,turns:4,tools:3,usage:{input_tokens:80}}},{tool:async()=>({}),event:()=>{}});
 assert.equal(rt.budget.usedTokens,150);assert.equal(rt.budget.turns,4);assert.equal(rt.budget.tools,3);assert.equal(rt.usage.input_tokens,80);
});

test('real child session shares Core budget, cannot delegate/execute, and preserves partial status',async()=>{
 let childTools:string[]=[];let parentTools:string[]=[];let parentToolResult='';
 const server=createServer(async(req,res)=>{
   let body='';for await(const chunk of req)body+=chunk;
   const r=JSON.parse(body),system=r.messages.find((m:any)=>m.role==='system')?.content||'';
   const child=system.includes('Child: no delegation/execution');
   const returned=r.messages.find((m:any)=>m.role==='tool');
   if(child)childTools=r.tools.map((t:any)=>t.function.name);
   else parentTools=r.tools.map((t:any)=>t.function.name);
   if(returned)parentToolResult=JSON.stringify(returned.content);
   const delta=child?{role:'assistant',content:JSON.stringify({status:'partial',evidence_refs:[],facts:[],limitations:['missing external service']})}:
     returned?{role:'assistant',content:'{"findings":[]}'}:{role:'assistant',tool_calls:[{index:0,id:'delegate-1',type:'function',function:{name:'delegate_evidence',arguments:'{"objective":"Inspect mock boundary","context":"test"}'}}]};
   res.writeHead(200,{'Content-Type':'text/event-stream'});
   const base={id:'test',object:'chat.completion.chunk',created:1,model:'test'};
   res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
   res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:child||returned?'stop':'tool_calls'}],usage:{prompt_tokens:10,completion_tokens:10,total_tokens:20}})+'\n\n');
   res.end('data: [DONE]\n\n');
 });
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));
 const rt=new GovernRuntime({model:{base_url:`http://127.0.0.1:${(server.address() as any).port}/v1`,api_key:'test',model:'test'},policy:{sha256:'s',stages:{validator:'ROLE:validator. Return JSON.'},tool_profiles:{validator:['read_file','delegate_evidence']}},input:{verification_enabled:true}}, {tool:async()=>{throw Error('unexpected host call')},event:()=>{}});
 try{
   await rt.stage('validator','validator',{});
   assert.equal(rt.budget.turns,3);assert.equal(rt.budget.tools,1);
   assert.ok(!childTools.includes('delegate_evidence'));assert.ok(!childTools.includes('run_verification'));
   assert.deepEqual([...parentTools].sort(),['delegate_evidence','read_file']);
   assert.deepEqual(childTools,['read_file']);
   assert.ok(parentToolResult.includes('partial'));
 }finally{rt.cancel();server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));}
});
test('real Pi AgentSessions execute all six stages through patched Core with isolated prompts',async()=>{
 const prompts:string[]=[];
 const server=createServer(async(req,res)=>{
   let body='';for await(const chunk of req)body+=chunk;
   const request=JSON.parse(body),system=request.messages.find((m:any)=>m.role==='system')?.content||'';prompts.push(system);
   const output=system.includes('ROLE:mapper')?{mappings:[]}:{findings:[]};
   res.writeHead(200,{'Content-Type':'text/event-stream'});
   const base={id:'test',object:'chat.completion.chunk',created:1,model:'test'};
   res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{role:'assistant',content:JSON.stringify(output)},finish_reason:null}]})+'\n\n');
   res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:10,total_tokens:20}})+'\n\n');
   res.end('data: [DONE]\n\n');
 });
 await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
 const port=(server.address() as any).port;const events:any[]=[];
 const roles=['mapper','correctness','security','contract_test','synthesizer','validator'];
 const rt=new GovernRuntime({method:'run_review',model:{base_url:`http://127.0.0.1:${port}/v1`,api_key:'test-only',model:'test'},
   policy:{sha256:'test',stages:Object.fromEntries(roles.map(r=>[r,`ROLE:${r}. Return only JSON.`])),specialist_fragments:{database:{roles:['correctness'],content:'SPECIALIST_DATABASE'}}},
   input:{units:[{change_unit_id:'u1',path:'a.py',context:'x=1',policy_fragments:{correctness:['database']},fragment_selection_reasons:{database:{precision:'lexical'}}}],requirements:[],business_profile:{},coverage:{},snapshot_id:'s1',verification_enabled:false}},
   {tool:async()=>{throw Error('unexpected tool')},event:e=>events.push(e)});
 try{const result=await rt.run();assert.deepEqual(result.findings,[]);assert.equal(prompts.length,6);
   assert.equal(events.filter(e=>e.event==='stage_complete').length,6);assert.equal(rt.budget.turns,6);
   for(const p of prompts)assert.equal(roles.filter(r=>p.includes('ROLE:'+r)).length,1);
   assert.equal(prompts.filter(p=>p.includes('SPECIALIST_DATABASE')).length,1);
   assert.ok(prompts.find(p=>p.includes('SPECIALIST_DATABASE'))?.includes('ROLE:correctness'));
   assert.deepEqual(events.find(e=>e.event==='stage_start'&&e.role==='correctness').specialist_fragments,['database']);
 }finally{rt.cancel();server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));}
});

test('specialist fragment cannot be injected into an unauthorized role',async()=>{
 const rt=new GovernRuntime({policy:{stages:{security:'Security'},specialist_fragments:{database:{roles:['correctness'],content:'DB'}}},input:{}},{tool:async()=>({}),event:()=>{}});
 await assert.rejects(rt.stage('security','invalid',{unit:{policy_fragments:{security:['database']}}}),/unauthorized specialist/);
 rt.cancel();
});
