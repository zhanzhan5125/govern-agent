import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {GovernRuntime,object} from '../src/runtime.js';

const roles=['mapper','correctness','security','contract_test','synthesizer','validator'];
type Reply={text?:string;tool?:boolean;toolCount?:number;toolName?:string;args?:any;httpError?:boolean;errorMessage?:string;errorCode?:string;requestId?:string};
async function fixture(reply:(role:string,attempt:number,request:any)=>Reply,eventHook?:(e:any,r:GovernRuntime)=>void){
 const calls:any[]=[],events:any[]=[];let hostCalls=0;const counts:Record<string,number>={};
 const server=createServer(async(req,res)=>{
  let body='';for await(const chunk of req)body+=chunk;
  const request=JSON.parse(body),system=request.messages.find((m:any)=>m.role==='system')?.content||'';
  const role=/ROLE:(\w+)/.exec(system)?.[1]||'unknown';
  const attempt=counts[role]=(counts[role]||0)+1;calls.push({role,attempt,request});
  let output:Reply;
  try{output=reply(role,attempt,request);}catch(error){res.writeHead(400,{'Content-Type':'application/json'});res.end(JSON.stringify({error:{message:String(error),type:'test_fixture_assertion'}}));return;}
  if(output.httpError){res.writeHead(400,{'Content-Type':'application/json','x-request-id':output.requestId||'req-local-test'});res.end(JSON.stringify({error:{message:output.errorMessage||'local test rejection',type:'test_error',code:output.errorCode}}));return;}
  const delta=output.tool?{role:'assistant',tool_calls:Array.from({length:output.toolCount||1},(_,i)=>({index:i,id:'c'+attempt+'-'+i,type:'function',function:{name:output.toolName||'read_file',arguments:JSON.stringify(output.args||{path:'a.py',start_line:1,end_line:1})}}))}:{role:'assistant',content:output.text};
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const base={id:'local',object:'chat.completion.chunk',created:1,model:'test'};
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:output.tool?'tool_calls':'stop'}],usage:{prompt_tokens:10,completion_tokens:10,total_tokens:20}})+'\n\n');
  res.end('data: [DONE]\n\n');
 });
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));
 const rt=new GovernRuntime({method:'run_review',model:{base_url:`http://127.0.0.1:${(server.address() as any).port}/v1`,api_key:'local-fake',model:'test'},
  policy:{sha256:'test',stages:Object.fromEntries(roles.map(r=>[r,`ROLE:${r}. Review frozen code.`])),tool_profiles:Object.fromEntries(roles.map(r=>[r,['read_file']]))},
  input:{units:[{change_unit_id:'u',path:'a.py',side:'head',context:'x=1',context_start:1,context_end:1}],requirements:[],snapshot_id:'snap',verification_enabled:true}},
  {event:e=>{events.push(e);eventHook?.(e,rt);},tool:async()=>{hostCalls++;return {path:'a.py',start_line:1,content:'x=1'};}});
 return {rt,calls,events,counts,get hostCalls(){return hostCalls;},close:async()=>{rt.cancel();server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));}};
}

for(const [role,text] of [['correctness','Now let me inspect the callers.'],['security','Now let me check authorization.'],['contract_test','Let me also inspect the relevant tests.'],['validator','我收到了这次验证任务，需要先核对证据。']]){
 test(role+' repairs the actual narrative failure without new tools or new budget',async()=>{
  const f=await fixture((_r,n)=>({text:n===1?text:'{"findings":[],"limitations":["Incomplete evidence; no safety assertion."]}'}));
  try{
   const result=await f.rt.stage(role,role,{});
   assert.deepEqual(result.findings,[]);assert.equal(f.calls.length,2);assert.equal(f.hostCalls,0);
   for(const call of f.calls)assert.equal(call.request.max_tokens??call.request.max_completion_tokens,4000);
   const repair=f.calls[1].request;
   assert.equal(repair.tool_choice,'none');assert.equal((repair.tools||[]).length,0);
   assert.ok(JSON.stringify(repair.messages).includes('Repair the format/schema'));
   assert.ok(JSON.stringify(repair.messages).includes('GOVERN OUTPUT CONTRACT'));
   assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,1);
   assert.equal(f.rt.budget.turns,2);assert.equal(f.rt.budget.usedTokens,40);
  }finally{await f.close();}
 });
}

test('malformed JSON and missing schema use exactly two repair attempts',async()=>{
 const f=await fixture((_r,n)=>({text:n===1?'```json\n{"findings":\n```':n===2?'{}':'{"findings":[],"limitations":["Evidence unavailable"]}'}));
 try{
  await f.rt.stage('correctness','correctness',{});
  assert.equal(f.calls.length,3);assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,2);
  assert.equal(f.rt.usage.llm_calls,3);assert.equal(f.rt.budget.usedTokens,60);
 }finally{await f.close();}
});

test('invalid finding severity and mapping shape are repaired, not accepted as structured success',async()=>{
 for(const role of ['mapper','security']){
  const invalid=role==='mapper'?{mappings:[{change_unit_id:'u',status:'guessed',evidence_refs:[]}]}:{findings:[{path:'a.py',line:1,title:'x',severity:'P0',evidence_refs:[],related_tests:[]}]};
  const valid=role==='mapper'?{mappings:[{change_unit_id:'u',status:'unmapped',evidence_refs:[]}]}:{findings:[]};
  const f=await fixture((_r,n)=>({text:JSON.stringify(n===1?invalid:valid)}));
  try{await f.rt.stage(role,role,{});assert.equal(f.calls.length,2);assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,1);}
  finally{await f.close();}
 }
});

test('all three Reviewers fail closed and never call synthesis, Validator or sandbox',async()=>{
 const f=await fixture(role=>({text:role==='mapper'?'{"mappings":[]}':'Now let me continue the review.'}));
 try{
  await assert.rejects(f.rt.run(),/All three Reviewers failed/);
  assert.equal(f.calls.length,10);assert.equal(f.counts.synthesizer,undefined);assert.equal(f.counts.validator,undefined);
  for(const role of ['correctness','security','contract_test'])assert.equal(f.counts[role],3);
  assert.equal(f.hostCalls,0);assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,6);
  assert.equal(f.events.filter(e=>e.event==='stage_failed').length,3);
  assert.equal(f.events.filter(e=>e.event==='pipeline_aborted'&&e.reason==='all_reviewers_failed').length,1);
  assert.equal(f.events.filter(e=>e.event==='stage_complete').length,1);
  assert.equal(f.rt.budget.usedTokens,200);assert.equal(f.rt.budget.reservedTokens,0);
 }finally{await f.close();}
});

test('partial Reviewer failure remains explicit and does not short-circuit successful reviewers',async()=>{
 const f=await fixture(role=>({text:role==='mapper'?'{"mappings":[]}':['security','contract_test'].includes(role)?'Let me also inspect more code.':'{"findings":[]}'}));
 try{
  const result=await f.rt.run();assert.equal(result.completion_status,'partial');
  assert.equal(result.coverage.incomplete_dimensions.length,2);
  assert.equal(f.counts.synthesizer,1);assert.equal(f.counts.validator,1);
  assert.equal(f.events.filter(e=>e.event==='pipeline_aborted').length,0);
 }finally{await f.close();}
});

test('bounded evidence loop explicitly requests final JSON before no-tools completion',async()=>{
 const f=await fixture((_r,_n,r)=>r.tool_choice==='none'?{text:'{"findings":[],"limitations":["bounded evidence"]}'}:{tool:true});
 try{
  await f.rt.stage('correctness','correctness',{});
  const final=f.calls.at(-1).request;
  assert.equal(final.tool_choice,'none');assert.ok(JSON.stringify(final.messages).includes('Evidence collection is closed'));
  assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,0);
  assert.ok(f.calls.length<=6);
 }finally{await f.close();}
});

test('missing source triggers one bounded followup and reads the base definition before finishing',async()=>{
 const f=await fixture((_r,n)=>n===1?{text:JSON.stringify({findings:[],incomplete_dimensions:[{dimension:'source',reason:'changeProblem 完整源码未包含在本次切片中'}]})}:n===2?{tool:true,args:{path:'a.py',side:'base',start_line:1,end_line:1}}:{text:'{"findings":[],"coverage_statuses":[{"dimension":"source","status":"covered","reason":"Read base definition","evidence_refs":["code://snap/base/a.py#L1"]}]}'});
 try{const result=await f.rt.stage('contract_test','contract_test',{});
  assert.equal(f.calls.length,3);assert.equal(f.hostCalls,1);assert.equal(f.events.filter(e=>e.event==='evidence_followup').length,1);
  assert.ok(!f.calls[1].request.tool_choice||f.calls[1].request.tool_choice!=='none');
  assert.equal(f.events.find(e=>e.event==='tool_complete')?.arguments.side,'base');
  assert.equal(result.incomplete_dimensions,undefined);
 }finally{await f.close();}
});

test('format-only repair is not an evidence-budget failure',async()=>{
 const f=await fixture((_r,n)=>({text:n===1?'Let me inspect.':'{"findings":[]}'}));
 try{const result=await f.rt.stage('correctness','correctness',{});assert.equal(result.incomplete_dimensions,undefined);assert.equal(f.events.filter(e=>e.event==='evidence_collection_closed').length,0);}finally{await f.close();}
});

test('valid repaired JSON with missing evidence may resume tools once, without resetting budgets',async()=>{
 const f=await fixture((_r,n)=>n===1?{text:'Not JSON'}:n===2?{text:'{"findings":[],"incomplete_dimensions":["base implementation not loaded"]}'}:n===3?{tool:true,args:{path:'a.py',side:'base',start_line:1,end_line:1}}:{text:'{"findings":[]}'});
 try{await f.rt.stage('correctness','correctness',{});assert.equal(f.calls[1].request.tool_choice,'none');assert.equal(f.events.filter(e=>e.event==='evidence_followup').length,1);assert.equal(f.rt.budget.turns,4);assert.equal(f.hostCalls,1);}finally{await f.close();}
});

test('cancellation during repair admission prevents another model call',async()=>{
 const f=await fixture(()=>({text:'我收到了这次验证任务。'}),(e,rt)=>{if(e.event==='stage_output_retry')rt.cancel();});
 try{
  await assert.rejects(f.rt.stage('validator','validator',{}));
  assert.equal(f.calls.length,1);assert.equal(f.hostCalls,0);
 }finally{await f.close();}
});

test('format repair cannot extend the shared run turn limit',async()=>{
 const f=await fixture(()=>({text:'Now let me continue.'}));
 f.rt.budget.limits.turns=1;
 try{
  await assert.rejects(f.rt.stage('correctness','correctness',{}),/turn budget/);
  assert.equal(f.calls.length,1);assert.equal(f.rt.budget.turns,1);
  assert.equal(f.rt.budget.usedTokens,20);assert.equal(f.rt.budget.reservedTokens,0);
 }finally{await f.close();}
});

test('model transport failure is not retried as a formatting mistake',async()=>{
 const f=await fixture(()=>({httpError:true}));
 try{
  await assert.rejects(f.rt.stage('validator','validator',{}),/not a format error/);
  assert.equal(f.calls.length,1);assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,0);
 }finally{await f.close();}
});

test('AIedu-shaped correctness first-response failure retains sanitized HTTP diagnosis',async()=>{
 const f=await fixture(()=>({httpError:true,errorMessage:'Invalid messages sequence; api_key=local-fake; payload={"source":"private"}',errorCode:'invalid_messages',requestId:'req-correctness-1'}));
 try{
  await assert.rejects(f.rt.stage('correctness','correctness',{}),/provider_4xx/);
  const diagnostic=f.events.find(e=>e.event==='stage_failed')?.diagnostic;
  assert.equal(diagnostic.http_status,400);assert.equal(diagnostic.request_id,'req-correctness-1');assert.equal(diagnostic.provider_code,'invalid_messages');
  assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,0);assert.ok(!JSON.stringify(f.events).includes('local-fake'));assert.ok(!JSON.stringify(f.events).includes('private'));
 }finally{await f.close();}
});

test('AIedu-shaped Validator failure after successful tools retains the failing HTTP response, not prior 200',async()=>{
 const f=await fixture((_r,n)=>n===1?{tool:true}:{httpError:true,errorMessage:'Invalid tool result sequence',errorCode:'tool_sequence',requestId:'req-validator-2'});
 try{
  await assert.rejects(f.rt.stage('validator','validator',{}),/provider_4xx/);
  assert.equal(f.hostCalls,1);
  const responses=f.events.filter(e=>e.event==='model_http_response');assert.deepEqual(responses.map(e=>e.http_status),[200,400]);
  const diagnostic=f.events.find(e=>e.event==='stage_failed')?.diagnostic;assert.equal(diagnostic.request_id,'req-validator-2');assert.equal(diagnostic.http_status,400);
  assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,0);
 }finally{await f.close();}
});

for(const [role,limit] of [['correctness',10],['security',10],['validator',12]] as const){
 test(role+' admits an oversized tool batch without aborting and closes with evidence intact',async()=>{
  const f=await fixture((_r,n,request)=>{
   if(n===1)return {tool:true,toolCount:limit+3};
   assert.equal(request.tool_choice,'none');
   const results=request.messages.filter((m:any)=>m.role==='tool');
   assert.equal(results.length,limit+3);
   assert.equal(results.filter((m:any)=>m.content.includes('Tool not executed')).length,3);
   assert.ok(results.some((m:any)=>m.content.includes('x=1')));
   return {text:'{"findings":[],"limitations":[]}'};
  });
  try{
   const result=await f.rt.stage(role,role,{});
   assert.equal(f.rt.budget.tools,limit);assert.equal(f.rt.usage.tool_calls,limit);
   assert.equal(f.events.filter(e=>e.event==='tool_budget_denied').length,3);
   assert.equal(f.events.filter(e=>e.event==='stage_failed').length,0);
   assert.equal(f.rt.budget.controller.signal.aborted,false);
   assert.equal(result.incomplete_dimensions[0].skipped_tools.length,3);
   assert.equal(f.calls.length,2);assert.equal(f.events.filter(e=>e.event==='stage_output_retry').length,0);
  }finally{await f.close();}
 });
}

test('shared parent tool cap rejects without partial child debit and permits no-tools closing',async()=>{
 const f=await fixture((_r,n)=>n===1?{tool:true,toolCount:5}:{text:'{"findings":[]}'});
 f.rt.budget.limits.tools=2;
 try{
  const result=await f.rt.stage('correctness','correctness',{});
  assert.equal(f.rt.budget.tools,2);assert.equal(f.calls[1].request.tool_choice,'none');
  assert.equal(result.incomplete_dimensions[0].skipped_tools.length,3);
  assert.equal(f.events.find(e=>e.event==='stage_complete')?.local_budget.tools,2);
  assert.equal(f.rt.budget.controller.signal.aborted,false);
 }finally{await f.close();}
});

test('exact tool boundary allows final JSON and format repair without reopening tools',async()=>{
 const f=await fixture((_r,n)=>n===1?{tool:true,toolCount:10}:n===2?{text:'Not JSON'}:{text:'{"findings":[],"limitations":["Source evidence still incomplete"]}'});
 try{
  const result=await f.rt.stage('correctness','correctness',{});
  assert.equal(f.rt.budget.tools,10);assert.equal(f.calls.length,3);
  // Pi retains historical tool schemas for transcript compatibility; none is
  // forced and Core admission/host execution remain disabled.
  for(const c of f.calls.slice(1))assert.equal(c.request.tool_choice,'none');
  assert.equal(f.events.filter(e=>e.event==='stage_failed').length,0);
  assert.ok(result.incomplete_dimensions.length);
 }finally{await f.close();}
});

test('oversized batch recovery still honors cancellation before finalization',async()=>{
 const f=await fixture(()=>({tool:true,toolCount:12}),(e,rt)=>{if(e.event==='tool_budget_denied')rt.cancel();});
 try{await assert.rejects(f.rt.stage('correctness','correctness',{}));assert.equal(f.calls.length,1);assert.ok(f.rt.budget.tools<=10);}
 finally{await f.close();}
});

test('six-stage budget regression produces partial coverage instead of discarding the run',async()=>{
 const f=await fixture((role,n)=>role==='mapper'?{text:'{"mappings":[]}'}:role==='synthesizer'?{text:'{"findings":[]}'}:n===1?{tool:true,toolCount:15}:{text:'{"findings":[]}'});
 try{
  const result=await f.rt.run();assert.equal(result.completion_status,'partial');
  assert.equal(result.coverage.coverage_statuses.filter((s:any)=>s.status==='incomplete').length,4);
  assert.equal(f.events.filter(e=>e.event==='stage_failed').length,0);
  assert.equal(f.events.filter(e=>e.event==='stage_complete').length,6);
  assert.ok(f.rt.budget.tools<=100);assert.equal(f.rt.budget.reservedTokens,0);
 }finally{await f.close();}
});

test('strict parser accepts a fenced object but never scrapes a JSON fragment out of prose',()=>{
 assert.deepEqual(object('  ```json\n{"findings":[]}\n```  '),{findings:[]});
 for(const text of ['Now let me review. {"findings":[]}','[]','null','{"findings":[]} trailing prose'])assert.throws(()=>object(text));
});

test('six-stage regression separates repaired/non-defects, keeps independent test gaps and readable coverage',async()=>{
 const common={path:'a.py',line:1,severity:'medium',evidence_refs:['code://snap/head/a.py#L1'],related_tests:[]};
 const fixed={...common,finding_id:'CORR-001',side:'base',title:'load 首次加载覆盖草稿',disposition:'fixed_in_head',recommendation:'head 版本修复方案正确'};
 const informational={...common,finding_id:'CORR-002',title:'total_score 与 base 一致，确认无回归',disposition:'no_regression'};
 const gap={...common,finding_id:'TEST-001',side:'head',title:'Missing first-load preservation test',disposition:'actionable',
  change_causality:{status:'test_gap',change_unit_ids:['u'],head_evidence_refs:['code://snap/head/a.py#L1'],base_evidence_refs:[],reasoning:'The repaired initialization has no assertion for saved answers.'},
  test_gap:{expected_behavior:'retain saved draft',missing_scenario:'reload with empty editor',assertion_source:'code://snap/head/a.py#L1',why_insufficient:'Only API save is tested',existing_test_evidence_refs:['code://snap/head/tests/test_api.py#L1']}};
 const dimension={dimension:'authorization',reason:'Backend handler not loaded',evidence_refs:[{path:'a.py',line:1}]};
 const f=await fixture((role,_n,request)=>{
  if(role==='mapper')return {text:'{"mappings":[]}'};
  if(role==='correctness')return {text:JSON.stringify({findings:[fixed,informational]})};
  if(role==='security')return {text:JSON.stringify({findings:[],incomplete_dimensions:[dimension]})};
  if(role==='contract_test')return {text:JSON.stringify({findings:[gap]})};
  if(role==='synthesizer'){
   const user=request.messages.find((m:any)=>m.role==='user').content;
   const payload=JSON.parse(typeof user==='string'?user:user.map((x:any)=>x.text||'').join(''));
   assert.deepEqual(payload.findings.map((x:any)=>x.finding_id),['TEST-001']);
  }
  return {text:JSON.stringify({findings:[gap]})};
 });
 try{
  const result=await f.rt.run();assert.deepEqual(result.findings.map((x:any)=>x.finding_id),['TEST-001']);
  assert.deepEqual(result.filtered_findings.map((x:any)=>x.reason),['fixed_in_head','no_regression']);
  assert.equal(result.completion_status,'partial');assert.ok(!JSON.stringify(result.coverage).includes('[object Object]'));
  assert.ok(result.coverage.incomplete_dimensions[0].includes('authorization: Backend handler not loaded'));
  assert.deepEqual(result.coverage.incomplete_details[0].value,dimension);
 }finally{await f.close();}
});

for(const readBase of [false,true])test('Validator coverage resolution '+(readBase?'clears a limitation after actual base read':'cannot clear a limitation using an unread reference'),async()=>{
 const dimension={dimension:'authorization',reason:'base implementation not loaded'};
 const f=await fixture((role,n,request)=>{
  if(role==='mapper')return {text:'{"mappings":[]}'};
  if(role==='security')return {text:JSON.stringify({findings:[],incomplete_dimensions:[dimension]})};
  if(role==='validator'){
   if(readBase&&n===1)return {tool:true,args:{path:'a.py',side:'base',start_line:1,end_line:1}};
   const value=request.messages.find((m:any)=>m.role==='user').content;
   const payload=JSON.parse(typeof value==='string'?value:value.map((x:any)=>x.text||'').join(''));
   const coverage_id=payload.coverage.coverage_statuses.find((s:any)=>s.dimension==='authorization').coverage_id;
   return {text:JSON.stringify({findings:[],coverage_resolutions:[{coverage_id,status:'covered',reason:'Inspected base implementation',evidence_refs:['code://snap/base/a.py#L1']}]})};
  }
  return {text:'{"findings":[]}'};
 });
 try{const result=await f.rt.run();assert.equal(result.coverage.incomplete_dimensions.length,readBase?0:1);assert.equal(result.completion_status,readBase?'completed':'partial');}finally{await f.close();}
});

test('Validator may demote an actionable candidate to a no-regression observation',async()=>{
 const finding={finding_id:'F-1',path:'a.py',line:1,title:'Candidate branch regression',severity:'medium',evidence_refs:['code://snap/head/a.py#L1'],related_tests:[],disposition:'actionable',
  change_causality:{status:'introduced',change_unit_ids:['u'],head_evidence_refs:['code://snap/head/a.py#L1'],base_evidence_refs:['code://snap/base/a.py#L1'],reasoning:'Proposed changed behavior'}};
 const f=await fixture(role=>({text:role==='mapper'?'{"mappings":[]}':JSON.stringify({findings:role==='correctness'||role==='synthesizer'?[finding]:role==='validator'?[{...finding,disposition:'no_regression',title:'Confirmed no regression; no fix required'}]:[]})}));
 try{
  const result=await f.rt.run();assert.deepEqual(result.findings,[]);
  assert.equal(result.filtered_findings.length,1);assert.equal(result.filtered_findings[0].source_stage,'validator');
  assert.equal(result.filtered_findings[0].reason,'no_regression');
 }finally{await f.close();}
});
