import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileTemplate} from '@vue/compiler-sfc';

const files=['App.vue','AutoEvolution.vue','PolicyCenter.vue'];
const sources=Object.fromEntries(files.map(file=>[file,readFileSync(new URL('../src/'+file,import.meta.url),'utf8')]));

test('user-facing templates omit legacy links and implementation-heavy instructions',()=>{
 const templates=Object.values(sources).map(source=>parse(source).descriptor.template.content).join('\n');
 for(const text of ['打开旧版兼容控制台','真实模型自动调用已锁定','GOVERN_AUTO_EVOLUTION_MODEL_CALLS','当前状态：','每个阶段展示实际生效的 Skill','公共规则和风险规则由系统另行注入','不模拟百分比','没有预置结果','旧版 Lead/Worker'])assert.ok(!templates.includes(text),text);
 for(const [filename,source] of Object.entries(sources))assert.deepEqual(compileTemplate({source:parse(source).descriptor.template.content,filename,id:'copy-test'}).errors,[]);
});

test('cleanup retains consent, errors and a concise unavailable automation state',()=>{
 assert.ok(sources['App.vue'].includes('v-model="consent"'));
 assert.ok(sources['App.vue'].includes('if(!consent.value)return'));
 assert.ok(sources['App.vue'].includes('可能产生费用'));
 assert.ok(sources['AutoEvolution.vue'].includes('自动生成暂不可用'));
 assert.ok(sources['AutoEvolution.vue'].includes('v-if="error"'));
 assert.ok(sources['AutoEvolution.vue'].includes('status?.config?.enabled'));
});
