import test from 'node:test';
import assert from 'node:assert/strict';
import {buildReviewSlices,mapperBatches} from '../src/review-slices.js';
import {SharedBudget} from '@earendil-works/pi-agent-core';
import {ScopedBudget} from '../src/scoped-budget.js';

const units=[
 {change_unit_id:'test',path:'AIedu 后端/tests/test_api.py',side:'head',symbol:{name:'test_saved_assignment_answers',kind:'FunctionDef',start:1,end:4},context_start:1,context_end:4,context:'def test_saved_assignment_answers():\n assert saved_answer == loaded_answer'},
 ...['base','head'].map(side=>({change_unit_id:side,path:'AIedu 前端/src/StudentAnswer.vue',side,symbol:{name:'load',kind:'method_definition',start:3,end:8},context_start:2,context_end:9,context:'methods: { load() { this.content = assignment.answer } }'})),
 {change_unit_id:'nested',path:'AIedu 前端/src/StudentAnswer.vue',side:'head',symbol:{name:'const firstQuestion',kind:'lexical_declaration',start:4,end:4},context_start:3,context_end:6,context:'firstQuestion = assignment.answer'}
];

test('AIedu-shaped changes pair both versions and attach changed regression test',()=>{
 const slices=buildReviewSlices(units,[{change_unit_id:'head',requirement_id:'KAN-5',criterion_id:'AC-S01',status:'partial'}],'snap');
 assert.equal(slices.length,1);assert.equal(slices[0].unit_ids.length,4);
 assert.deepEqual(new Set(slices[0].evidence_pack.map((e:any)=>e.side)),new Set(['base','head']));
 assert.equal(slices[0].evidence_pack.length,3);assert.equal(slices[0].mappings.length,1);
 assert.ok(slices[0].grouping_basis.includes('changed-test-lexical-behavior-overlap'));
});
test('bounded slices cover every unit and preserve cross-slice relationships',()=>{
 const slices=buildReviewSlices(units,[],'snap',50,1);
 assert.deepEqual(new Set(slices.flatMap(s=>s.unit_ids)),new Set(units.map(u=>u.change_unit_id)));
 assert.ok(slices.length>1);assert.ok(slices.some(s=>s.related_slice_ids.length));
 assert.ok(mapperBatches(units,100).length>1);
});

test('generic source syntax alone does not link unrelated tests',()=>{
 const make=(path:string,name:string)=>({change_unit_id:path,path,side:'head',symbol:{name},context:'const content = this.value; return content;',context_start:1,context_end:1});
 const slices=buildReviewSlices([make('src/payment.js','charge'),make('tests/test_calendar.py','test_calendar')],[],'snap');
 assert.equal(slices.length,2);
});

test('changed caller and callee share a slice only with a reference inside the changed symbol',()=>{
 const callee={change_unit_id:'callee',path:'src/store.py',side:'head',symbol:{name:'save_answer',start:1,end:4},context:'def save_answer(): pass',context_start:1,context_end:1,
  reference_candidates:[{path:'src/api.py',line:12,precision:'lexical'}]};
 const caller={change_unit_id:'caller',path:'src/api.py',side:'head',symbol:{name:'submit',start:10,end:15},context:'save_answer()',context_start:12,context_end:12};
 const slices=buildReviewSlices([callee,caller],[],'snap');
 assert.equal(slices.length,1);assert.ok(slices[0].grouping_basis.includes('changed-symbol-reference-candidate'));
 assert.equal(buildReviewSlices([{...callee,reference_candidates:[{path:'src/api.py',line:99}]},caller],[],'snap').length,2);
});
test('temporary parallel reservation waits without cancelling the global run',async()=>{
 const b=new SharedBudget({tokens:100,turns:5,tools:5,milliseconds:2000});
 const a=await b.reserveAsync([],60);let admitted=false;
 const waiting=b.reserveAsync([],60).then(r=>{admitted=true;return r;});
 await new Promise(r=>setTimeout(r,40));assert.equal(admitted,false);assert.equal(b.controller.signal.aborted,false);
 a.settle(10);const next=await waiting;next.settle(10);assert.equal(b.usedTokens,20);
});
test('local exhaustion leaves final-stage budget intact and all actual usage is charged',async()=>{
 const global=new SharedBudget({tokens:1000,turns:20,tools:20,milliseconds:2000});
 const local=new ScopedBudget(global,{tokens:100,turns:2,tools:2,milliseconds:2000});
 const a=await local.reserveAsync([],50);a.settle(20);
 await assert.rejects(()=>local.reserveAsync([],100),/token budget/);
 assert.equal(global.controller.signal.aborted,false);assert.equal(global.usedTokens,20);
 const final=await global.reserveAsync([],100);final.settle(30);assert.equal(global.usedTokens,50);local.dispose();
});
test('waiting admission observes cancellation',async()=>{
 const b=new SharedBudget({tokens:100,turns:5,tools:5,milliseconds:2000});const a=await b.reserveAsync([],60);
 const controller=new AbortController();const pending=b.reserveAsync([],60,controller.signal);controller.abort();
 await assert.rejects(()=>pending,/cancelled/);a.settle(0);assert.equal(b.reservedTokens,0);
});
