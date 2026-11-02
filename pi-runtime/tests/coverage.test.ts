import test from 'node:test';
import assert from 'node:assert/strict';
import {CoverageLedger,coverageStatus,canFollowup,rangeCovered} from '../src/coverage.js';
test('AIedu covered concurrency explanation is an observation, not incomplete coverage',()=>{
 const ledger=new CoverageLedger();
 ledger.add('correctness','slice-a',[{dimension:'concurrency',reason:'load() 为单次请求，未发现共享状态。声明为已覆盖。'}]);
 assert.equal(ledger.incomplete.length,0);assert.equal(ledger.observations[0].status,'reported_covered');
 assert.equal(coverageStatus({dimension:'auth',status:'not_applicable',reason:'No endpoint changes'}),'not_applicable');
 assert.equal(coverageStatus('Security surface unchanged from base but backend authorization not reviewed'),'incomplete');
});
test('truncated source is resolved only by contiguous frozen windows on the same side',()=>{
 const w=[{path:'a.vue',side:'base',start_line:1,end_line:100},{path:'a.vue',side:'base',start_line:101,end_line:200}];
 assert.equal(rangeCovered('a.vue','base',1,180,w),true);
 assert.equal(rangeCovered('a.vue','head',1,180,w),false);
 assert.equal(rangeCovered('a.vue','base',1,201,w),false);
});
test('inherited synthesizer limitations deduplicate and retain source ownership',()=>{
 const ledger=new CoverageLedger();ledger.add('security','slice-a',['security.authorization']);
 ledger.add('synthesizer','global',['slice-a:security:security.authorization','global:synthesizer:slice-a:security:security.authorization']);
 assert.deepEqual(ledger.incomplete,['slice-a:security:security.authorization']);
 ledger.add('contract_test','slice-a',['前端测试：快照中未找到 StudentAnswer.vue 对应测试']);
 ledger.add('synthesizer','global',['slice-a:contract_test:前端测试：快照中未找到StudentAnswer.vue对应测试']);
 assert.equal(ledger.incomplete.length,2);
 ledger.add('contract_test','slice-b',['前端组件测试套件：快照中未找到 StudentAnswer.vue 对应的前端测试文件（如 .spec.js 或 .test.js），无法评估现有前端测试覆盖']);
 ledger.add('synthesizer','global',['slice-b:contract_test:前端组件测试套件：快照中未找到StudentAnswer.vue对应的前端测试文件，无法评估现有前端测试覆盖']);
 assert.equal(ledger.incomplete.length,3);
});
test('coverage resolution requires a known entry and actual frozen evidence',()=>{
 const ledger=new CoverageLedger();ledger.add('contract_test','slice-a',[{dimension:'source',reason:'base method not loaded'}]);
 const coverage_id=ledger.statuses[0].coverage_id;
 ledger.resolve([{coverage_id,status:'covered',reason:'Read both sides',evidence_refs:['fake']}],()=>false);
 assert.equal(ledger.incomplete.length,1);
 ledger.resolve([{coverage_id,status:'covered',reason:'Read both sides',evidence_refs:['code://snap/base/a.py#L1']}],()=>true);
 assert.equal(ledger.incomplete.length,0);assert.equal(ledger.details.length,0);assert.equal(ledger.statuses[0].status,'covered');
});
test('AIedu English reviewer and Chinese synthesis describe one XSS limitation with all variants retained',()=>{
 const ledger=new CoverageLedger();
 ledger.add('security','slice-a',[{dimension:'injection',reason:'Template rendering XSS review not completed; formatContent not reviewed',evidence_refs:['code://snap/head/src/StudentAnswer.vue#L1-L99']}]);
 ledger.add('security','slice-a',['The complete template rendering of user-generated content and v-html could not be fully traced.']);
 ledger.add('synthesizer','global',[{dimension:'security:injection',reason:'模板渲染 XSS 向量审查未完成，formatContent 未审查',evidence_refs:['code://snap/head/src/StudentAnswer.vue#L1-L99']}]);
 assert.equal(ledger.incomplete.length,1);assert.equal(ledger.statuses[0].variants.length,2);assert.equal(ledger.statuses[0].source_stage,'security');
 const id=ledger.statuses[0].coverage_id;ledger.add('synthesizer','global',[{coverage_id:id}]);assert.equal(ledger.incomplete.length,1);
});
test('same dimension in different files, sides, slices or execution conditions does not merge',()=>{
 const ledger=new CoverageLedger();
 for(const [slice,path,side] of [['slice-a','a.vue','head'],['slice-a','b.vue','head'],['slice-a','a.vue','base'],['slice-b','a.vue','head']])ledger.add('security',slice,[{dimension:'authorization',reason:'Ownership not checked',evidence_refs:[`code://snap/${side}/${path}#L1`]}]);
 assert.equal(ledger.incomplete.length,4);
 ledger.add('contract_test','slice-a',[{dimension:'test_coverage',reason:'Tests not run',path:'a.vue'}]);
 ledger.add('contract_test','slice-a',[{dimension:'test_coverage',reason:'Tests failed',path:'a.vue'}]);
 assert.equal(ledger.incomplete.length,6);
});
test('failed Reviewer coverage stays unresolved but its synthesized translation is not a second failure',()=>{
 const ledger=new CoverageLedger();ledger.add('correctness','slice-a',['Error: Stage model response failed (correctness): error']);
 ledger.add('synthesizer','global',[{dimension:'correctness:baseline',reason:'正确性审查模型响应失败，维度未完成'}]);
 assert.equal(ledger.incomplete.length,1);assert.equal(ledger.statuses[0].status,'incomplete');
});
test('evidence followup is bounded and cannot reopen exhausted or format-only work',()=>{
 const missing={incomplete_dimensions:[{dimension:'source',reason:'changeProblem/check 完整源码未包含在切片中'}]};
 const state={attempts:0,formatAttempt:0,closed:false,turns:2,turnLimit:6,tools:2,toolLimit:10};
 assert.ok(canFollowup(missing,state));
 for(const patch of [{attempts:1},{closed:true},{turns:5},{tools:9},{formatAttempt:1}])assert.equal(canFollowup(missing,{...state,...patch}),false);
 assert.equal(canFollowup({findings:[],incomplete_dimensions:[]},state),false);
});
