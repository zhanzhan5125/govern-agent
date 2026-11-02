import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {GovernRuntime} from '../src/runtime.js';
import {prepareContext,contextPolicy} from '../src/context-policy.js';

test('Pi requests host compression without granting the model a compression tool',async()=>{
 let calls=0;const events:any[]=[];
 const server=createServer(async(req,res)=>{
  let raw='';for await(const part of req)raw+=part;
  const body=JSON.parse(raw);
  assert.ok(!(body.tools||[]).some((t:any)=>t.function?.name==='compact_context'));
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const base={id:'offline',object:'chat.completion.chunk',created:1,model:'test'};
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{role:'assistant',content:'{"mappings":[]}'},finish_reason:null}]})+'\n\n');
  res.end('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:10,total_tokens:20}})+'\n\ndata: [DONE]\n\n');
 });
 await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));
 const runtime=new GovernRuntime({method:'run_review',context_backend:'python',input:{},
  model:{base_url:`http://127.0.0.1:${(server.address() as any).port}/v1`,model:'test',api_key:'local-test-only'},
  policy:{sha256:'offline',stages:{mapper:'ROLE:mapper. Return mappings JSON.'},tool_profiles:{mapper:[]}}},
  {event:e=>events.push(e),tool:async(role,name,args)=>{
   assert.equal(role,'mapper');assert.equal(name,'compact_context');calls++;
   return prepareContext(args.messages,contextPolicy(),args.overhead);
  }});
 try{assert.deepEqual(await runtime.stage('mapper','mapper',{units:[],requirements:[]}),{mappings:[]});assert.ok(calls>0);}
 finally{runtime.cancel();await new Promise<void>(r=>server.close(()=>r()));}
});
