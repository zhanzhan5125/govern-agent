<script setup>
import {ref,watch} from 'vue';
import {api} from './api.js';
const props=defineProps({policy:{type:Object,required:true}}),emit=defineEmits(['saved']);
const editing=ref(''),draft=ref(''),busy=ref(false),error=ref('');
const names={mapper:'需求变更映射',correctness:'正确性审查',security:'安全审查',contract_test:'契约与测试',synthesizer:'结果综合',validator:'独立核验'};
watch(()=>props.policy.source_sha256,()=>{editing.value='';error.value='';});
function edit(section){editing.value=section;draft.value=section==='common'?props.policy.common_rules:props.policy.risk_policy;error.value='';}
async function save(){
  if(!confirm('这是人工策略修改，将影响所有租户之后的新审查，并使旧候选需要重新生成和评测；已冻结任务不变。确认保存？'))return;
  busy.value=true;error.value='';
  try{await api('/v1/govern/policy/manual',{section:editing.value,content:draft.value,expected_source_sha256:props.policy.source_sha256});editing.value='';emit('saved');}
  catch(e){error.value=e.message;}finally{busy.value=false;}
}
</script>
<template>
  <section class="card"><h2>公共规则与风险规则</h2><p class="muted">不参与自进化。仅本机管理员可人工修改；每次审查冻结当时版本。修改后旧候选须重新生成和评测。</p>
    <div v-for="section in ['common','risk']" :key="section" class="policy-fragment">
      <div class="section-heading"><h3>{{section==='common'?'公共证据规则':'风险等级规则'}}</h3><button class="button secondary" :disabled="busy" @click="edit(section)">人工编辑{{section==='common'?'公共规则':'风险规则'}}</button></div>
      <small>policies/govern-review/common/{{section==='common'?'evidence':'risk'}}.md · 人工维护 · 不可自动进化</small>
      <template v-if="editing===section"><label :for="'rule-'+section">{{section==='common'?'公共规则内容':'风险规则内容'}}</label><textarea :id="'rule-'+section" v-model="draft" rows="15" :disabled="busy"></textarea><p v-if="error" role="alert">{{error}}</p><div class="row"><button class="button primary" :disabled="busy||!draft.trim()" @click="save">保存人工修改</button><button class="button secondary" :disabled="busy" @click="editing=''">取消编辑</button></div></template>
      <pre v-else>{{section==='common'?policy.common_rules:policy.risk_policy}}</pre>
    </div>
  </section>
  <section class="card"><h2>六阶段策略</h2><p class="muted">自进化只更新被归因阶段的补充指导，一次候选只能改变一个阶段；其他阶段、公共规则、风险和输出契约不变。</p>
    <details v-for="(text,role) in policy.stage_sources" :key="role" class="policy-fragment"><summary><b>{{names[role]}}</b> <code>{{role}}</code></summary>
      <small>policies/govern-review/stages/{{role}}.md · 阶段基础文件（人工维护）</small><pre>{{text}}</pre>
      <h3>活动进化补充指导</h3><pre>{{policy.stage_overrides[role]||'暂无补充指导，使用阶段基础策略。'}}</pre>
      <details><summary>最终有效指令（专项片段按 Slice 另行加载）</summary><pre>{{policy.stages[role]}}</pre></details>
    </details>
  </section>
  <section class="card"><h2>专项片段</h2><p class="muted">按路径与代码词法信号增加检查，不替代基础审查，不参与自动进化。选择结果与命中原因记录在 Trace。</p>
    <details v-for="(fragment,name) in policy.specialist_fragments" :key="name" class="policy-fragment"><summary><b>{{name}}</b> <small>{{fragment.roles.join(' / ')}}</small></summary><small>policies/govern-review/{{fragment.file}}</small><pre>{{fragment.content}}</pre><pre>{{JSON.stringify({path_patterns:fragment.path_patterns,code_patterns:fragment.code_patterns},null,2)}}</pre></details>
  </section>
  <section class="card"><details><summary>输出契约（非进化内容）</summary><pre>{{policy.output_contract}}</pre></details><p class="muted">旧版 Lead/Worker Skill 已归档到 policies/legacy-skills，仅兼容流程使用，不会自动注入 Pi 六阶段。</p></section>
</template>
