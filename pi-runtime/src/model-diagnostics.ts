import {createHash} from 'node:crypto';
type J=Record<string,any>;
export function redact(value:unknown,secrets:string[]=[]):string{
 let text=String(value??'');
 for(const secret of secrets.filter(s=>s.length>=4).sort((a,b)=>b.length-a.length))text=text.split(secret).join('[REDACTED]');
 return text.replace(/Bearer\s+[^\s"',;]+/gi,'Bearer [REDACTED]')
  .replace(/\bsk-[A-Za-z0-9_-]+/g,'[REDACTED]')
  .replace(/((?:authorization|api[_-]?key|access[_-]?token|cookie)\s*["']?\s*[:=]\s*)[^,;\n]+/gi,'$1[REDACTED]')
  .replace(/https?:\/\/[^\s"'<>]+/gi,url=>{try{const u=new URL(url);return u.origin+u.pathname;}catch{return '[URL]';}});
}
export function responseMetadata(value:{status:number;headers:Record<string,string>},secrets:string[]=[]):J{
 const headers=Object.fromEntries(Object.entries(value.headers||{}).map(([k,v])=>[k.toLowerCase(),v]));
 const requestId=headers['x-request-id']||headers['request-id']||headers['x-amzn-requestid'];
 return {http_status:value.status,request_id:requestId?.match(/^[A-Za-z0-9_.:/-]{1,160}$/)?redact(requestId,secrets):null,
  retry_after:headers['retry-after']?.match(/^\d{1,6}$/)?headers['retry-after']:null};
}
export function modelDiagnostic(error:any,meta:J={},secrets:string[]=[]):J{
 const raw=typeof error==='string'?error:error?.errorMessage||error?.message||String(error);
 const embedded=raw.match(/(?:^|\bHTTP\s+)([45]\d\d)\b/i);
 const status=meta.http_status??(typeof error?.status==='number'?error.status:embedded?Number(embedded[1]):null);
 let detail=raw,providerCode:string|null=null,parameter:string|null=null;
 const begin=raw.indexOf('{');
 if(begin>=0){try{const parsed=JSON.parse(raw.slice(begin));const body=parsed.error||parsed;
   if(typeof body.message==='string')detail=body.message;
   if(typeof body.code==='string'&&/^[\w.-]{1,100}$/.test(body.code))providerCode=body.code;
   if(typeof body.param==='string'&&/^[\w.\[\]-]{1,120}$/.test(body.param))parameter=body.param;
 }catch{/* No raw body is persisted when it is not safely parseable. */}}
 let category='sdk_or_provider_unknown';
 if(/Stage output contract failed|output contract/i.test(raw))category='output_contract';
 else if(/abort|cancel/i.test(raw))category='cancelled';
 else if(/budget|context preflight|context.*(?:limit|length|exceed)|maximum context/i.test(raw))category=/budget/i.test(raw)?'budget':'context_limit';
 else if(status===429)category='rate_limit';else if(status===401||status===403)category='authentication';
 else if(status>=500)category='provider_5xx';else if(status>=400)category='provider_4xx';
 else if(/timeout|timed out/i.test(raw))category='timeout';
 else if(/fetch failed|ECONN|ENOTFOUND|socket|network/i.test(raw))category='network';
 else if(/tool.*(?:call|result)|messages.*(?:role|sequence)/i.test(raw))category='tool_protocol';
 else if(/JSON|SSE|stream|parse|protocol/i.test(raw))category='protocol';
 // Provider errors can echo request bodies; never save raw payloads or full stack traces.
 const message=redact(detail,secrets).split(/[\r\n]/)[0]
  .replace(/((?:prompt|messages|request_body|payload)\s*["']?\s*[:=]).*/i,'$1[OMITTED]')
  .replace(/\{.*\}/g,'[provider body omitted]').slice(0,600);
 return {category,http_status:status,request_id:meta.request_id?redact(meta.request_id,secrets):null,retry_after:meta.retry_after??null,provider_code:providerCode?redact(providerCode,secrets):null,parameter:parameter?redact(parameter,secrets):null,
  message:message||'No provider error detail was supplied',error_sha256:createHash('sha256').update(raw).digest('hex'),
  recoverability:status===429||status>=500||['timeout','network'].includes(category)?'potentially_transient':'unknown_or_nonretryable',
  source:meta.source||'assistant_error',stop_reason:meta.stop_reason||null,last_http_status:meta.last_http_status??null,last_call_id:meta.last_call_id??null};
}
