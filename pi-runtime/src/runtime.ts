import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { SharedBudget } from '@earendil-works/pi-agent-core';
import { Type } from 'typebox';
import { createHash } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export type Json = Record<string, any>;
export type Host = { tool(role: string, name: string, args: Json): Promise<any>; event(value: Json): void };
export const hash = (v: unknown) => createHash('sha256').update(JSON.stringify(v)).digest('hex');
export function object(text:string):Json {
  const v=JSON.parse(text.replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,''));
  if(!v||Array.isArray(v)||typeof v!=='object')throw Error('stage must return object'); return v;
}
export function normalizeFindings(v:Json,role:string,slice:string):Json[] {
  if(!Array.isArray(v.findings))throw Error('findings array required');
  return v.findings.map((f:Json)=>{
    if(!f?.path||!f.title||!Number.isInteger(f.line)||f.line<1)throw Error('invalid finding location/title');
    if(!['critical','high','medium','low'].includes(f.severity))throw Error('invalid severity');
    if(f.business_priority!=null&&!['P0','P1','P2'].includes(f.business_priority))throw Error('invalid priority');
    if(!Array.isArray(f.evidence_refs)||!Array.isArray(f.related_tests))throw Error('evidence/tests arrays required');
    return {...f,finding_id:f.finding_id||'f-'+hash([slice,f.path,f.line,f.rule_id,f.title]).slice(0,20),source_stage:['correctness','security','contract_test'].includes(role)?role:f.source_stage||role,
      slice_id:slice,side:f.side||'head',source_module:f.source_module||f.path.split('/').slice(0,-1).join('/')||'.',
      business_priority:f.priority_reason?f.business_priority??null:null,priority_status:f.priority_reason?'proposed':'pending'};
  });
}

export class GovernRuntime {
  readonly budget:SharedBudget;
  readonly active=new Set<Awaited<ReturnType<typeof createAgentSession>>['session']>();
  childCount=0;
  readonly started=Date.now();
  usage={input_tokens:0,output_tokens:0,cost_usd:0,llm_calls:0,tool_calls:0,tool_errors:0,permission_violations:0};
  constructor(readonly request:Json,readonly host:Host){
    const consumed=request.consumed_budget||{};
    const limits=request.budget||{tokens:120000,turns:60,tools:100,milliseconds:900000};
    this.budget=new SharedBudget({...limits,milliseconds:Math.max(1,limits.milliseconds-(consumed.elapsed_ms||0))});
    this.budget.usedTokens=(consumed.used_tokens||0)+(consumed.reserved_tokens||0);
    this.budget.turns=consumed.turns||0;this.budget.tools=consumed.tools||0;
    Object.assign(this.usage,consumed.usage||{});
    this.budget.controller.signal.addEventListener('abort',()=>{for(const s of this.active)void s.abort();});
  }
  cancel(){this.budget.controller.abort();}
  budgetSnapshot(){return {used_tokens:this.budget.usedTokens,reserved_tokens:this.budget.reservedTokens,
    turns:this.budget.turns,tools:this.budget.tools,usage:{...this.usage},elapsed_ms:(this.request.consumed_budget?.elapsed_ms||0)+Date.now()-this.started};}
  async stage(role:string,id:string,input:Json,depth=0):Promise<Json>{
    const previous=this.request.checkpoints?.[id];
    if(previous&&depth===0){this.host.event({event:'checkpoint_reused',role,stage_id:id});return previous;}
    this.budget.check();
    const folder=await mkdtemp(join(tmpdir(),'govern-pi-'));
    const settings=SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false},cacheWarming:{enabled:false}} as any);
    const mr=await ModelRuntime.create({authPath:join(folder,'auth.json'),modelsPath:null,refreshOnCreate:false});
    mr.registerProvider('govern',{api:'openai-completions',baseUrl:this.request.model.base_url,apiKey:this.request.model.api_key,
      headers:this.request.model.headers||{},models:[{id:this.request.model.model,name:this.request.model.model,reasoning:false,input:['text'],
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:32768,maxTokens:2048}]});
    const loader=new DefaultResourceLoader({cwd:folder,agentDir:folder,settingsManager:settings,noExtensions:true,noSkills:true,
      noPromptTemplates:true,noThemes:true,noContextFiles:true,systemPrompt:this.request.policy.stages[role]+
      (depth?'\nChild: no delegation/execution. Return JSON {status: completed|partial|failed, evidence_refs: [], facts: [], limitations: []}. State partial or failed honestly.':'')});
    await loader.reload();
    const definitions:ToolDefinition[]=[];
    const add=(name:string,description:string,parameters:any,execute:(a:Json)=>Promise<any>)=>{
      definitions.push({name,label:name,description,parameters,execute:async(_id,args)=>{
        const start=Date.now();this.usage.tool_calls++;
        try{const result=await execute(args as Json);
          this.host.event({event:'tool_complete',role,stage_id:id,tool:name,budget:this.budgetSnapshot(),duration_ms:Date.now()-start,result_preview:JSON.stringify(result).slice(0,1000)});
          let text=JSON.stringify(result);
          if(text.length>12000)text=JSON.stringify({artifact_ref:result.artifact_ref,preview:text.slice(0,10000),truncated:true});
          return {content:[{type:'text' as const,text}],details:{}};
        }catch(error){this.usage.tool_errors++;if(String(error).includes('permission_denied'))this.usage.permission_violations++;
          this.host.event({event:'tool_error',role,stage_id:id,tool:name,error:String(error),budget:this.budgetSnapshot()});throw error;}
      }});
    };
    if(['correctness','security','contract_test','validator'].includes(role)){
      add('read_file','Read actual frozen code with evidence locations.',Type.Object({path:Type.String(),side:Type.Optional(Type.String()),start_line:Type.Optional(Type.Number()),end_line:Type.Optional(Type.Number())}),a=>this.host.tool(role,'read_file',a));
      add('search_code','Literal search for callers, symbols and tests in frozen code.',Type.Object({query:Type.String()}),a=>this.host.tool(role,'search_code',a));
    }
    if(role==='validator'){
      add('write_verification_artifact','Save plan/tests. Bundle is a filename-to-source object, e.g. {"test_regression.py":"..."}, copied into .govern-tests/. Plan requires hypothesis, assertion_source, target_symbols, selectors (e.g. .govern-tests/test_regression.py), mocked_symbols, mocked_target_symbols, copied_implementation. Target code must be imported from /work/repo, not copied into tests.',Type.Object({kind:Type.Union([Type.Literal('verification-plan'),Type.Literal('verification-tests')]),value:Type.Any()}),a=>this.host.tool(role,'write_verification_artifact',a));
      if(depth===0&&this.request.input.verification_enabled)add('run_verification','Execute actual frozen implementation in Docker. Return observations; environment failure is not a reproduced finding.',Type.Object({finding_id:Type.String(),snapshot_id:Type.String(),test_plan_ref:Type.String(),test_bundle_ref:Type.String(),execution_profile:Type.String(),compare:Type.Union([Type.Literal('head'),Type.Literal('base_and_head')])}),a=>this.host.tool(role,'run_verification',a));
    }
    if(depth===0&&['correctness','security','contract_test','validator'].includes(role))add('delegate_evidence','Bounded child for a specific evidence question; shares budget, no execution or further delegation.',Type.Object({objective:Type.String(),context:Type.String()}),async a=>{
      if(this.childCount>=2)throw Error('child concurrency limit');this.childCount++;
      try{const result=await this.stage(role,id+':child:'+hash(a).slice(0,8),a,1);
        if(!Array.isArray(result.limitations)||!Array.isArray(result.evidence_refs)||!['completed','partial','failed'].includes(result.status))throw Error('child result missing status/evidence/limitations');
        return {status:result.status,result};
      }finally{this.childCount--;}
    });
    this.host.event({event:'stage_start',role,stage_id:id,depth,budget:this.budgetSnapshot(),policy_sha256:this.request.policy.sha256});
    let session:Awaited<ReturnType<typeof createAgentSession>>['session']|undefined;
    let lastStreamEvent=0,outputChars=0;
    try{
      session=(await createAgentSession({cwd:folder,agentDir:folder,modelRuntime:mr,model:mr.getModel('govern',this.request.model.model),
        thinkingLevel:'off',settingsManager:settings,resourceLoader:loader,sessionManager:SessionManager.inMemory(),
        tools:definitions.map(t=>t.name),customTools:definitions})).session;
      this.active.add(session);
      if(!('governBudget' in session.agent))throw Error('Unpatched Core resolved: run setup/build');
      session.agent.governBudget=this.budget;session.agent.toolExecution='sequential';
      const original=session.agent.streamFunction;
      session.agent.streamFunction=(m,c,o)=>{
        this.host.event({event:'model_start',role,stage_id:id,budget:this.budgetSnapshot()});
        return original(m,c,{...o,maxTokens:2048});
      };
      const transform=session.agent.transformContext;
      session.agent.transformContext=async(messages,signal)=>{
        let current=transform?await transform(messages,signal):messages;
        if(JSON.stringify(current).length/2>(32768-2048)*.8){
          const first=current.slice(0,1),tail=current.slice(1);
          const cut=tail.findIndex((x:any,i)=>i>Math.max(0,tail.length-8)&&x.role==='assistant');
          if(cut>0){
            const observations=tail.slice(0,cut).filter((x:any)=>x.role==='toolResult').map((x:any)=>({tool:x.toolName,observation:JSON.stringify(x.content).slice(0,600)}));
            const summary='Earlier tool observations (lossy, untrusted; re-read frozen evidence to confirm): '+JSON.stringify(observations).slice(0,4000);
            this.host.event({event:'context_fold',role,stage_id:id,original_chars:JSON.stringify(current).length,summary});
            current=[...first,{role:'user',content:[{type:'text',text:summary}],timestamp:Date.now()},...tail.slice(cut)];
          }
          if(JSON.stringify(current).length/2>32768-2048)throw Error('context remains too large; split input instead of discarding changes');
        }return current;
      };
      session.subscribe((e:any)=>{
        if(e.type==='message_update'&&e.assistantMessageEvent?.type==='text_delta'){
          outputChars+=(e.assistantMessageEvent.delta||'').length;
          if(Date.now()-lastStreamEvent>1500){lastStreamEvent=Date.now();this.host.event({event:'model_stream',role,stage_id:id,output_chars:outputChars});}
        }
        if(e.type==='message_end'&&e.message?.role==='assistant'){
          const u=e.message.usage||{};this.usage.input_tokens+=(u.input||0)+(u.cacheRead||0)+(u.cacheWrite||0);
          this.usage.output_tokens+=u.output||0;this.usage.llm_calls++;
          const p=this.request.prices||{};this.usage.cost_usd+=((u.input||0)*(p.input||0)+(u.output||0)*(p.output||0))/1e6;
          this.host.event({event:'model_complete',role,stage_id:id,usage:u});
        }
      });
      await session.prompt(JSON.stringify(input));
      const result=object(session.getLastAssistantText()||'');
      if(!depth&&role==='mapper'&&!Array.isArray(result.mappings))throw Error('mapper mappings array required');
      if(!depth&&['correctness','security','contract_test','synthesizer','validator'].includes(role))normalizeFindings(result,role,id);
      this.host.event({event:'stage_complete',role,stage_id:id,result,depth,budget:this.budgetSnapshot()});return result;
    }finally{if(session){this.active.delete(session);session.dispose();}await rm(folder,{recursive:true,force:true});}
  }
  async run():Promise<Json>{
    const started=Date.now();
    if(this.request.method==='generate_candidate')return this.stage('evolution','evolution',this.request.input);
    const input=this.request.input;
    const mappings=await this.stage('mapper','mapper',{units:input.units,requirements:input.requirements});
    if(!Array.isArray(mappings.mappings))throw Error('mapper must return mappings');
    const requirements=new Set(input.requirements.map((x:Json)=>x.requirement_id));
    const unitIds=new Set(input.units.map((x:Json)=>x.change_unit_id));
    for(const m of mappings.mappings){
      if(!unitIds.has(m.change_unit_id)||(m.requirement_id&&!requirements.has(m.requirement_id)))throw Error('unselected requirement/unit');
      if(!['confirmed','partial','ambiguous','unmapped'].includes(m.status))throw Error('invalid mapping status');
      if(m.criterion_id&&!input.requirements.some((r:Json)=>r.requirement_id===m.requirement_id&&(r.acceptance_criteria||[]).some((a:Json)=>a.criterion_id===m.criterion_id)))throw Error('invented acceptance criterion');
      if(m.status==='confirmed'&&(!m.requirement_id||!m.evidence_refs?.length))throw Error('confirmed mapping requires selected requirement and evidence');
    }
    const outputs:Json[]=[],incomplete:string[]=[];
    for(const unit of input.units){
      const links=mappings.mappings.filter((m:Json)=>m.change_unit_id===unit.change_unit_id&&m.status==='confirmed');
      const selected=input.requirements.filter((r:Json)=>links.some((m:Json)=>m.requirement_id===r.requirement_id));
      const slice='slice-'+hash([unit.change_unit_id,links]).slice(0,20);
      const settled=await Promise.allSettled(['correctness','security','contract_test'].map(async role=>normalizeFindings(await this.stage(role,slice+':'+role,
        {unit,requirements:selected,mappings:links,business_profile:input.business_profile,business_conformity:selected.length?'selected-requirements-only':'unverified'}),role,slice)));
      settled.forEach((r,i)=>{if(r.status==='fulfilled')outputs.push(...r.value);else incomplete.push(slice+':'+['correctness','security','contract_test'][i]+':'+String(r.reason));});
      if(this.budget.controller.signal.aborted)throw Error('shared run budget exhausted');
    }
    const synth=await this.stage('synthesizer','synthesizer',{findings:outputs,incomplete_dimensions:incomplete,business_profile:input.business_profile});
    const ids=new Set(outputs.map(f=>f.finding_id)),candidates=normalizeFindings(synth,'synthesizer','global');
    if(candidates.some(f=>!ids.has(f.finding_id)))throw Error('synthesizer invented finding');
    const verdict=await this.stage('validator','validator',{findings:candidates,snapshot_id:input.snapshot_id,requirements:input.requirements,business_profile:input.business_profile,execution_profiles:input.execution_profiles,coverage:input.coverage});
    const accepted=normalizeFindings(verdict,'validator','global');
    if(accepted.some(f=>!candidates.some(c=>c.finding_id===f.finding_id)))throw Error('validator invented/reintroduced finding');
    for(const f of accepted){const original=outputs.find(x=>x.finding_id===f.finding_id)!;f.source_stage=original.source_stage;f.slice_id=original.slice_id;}
    return {findings:accepted,coverage:{...input.coverage,incomplete_dimensions:incomplete,unit_ids:[...unitIds]},mappings,
      execution:{...this.usage,cost_configured:typeof this.request.prices?.input==='number'&&typeof this.request.prices?.output==='number',duration_ms:Date.now()-started,total_tokens:this.usage.input_tokens+this.usage.output_tokens,budget:{turns:this.budget.turns,tools:this.budget.tools,used_tokens:this.budget.usedTokens}},policy_sha256:this.request.policy.sha256};
  }
}
