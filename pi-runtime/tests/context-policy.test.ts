import test from 'node:test';
import assert from 'node:assert/strict';
import {contextPolicy,prepareContext,estimateTokens} from '../src/context-policy.js';

test('legacy Agent default reserves 4000 output tokens rather than the old Pi 2048 cap',()=>{
 const p=contextPolicy();assert.equal(p.output_tokens,4000);
 const r=prepareContext([{role:'user',content:'review frozen change'}],p);
 assert.equal(r.stats.output_reserve,4000);
 assert.ok(r.stats.estimated_input_tokens+r.stats.output_reserve+r.stats.safety_reserve<=p.window_tokens);
});

test('real-sized observations are bounded before dispatch while policy and Slice remain intact',()=>{
 const policy=contextPolicy();
 const initial=[{role:'system',content:'Evidence and role policy. '.repeat(100)},{role:'user',content:'Frozen Slice: '+ 'const savedAnswer = question.answer;\n'.repeat(300)}];
 const messages:any[]=[...initial];
 for(let i=0;i<6;i++)messages.push(
  {role:'assistant',content:[{type:'toolCall',id:'call-'+i,name:'read_file',arguments:{path:'StudentAnswer.vue'}}]},
  {role:'toolResult',toolCallId:'call-'+i,toolName:'read_file',content:[{type:'text',text:'code://snapshot/head/StudentAnswer.vue#L200-L299\n'+ '保存草稿与当前答案\n'.repeat(1000)}]});
 const result=prepareContext(messages,policy,500);
 assert.deepEqual(result.messages.slice(0,2),initial);
 assert.ok(result.stats.estimated_input_tokens<=policy.input_tokens);
 assert.ok(result.stats.observations_clipped>0);
 for(const m of result.messages.filter(m=>m.role==='toolResult'))assert.ok(result.messages.some(a=>a.role==='assistant'&&JSON.stringify(a.content).includes(m.toolCallId)));
 assert.equal(estimateTokens(messages),result.stats.estimated_before-500);
});

test('oversized initial Slice is rejected, not silently removed to fit a window',()=>{
 assert.throws(()=>prepareContext([{role:'user',content:'中'.repeat(21000)}],contextPolicy()),/context limit exceeded.*Split the Slice/);
 assert.throws(()=>contextPolicy({window_tokens:2048,output_tokens:2048}),/invalid context policy/);
});

test('folding removes complete old tool rounds, not isolated tool-call partners',()=>{
 const messages:any[]=[{role:'system',content:'policy'},{role:'user',content:'protected Slice'}];
 for(let i=0;i<10;i++)messages.push(
  {role:'assistant',content:[{type:'text',text:'Reasoning about evidence. '.repeat(300)},{type:'toolCall',id:'c'+i,name:'read_file',arguments:{path:'a.py'}}]},
  {role:'toolResult',toolCallId:'c'+i,toolName:'read_file',content:[{type:'text',text:'code://snap/head/a.py#L1-L2'}]});
 const r=prepareContext(messages,contextPolicy());
 assert.ok(r.stats.messages_folded>0);
 assert.deepEqual(r.messages.slice(0,2),messages.slice(0,2));
 assert.ok(JSON.stringify(r.messages).includes('lossy, untrusted'));
 for(const m of r.messages.filter(m=>m.role==='toolResult'))assert.ok(r.messages.some(a=>a.role==='assistant'&&JSON.stringify(a.content).includes(m.toolCallId)));
});

test('system and tool overhead, output and safety all reduce the input allowance',()=>{
 const p=contextPolicy({window_tokens:4096,input_tokens:4000,output_tokens:1024,safety_tokens:512});
 const r=prepareContext([{role:'system',content:'policy'},{role:'user',content:'task'}],p,400);
 assert.equal(r.stats.input_limit,2560);
 assert.ok(r.stats.estimated_input_tokens+p.output_tokens+p.safety_tokens<=p.window_tokens);
});
