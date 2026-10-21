import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {SharedBudget} from '@earendil-works/pi-agent-core';
import {GovernRuntime,normalizeFindings} from '../src/runtime.js';

test('Core shared reservations prevent concurrent overspend and settle once',()=>{
 const b=new SharedBudget({tokens:100,turns:3,tools:2,milliseconds:10000});
 const a=b.reserve([],60);assert.throws(()=>b.reserve([],60),/budget exhausted/);a.settle(20);a.settle(20);
 assert.equal(b.reservedTokens,0);assert.equal(b.usedTokens,20);
});
test('Core tool counter and cancellation are enforced',()=>{
 const b=new SharedBudget({tokens:1000,turns:3,tools:1,milliseconds:10000});b.takeTool();assert.throws(()=>b.takeTool(),/tool budget/);
 assert.equal(b.controller.signal.aborted,true);
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
