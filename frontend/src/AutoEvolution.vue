<script setup>
import {ref,onMounted,onUnmounted} from 'vue';
import {api} from './api.js';
const status=ref(null),error=ref(''),busy=ref(false),dirty=ref(false);
const form=ref({enabled:false,threshold:10,cooldown_minutes:1440});
const labels={disabled:'已关闭',below_threshold:'等待反馈累计',waiting_authorization:'阈值已满足，等待模型调用授权',cooldown:'冷却中',running:'生成与评测中',awaiting_human_review:'评测通过，等待人工审批',awaiting_evaluation:'候选已生成，评测尚未完成',evaluation_error:'评测出错，需人工检查数据与环境',evaluation_rejected:'评测未通过，候选已拒绝',no_candidates:'未生成候选',failed:'执行失败（冷却后重试）',manual_generation_running:'手动生成正在运行',stopped:'已停止'};
let timer;
async function load(){try{status.value=await api('/v1/govern/auto-evolution');if(!dirty.value)form.value={...status.value.config};}catch(e){error.value=e.message;}}
async function save(){busy.value=true;error.value='';try{await api('/v1/govern/settings',{auto_evolution:form.value});dirty.value=false;await load();}catch(e){error.value=e.message;}finally{busy.value=false;}}
onMounted(()=>{load();timer=setInterval(load,10000);});onUnmounted(()=>clearInterval(timer));
</script>
<template>
 <section class="card">
  <div class="section-heading"><h2>反馈阈值自动进化</h2><span class="pill">发布须人工审批</span></div>
  <p class="muted">同一阶段新增、去重的有效误报／漏报案例达到阈值后，后台生成候选并尝试固定集评测。保留下面的手动生成入口；不会自动批准或发布。</p>
  <form @submit.prevent="save" @input="dirty=true">
   <label><input type="checkbox" v-model="form.enabled"/> 开启反馈阈值自动触发</label>
   <div class="two-col"><div><label for="evolution-threshold">每阶段待处理案例阈值</label><input id="evolution-threshold" type="number" min="1" max="100" step="1" v-model.number="form.threshold" required/></div>
   <div><label for="evolution-cooldown">两轮最小间隔（分钟）</label><input id="evolution-cooldown" type="number" min="1" max="10080" step="1" v-model.number="form.cooldown_minutes" required/></div></div>
   <p v-if="status&&!status.model_calls_authorized" class="notice warning">真实模型自动调用已锁定。即使开启并达到阈值，也只记录“等待授权”，不会发送案例或调用模型。后续由操作者通过服务端 GOVERN_AUTO_EVOLUTION_MODEL_CALLS=1 单独授权。</p>
   <p v-if="error" role="alert">{{error}}</p>
   <button class="button primary" :disabled="busy">{{busy?'保存中…':'保存自动进化配置'}}</button>
  </form>
  <p>当前状态：{{labels[status?.state?.status]||'等待后台检查'}} <span v-if="status?.running"> · 后台处理中</span></p>
  <p v-if="status?.running" class="muted">关闭后不再发起后续生成或评测；已经发出的模型请求可能仍会完成，不会自动发布。</p>
  <p v-if="status?.state?.pending_by_stage">待处理案例：{{JSON.stringify(status.state.pending_by_stage)}}</p>
  <p v-if="status?.state?.last_attempt_at">最近尝试：{{new Date(status.state.last_attempt_at*1000).toLocaleString()}}</p>
  <p v-if="status?.state?.error_type">失败类型：{{status.state.error_type}}（详情请检查服务日志与候选记录）</p>
  <p v-if="status?.state?.candidate_ids?.length">本轮候选：{{status.state.candidate_ids.join('、')}}</p>
  <pre v-if="status?.state?.evaluation?.length">{{JSON.stringify(status.state.evaluation,null,2)}}</pre>
 </section>
</template>
