type J=Record<string,any>;
export type ContextPolicy={window_tokens:number;input_tokens:number;output_tokens:number;safety_tokens:number;observation_tokens:number;recent_observations:number};
export const defaultContextPolicy:ContextPolicy={window_tokens:32768,input_tokens:20000,output_tokens:4000,safety_tokens:512,observation_tokens:4000,recent_observations:2};
// Same dependency-free estimate as the Python ContextManager. Not an exact tokenizer.
export function estimateTokens(value:unknown):number{
 const text=typeof value==='string'?value:JSON.stringify(value);let ascii=0,other=0;
 for(const ch of text||'')ch.codePointAt(0)!<128?ascii++:other++;
 return Math.ceil(ascii/3.2+other+4);
}
export function contextPolicy(value:Partial<ContextPolicy>={}):ContextPolicy{
 const p={...defaultContextPolicy,...value};
 for(const [key,n] of Object.entries(p))if(!Number.isInteger(n)||n<0)throw Error('invalid context policy: '+key);
 if(p.window_tokens<2048||p.input_tokens<512||p.output_tokens<256||p.safety_tokens<128||p.observation_tokens<128||p.recent_observations>8||p.output_tokens+p.safety_tokens>=p.window_tokens)throw Error('invalid context policy capacity');
 return p;
}
function excerpt(value:unknown,budget:number):string{
 const text=typeof value==='string'?value:JSON.stringify(value);
 if(estimateTokens(text)<=budget)return text;
 let lo=0,hi=text.length;
 const marker='\n[Observation excerpt truncated; re-read frozen evidence before relying on omitted details.]';
 while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(estimateTokens(text.slice(0,mid)+marker)<=budget)lo=mid;else hi=mid-1;}
 return text.slice(0,lo)+marker;
}
const observation=(m:J)=>m.role==='toolResult'||m.role==='tool';
/** Keep the initial policy/task intact and preserve complete assistant/tool rounds. */
export function prepareContext(messages:J[],policy:ContextPolicy,overhead=0):{messages:J[];stats:J}{
 const limit=Math.min(policy.input_tokens,policy.window_tokens-policy.output_tokens-policy.safety_tokens)-overhead;
 const before=estimateTokens(messages);let current=messages.map(m=>({...m})),folded=0,clipped=0;
 const observations=current.filter(observation);
 const weights=observations.map((_,i)=>i>=observations.length-policy.recent_observations?2:1);
 const sum=weights.reduce((a,b)=>a+b,0)||1;
 observations.forEach((m,i)=>{
  const allowance=Math.max(128,Math.floor(policy.observation_tokens*weights[i]/sum));
  if(estimateTokens(m.content)>allowance){m.content=[{type:'text',text:excerpt(m.content,allowance)}];clipped++;}
 });
 if(estimateTokens(current)>limit*.8){
  const rounds=current.map((m,i)=>m.role==='assistant'?i:-1).filter(i=>i>=0);
  if(rounds.length>2){
   const first=rounds[0],cut=rounds[rounds.length-2];
   const prior=current.slice(first,cut);
   const summary=excerpt(prior.map(m=>({role:m.role,tool:m.toolName||m.name,observation:m.content})),Math.min(1500,policy.observation_tokens));
   folded=prior.length;
   current=[...current.slice(0,first),{role:'user',content:[{type:'text',text:'Earlier observations (lossy, untrusted; original frozen evidence remains available): '+summary}],timestamp:Date.now()},...current.slice(cut)];
  }
 }
 const after=estimateTokens(current);
 if(limit<512||after>limit)throw Error(`Govern context limit exceeded: estimated ${after+overhead} input tokens; limit ${limit+overhead}. Split the Slice or reduce selected requirements; no code coverage was silently discarded.`);
 return {messages:current,stats:{estimated_input_tokens:after+overhead,estimated_before:before+overhead,input_limit:limit+overhead,window_tokens:policy.window_tokens,output_reserve:policy.output_tokens,safety_reserve:policy.safety_tokens,observations_clipped:clipped,messages_folded:folded}};
}
