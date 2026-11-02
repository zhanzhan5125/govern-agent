import test from 'node:test';
import assert from 'node:assert/strict';
import {changedStages,candidateActions,versionGroups,versionControls} from '../src/policy-versions.js';
test('only changed stages are named and unpublished candidates cannot restore',()=>{
 const c={id:'c1',status:'STATIC_PASSED',content:{files:{'stages/security.md':'new'}},baseline:{content:{files:{'stages/security.md':'old'}}}};
 assert.deepEqual(changedStages(c),['security']);
 assert.deepEqual(candidateActions(c,{}),['evaluate','reject']);
 assert.deepEqual(candidateActions({...c,status:'ROLLED_BACK'},{}),[]);
 assert.deepEqual(candidateActions({...c,status:'ROLLED_BACK',reports:{release:{generation:1}}},{}),['rollback']);
 assert.deepEqual(candidateActions({...c,status:'ACTIVE',reports:{release:{}}},{candidate_id:'c1'}),[]);
});
test('batches group candidates without inventing historical version numbers',()=>{
 const data={batches:[{id:'b1',version:1},{id:'b2',version:2,status:'FAILED'}],candidates:[{id:'c1',lineage:{batch_id:'b1'}},{id:'c2',lineage:{batch_id:'b1'}},{id:'old',lineage:{}}]};
 const groups=versionGroups(data);
 assert.equal(groups[0].candidates.length,2);assert.equal(groups[1].candidates.length,0);
 assert.equal(groups[2].version,null);assert.equal(groups[2].candidates[0].id,'old');
});
test('current version buttons are grey; pending version publishes and only a previous release restores',()=>{
 assert.deepEqual(versionControls({id:'a',status:'ACTIVE',reports:{release:{}}},{candidate_id:'a'}),{current:true,publish:false,restore:false});
 assert.deepEqual(versionControls({id:'b',status:'HOLDOUT_PASSED'},{candidate_id:'a'}),{current:false,publish:true,restore:false});
 assert.deepEqual(versionControls({id:'b',status:'STATIC_PASSED'},{candidate_id:'a'}),{current:false,publish:false,restore:false});
 assert.deepEqual(versionControls({id:'b',status:'ROLLED_BACK',reports:{release:{}}},{candidate_id:'a'}),{current:false,publish:false,restore:true});
});
