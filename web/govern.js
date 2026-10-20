const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let token=localStorage.getItem('govern_token')||'',taskId='',task=null,catalog=[];
const esc=v=>{const el=document.createElement('span');el.textContent=typeof v==='object'?JSON.stringify(v,null,2):String(v??'未提供／未验证');return el.innerHTML.replaceAll('"','&quot;').replaceAll("'",'&#39;');};
const status=v=>$('#status').textContent=v;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body?{body:JSON.stringify(body)}:{})});const v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v;}
function tab(id){$$('main>section').forEach(s=>s.hidden=s.id!==id);$('#title').textContent=$(`button[data-tab="${id}"]`).textContent;}
async function attempt(fn){try{status('处理中…');await fn();if($('#status').textContent==='处理中…')status('');}catch(e){status(e.message);}}
$$('[data-tab]').forEach(b=>b.onclick=()=>attempt(async()=>{tab(b.dataset.tab);if(b.dataset.tab==='policy'||b.dataset.tab==='evaluation')await policies();if(b.dataset.tab==='monitor')await history();}));
$('#login').onsubmit=e=>{e.preventDefault();attempt(async()=>{const f=new FormData(e.target),v=await api('/v1/auth/login',Object.fromEntries(f));token=v.access_token||v.token;localStorage.setItem('govern_token',token);await history();});};
$('#inspect').onclick=()=>attempt(async()=>{const path=new FormData($('#review-form')).get('path');const v=await api('/v1/govern/repository?path='+encodeURIComponent(path));$('#repo-status').textContent=`${v.branch} · ${v.dirty?'存在未提交修改':'工作区干净'}`;$('#refs').innerHTML=v.refs.map(r=>`<option value="${esc(r)}">`).join('');$('#commits').textContent=v.commits.map(c=>`${c.sha.slice(0,8)} ← ${c.parents.map(p=>p.slice(0,8)).join(', ')}  ${c.subject}`).join('\n');});
$('#load-requirements').onclick=()=>attempt(async()=>{const epic=new FormData($('#review-form')).get('epic');const v=await api('/v1/govern/requirements?epic='+encodeURIComponent(epic));catalog=v.requirements;$('#requirements').innerHTML=catalog.map((r,i)=>`<details><summary><input type="checkbox" name="requirement" value="${i}"> ${esc(r.requirement_id)} · ${esc(r.title)}</summary><pre>${esc(r.description)}</pre><pre>${esc(r.acceptance_criteria)}</pre></details>`).join('');});
$('#review-form').onsubmit=e=>{e.preventDefault();attempt(async()=>{const f=new FormData(e.target);const v=await api('/v1/govern/reviews',{source:{kind:f.get('source'),path:f.get('path'),url:f.get('url')},scope:{mode:f.get('mode'),base:f.get('base'),head:f.get('head'),include_untracked:f.has('untracked')},epic_key:f.get('epic'),requirement_keys:f.getAll('requirement').map(i=>catalog[Number(i)].requirement_id),business_profile:JSON.parse(f.get('business')||'{}'),verification_enabled:f.has('verification')});taskId=v.task_id;tab('report');await history();await refresh();});};
async function history(){
 const data=await api('/api/tasks?limit=100'),rows=data.tasks;
 $('#task-select').innerHTML='<option value="">选择历史审查</option>'+rows.map(t=>`<option value="${esc(t.id)}" ${t.id===taskId?'selected':''}>${esc(t.repository)} · ${esc(t.state)} · ${t.id.slice(0,8)}</option>`).join('');
 $('#history').innerHTML=rows.map(t=>`<article><button data-run="${esc(t.id)}">${esc(t.repository)}</button> ${esc(t.state)} · ${esc(t.created_at)}</article>`).join('');
 $$('[data-run]').forEach(b=>b.onclick=()=>{taskId=b.dataset.run;tab('report');attempt(refresh);});
 const completed=await Promise.all(rows.filter(t=>t.state==='SUCCESS').slice(0,20).map(t=>api('/v1/tasks/'+t.id)));
 const total=completed.reduce((a,t)=>{const x=t.report?.execution||{},v=t.report?.verification_metrics||{};
   a.tokens+=x.total_tokens||0;a.cost+=x.cost_usd||0;a.unpriced+=!x.cost_configured;
   a.attempts+=v.verification_attempts||0;a.sandbox_ms+=v.sandbox_duration_ms||0;a.timeouts+=v.verification_timeouts||0;a.environments+=v.verification_environment_errors||0;return a;
 },{tokens:0,cost:0,unpriced:0,attempts:0,sandbox_ms:0,timeouts:0,environments:0});
 $('#metrics').innerHTML=`<div class="metric">任务样本 ${rows.length}（最多100）</div><div class="metric">最近20份完成报告Token ${total.tokens}</div><div class="metric">已配置单价的估算费用 $${total.cost.toFixed(4)}；${total.unpriced}份未定价</div><div class="metric">验证尝试 ${total.attempts} · 沙箱 ${(total.sandbox_ms/1000).toFixed(1)}秒</div><div class="metric">超时 ${total.timeouts} · 环境不足 ${total.environments}</div>`;
}
async function refresh(){if(!taskId)return;task=await api('/v1/tasks/'+taskId);if(!task.input?.govern){$('#report-content').textContent='旧版任务请使用兼容控制台查看';return;}render();$('#trace-content').innerHTML=(task.runtime_journal||[]).map(e=>`<details><summary>${esc(e.sequence)} · ${esc(e.node)} · ${esc(e.kind)}</summary><pre>${esc(e.detail||e)}</pre></details>`).join('');}
function render(){const report=task?.report;$('#report-content').innerHTML=`<p>${esc(task.state)} ${esc(task.error||'')}</p>`;if(!report)return;$('#report-content').innerHTML+=`<details open><summary>覆盖与限制</summary><pre>${esc(report.coverage)}</pre></details>`;const priority=$('#priority').value;for(const f of report.findings||[]){if(priority&&(priority==='pending'?f.business_priority!=null:f.business_priority!==priority))continue;const fields={'风险等级':`${f.business_priority||'待确认'} / ${f.severity}`,'来源模块':f.source_module,'关联测试用例':f.related_tests,'风险位置':`${f.path}:${f.line} (${f.side})`,'问题描述':f.description,'影响范围':f.impact_scope,'修复建议':f.recommendation,'证据依据':f.evidence_refs,'运行验证':{verdict:f.runtime_verdict||'not_applicable',refs:f.verification_refs||[],limitations:f.limitations||[]}};$('#report-content').innerHTML+=`<article><h3><span class="badge ${esc(f.business_priority)}">${esc(f.business_priority||'待确认')}</span> ${esc(f.title)}</h3><dl>${Object.entries(fields).map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl><button data-accepted="${esc(f.finding_id)}">问题有效 · 创建Bug</button> <button data-invalid="${esc(f.finding_id)}">置为无效</button></article>`;}$$('[data-accepted]').forEach(b=>b.onclick=()=>feedback(b.dataset.accepted,'accepted'));$$('[data-invalid]').forEach(b=>b.onclick=()=>feedback(b.dataset.invalid,'false_positive'));}
async function feedback(id,type){
 const finding=task.report.findings.find(f=>f.finding_id===id);
 if(type==='accepted'&&!confirm('Jira Bug预览\n'+JSON.stringify({title:finding.title,priority:finding.business_priority,location:`${finding.path}:${finding.line}`,description:finding.description,impact:finding.impact_scope,recommendation:finding.recommendation,evidence:finding.evidence_refs},null,2)+'\n确认保存有效反馈并创建Bug？'))return;
 const reason=prompt(type==='accepted'?'确认有效的依据':'无效原因：上游校验／历史问题／需求误解／证据不足等');if(!reason)return;
 const reason_code=type==='accepted'?'confirmed':prompt('归因代码：upstream_guard / preexisting / requirement_mismatch / insufficient_evidence / other','insufficient_evidence');if(!reason_code)return;
 await attempt(async()=>{const v=await api(`/v1/govern/tasks/${taskId}/feedback`,{finding_id:id,type,reason,reason_code,create_bug:type==='accepted'});status(v.bug_error?'反馈已保存；建Bug失败：'+v.bug_error:'反馈已记录 '+JSON.stringify(v.bug||''));});
}
$('#missed').onsubmit=e=>{e.preventDefault();attempt(async()=>{const f=new FormData(e.target);await api(`/v1/govern/tasks/${taskId}/feedback`,{type:'missed_issue',reason:f.get('reason'),reason_code:'missed_behavior',finding:{path:f.get('path'),line:Number(f.get('line')),description:f.get('reason'),source_stage:f.get('stage')}});e.target.reset();});};
$('#priority').onchange=render;$('#task-select').onchange=e=>{taskId=e.target.value;attempt(refresh);};$('#refresh').onclick=()=>attempt(refresh);$('#cancel').onclick=()=>attempt(async()=>{await api(`/v1/tasks/${taskId}/cancel`,{});await refresh();});
$('#export').onclick=()=>{if(!task?.report)return;const url=URL.createObjectURL(new Blob([task.report.report_markdown||''],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`govern-${taskId}.md`;a.click();URL.revokeObjectURL(url);};
async function policies(){
 const p=await api('/v1/govern/policy');$('#policy-content').innerHTML=`<p>Bundle ${esc(p.version)} · ${esc(p.sha256)}</p>`+Object.entries(p.stages).map(([r,v])=>`<details><summary>${esc(r)}</summary><pre>${esc(v)}</pre></details>`).join('');
 const c=await api('/v1/govern/candidates');$('#dataset').textContent=JSON.stringify({dataset:c.dataset,release:c.release},null,2);
 const transitions={STATIC_PASSED:['evaluate','reject'],HOLDOUT_PASSED:['approve','reject'],HUMAN_APPROVED:['shadow','reject'],SHADOW:['activate','reject'],SUPERSEDED:['rollback'],ROLLED_BACK:['rollback']};
 const markup=c.candidates.map(x=>`<article><b>${esc(x.id)} · ${esc(x.status)}</b><pre>${esc(x.lineage?.change_diff||x.reports)}</pre><div class="row">${(transitions[x.status]||[]).map(a=>`<button data-action="${a}" data-id="${esc(x.id)}" data-revision="${x.revision}">${a}</button>`).join('')}</div></article>`).join('');
 $('#candidates').innerHTML=markup;$('#evaluation-content').innerHTML=markup;
 $$('[data-action]').forEach(b=>b.onclick=()=>attempt(async()=>{
   const reason=prompt('操作原因（发布/回滚会影响之后的新审查）','用户确认');if(!reason)return;
   await api(`/v1/govern/candidates/${b.dataset.id}/${b.dataset.action}`,{expected_revision:Number(b.dataset.revision),expected_generation:c.release.generation,reason});await policies();
 }));
}
$('#evolve').onclick=()=>attempt(async()=>{await api('/v1/govern/evolve',{});await policies();});
setInterval(()=>{if(taskId&&task&&['PENDING','EXECUTING'].includes(task.state))refresh().catch(e=>status(e.message));},2500);
attempt(history);
