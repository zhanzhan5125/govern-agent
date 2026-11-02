<script setup>
defineProps({dataset:{type:Object,default:null}});
const sources={ 'synthetic-controlled':'合成受控样本','public-github-pr':'公开GitHub PR','private-historical-pr':'授权历史PR'};
</script>

<template>
 <section class="card">
  <div class="section-heading"><h2>固定评测集</h2><span class="pill" :class="dataset?.mechanism_ready?'success':'warning'">{{dataset?.mechanism_ready?'机制回归可用':'尚未加载可用回归集'}}</span></div>
  <p class="muted">{{dataset?.name||'未配置数据文件'}} · {{dataset?.total_cases??0}} 条 · {{(dataset?.source_kinds||[]).map(s=>sources[s]||s).join('、')||'无数据来源'}}</p>
  <p class="muted">反馈用于策略生成，固定测试集用于评测。</p>
  <div class="split-grid"><div v-for="(v,k) in dataset?.counts||{}" :key="k"><small>{{k}}</small><strong>{{v}}</strong></div></div>
 </section>
</template>
