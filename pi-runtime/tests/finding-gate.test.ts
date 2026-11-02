import test from 'node:test';
import assert from 'node:assert/strict';
import {coverageText,nonDefectReason,screenFindings} from '../src/finding-gate.js';
import {normalizeFindings} from '../src/runtime.js';

const causal={status:'introduced',change_unit_ids:['u'],head_evidence_refs:['code://snap/head/a.py#L2'],base_evidence_refs:['code://snap/base/a.py#L2'],reasoning:'Changed branch still violates its contract in head.'};
const finding={finding_id:'f',path:'a.py',line:2,side:'head',title:'New branch regression',severity:'high',related_tests:[],evidence_refs:['code://snap/head/a.py#L2'],disposition:'actionable',change_causality:causal};

test('AIedu fixed base bug and no-regression confirmation are excluded with an audit record',()=>{
 const audit:any[]=[];
 const fixed={...finding,finding_id:'CORR-001',side:'base',title:'load 首次加载覆盖草稿',recommendation:'head 版本修复方案正确：直接初始化答案。'};
 const noRegression={...finding,finding_id:'CORR-002',title:'total_score 与 base 一致，确认无回归'};
 const accepted=screenFindings([fixed,noRegression,finding],'validator',audit);
 assert.deepEqual(accepted,[finding]);assert.deepEqual(audit.map(x=>x.reason),['fixed_in_head','no_regression']);
 assert.equal(nonDefectReason({...finding,disposition:'pre_existing'}),'pre_existing');
});

test('actionable schema requires causal structure, not just valid JSON',()=>{
 assert.equal(normalizeFindings({findings:[finding]},'correctness','slice').length,1);
 assert.throws(()=>normalizeFindings({findings:[{...finding,change_causality:undefined}]},'correctness','slice'),/change_causality/);
 assert.throws(()=>normalizeFindings({findings:[{...finding,disposition:undefined}]},'correctness','slice'),/disposition/);
});

test('suggested regression test is not automatically a test-gap finding',()=>{
 const gap={...finding,change_causality:{...causal,status:'test_gap'},recommendation:'head 版本修复方案正确，建议增加回归测试'};
 const audit:any[]=[];assert.deepEqual(screenFindings([gap],'contract_test',audit),[]);
 assert.equal(audit[0].reason,'unsupported_test_gap');
 const proven={...gap,test_gap:{expected_behavior:'retain draft',missing_scenario:'reload saved first answer',assertion_source:'KAN-5:AC-1',why_insufficient:'Only API save is tested',existing_test_evidence_refs:['code://snap/head/tests/test_api.py#L1']}};
 assert.deepEqual(screenFindings([proven],'contract_test',[]),[proven]);
});

test('object coverage preserves dimension, reason and evidence instead of object coercion',()=>{
 const text=coverageText({dimension:'authorization',reason:'Handler not read',evidence_refs:[{path:'a.py',line:2}]});
 assert.match(text,/authorization: Handler not read/);assert.match(text,/a.py/);assert.ok(!text.includes('[object Object]'));
 assert.match(coverageText({other:{nested:'detail'}}),/detail/);
});
