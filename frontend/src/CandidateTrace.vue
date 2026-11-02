<script setup>
import {roleNames,changedStages} from './policy-versions.js';
const props=defineProps({candidate:Object});
const phases=['VALIDATION_PASSED','HOLDOUT_PASSED','REJECTED'];
function diffFor(stage){return (props.candidate.lineage?.change_diff||'').split(/(?=--- baseline\/)/).filter(s=>s.startsWith('--- baseline/stages/'+stage+'.md')).join('')||'无差异片段';}
</script>
<template>
 <section v-for="stage in changedStages(candidate)" :key="stage" class="card">
  <h3>{{roleNames[stage]}} Skill</h3><h4>反馈原因</h4>
  <template v-for="f in candidate.lineage?.feedback_context||[]" :key="f.feedback_id"><p v-if="f.source_stage===stage">{{f.finding?.business_priority}} {{f.reason}}<small v-if="f.finding?.path"> · {{f.finding.path}}:{{f.finding.line}}</small></p></template>
  <p v-if="candidate.lineage?.stage_rationales?.[stage]" class="muted">{{candidate.lineage.stage_rationales[stage]}}</p>
  <h4>修改部分</h4><pre>{{diffFor(stage)}}</pre>
 </section>
 <h4>静态验证</h4><p>{{candidate.reports?.STATIC_PASSED?.passed?'通过 · 策略文件结构、修改范围及内容完整性已检查':'尚未通过'}}</p>
 <template v-for="p in phases" :key="p"><section v-if="candidate.reports?.[p]?.baseline"><h4>{{p==='VALIDATION_PASSED'?'Validation':p==='HOLDOUT_PASSED'?'Holdout':p==='SHADOW'?'Shadow':'未通过评测'}} · {{candidate.reports[p].passed?'通过':'未通过'}}</h4><table><thead><tr><th>指标</th><th>修改前</th><th>修改后</th></tr></thead><tbody><tr v-for="m in ['recall','precision','p01_recall','evidence_accuracy','total_tokens']" :key="m"><td>{{m}}</td><td>{{candidate.reports[p].baseline[m]}}</td><td>{{candidate.reports[p].candidate[m]}}</td></tr></tbody></table></section></template>
</template>
