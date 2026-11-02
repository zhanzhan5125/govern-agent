import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileTemplate} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';

test('strategy versions render only in evaluation, not in the policy center',()=>{
 const source=readFileSync(new URL('../src/App.vue',import.meta.url),'utf8');
 const {descriptor}=parse(source);const tree=baseParse(descriptor.template.content);
 const components=[];
 function visit(node){if(node.tag==='EvolutionVersions')components.push(node);for(const child of node.children||[])visit(child);}
 visit(tree);assert.equal(components.length,1);
 assert.ok(components[0].props.some(p=>p.type===7&&p.name==='if'&&p.exp.content==="page==='evaluation'"));
 assert.ok(source.includes('policy.active_release'));
 assert.equal(compileTemplate({source:descriptor.template.content,filename:'App.vue',id:'policy-test'}).errors.length,0);
});

test('displayed Skill uses active effective content and clears stale edit on version change',()=>{
 const source=readFileSync(new URL('../src/PolicyCenter.vue',import.meta.url),'utf8');
 assert.ok(source.includes('in policy.effective_skills'));
 assert.ok(source.includes('props.policy.sha256,props.policy.active_release?.generation'));
 assert.ok(!source.includes('EvolutionVersions'));
});
