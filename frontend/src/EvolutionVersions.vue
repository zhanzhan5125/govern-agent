<script setup>
import {computed} from 'vue';
import CandidateTrace from './CandidateTrace.vue';
import {roleNames,statusNames,changedStages,versionGroups,versionControls} from './policy-versions.js';
const props=defineProps({data:Object,generating:Boolean,acting:Boolean});
defineEmits(['generate','refresh','action','restore-default']);
const groups=computed(()=>versionGroups(props.data).filter(g=>!g.baseline));
const current=c=>versionControls(c,props.data?.release).current;
const canPublish=c=>versionControls(c,props.data?.release).publish;
const canRestore=c=>versionControls(c,props.data?.release).restore;
const name=g=>'版本_'+new Date(g.created_at).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai'}).replaceAll('-','').replaceAll(':','').replace(' ','_');
</script>
<template>
 <section class="card">
  <div class="section-heading"><h2>策略版本</h2><div class="row"><button class="button secondary" @click="$emit('refresh')">刷新</button><button class="button primary" :disabled="generating||acting" @click="$emit('generate')">{{generating?'正在生成…':'从反馈生成版本'}}</button><button class="button secondary" :disabled="acting||generating||data?.default_release?.is_active" @click="$emit('restore-default')">恢复默认版本</button></div></div>
  <p v-if="data?.default_release?.is_active" class="muted">当前生效：默认版本</p>
  <p class="muted">一次生成一个版本，可同时更新多个 Skill。发布与回滚均作用于该版本的完整策略包。</p>
  <div v-if="!groups.length" class="empty small"><p>暂无策略版本</p></div>
  <details v-for="g in groups" :key="g.id" class="candidate" open>
   <summary><b>{{name(g)}}</b><span>{{g.candidates.flatMap(c=>changedStages(c)).map(r=>roleNames[r]).join(' · ')}}</span><small v-if="!g.candidates.length">{{g.status==='GENERATING'?'生成中':g.status==='FAILED'?'生成失败':'无新增修改'}}</small></summary>
   <p v-if="g.reason&&!g.candidates.length" class="muted">{{g.reason}}</p>
   <section v-for="c in g.candidates" :key="c.id">
    <p><span class="pill" :class="current(c)?'success':''">{{current(c)?'当前版本':statusNames[c.status]||c.status}}</span></p>
    <CandidateTrace :candidate="c"/>
    <div class="row"><button v-if="['STATIC_PASSED','VALIDATION_PASSED'].includes(c.status)" class="button secondary" :disabled="acting" @click="$emit('action',c,'evaluate')">运行评测</button><button class="button primary" :disabled="acting||current(c)||!canPublish(c)" @click="$emit('action',c,'publish')">确认发布</button><button class="button secondary" :disabled="acting||!canRestore(c)" @click="$emit('action',c,'rollback')">{{current(c)?'当前版本':'回滚至此版本'}}</button></div>
   </section>
  </details>
 </section>
</template>
