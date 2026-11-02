import test from 'node:test';
import assert from 'node:assert/strict';
import {modelDiagnostic,responseMetadata} from '../src/model-diagnostics.js';
test('known HTTP/provider fields survive without retaining raw response bodies or credentials',()=>{
 const raw='400 '+JSON.stringify({error:{message:'Invalid messages sequence; api_key=secret-key-value; payload={"source":"private source"}',code:'invalid_request',param:'messages[2]'}});
 const d=modelDiagnostic(raw,{http_status:400,request_id:'req-123'},['secret-key-value']);
 assert.equal(d.http_status,400);assert.equal(d.request_id,'req-123');assert.equal(d.provider_code,'invalid_request');assert.equal(d.parameter,'messages[2]');
 assert.ok(d.message.includes('Invalid messages sequence'));assert.ok(!JSON.stringify(d).includes('secret-key-value'));assert.ok(!JSON.stringify(d).includes('private source'));
});
test('HTTP metadata uses an allowlist, including when request ID echoes a credential',()=>{
 const data=responseMetadata({status:429,headers:{'X-Request-ID':'secret-key-value','Retry-After':'5','Set-Cookie':'secret cookie','Authorization':'Bearer secret-key-value'}},['secret-key-value']);
 assert.equal(data.request_id,'[REDACTED]');assert.equal(data.retry_after,'5');assert.ok(!JSON.stringify(data).includes('cookie'));
});
test('transport, context, protocol, cancellation and unknown failures remain distinguishable',()=>{
 for(const [error,category] of [['429 rate limited','rate_limit'],['503 service unavailable','provider_5xx'],['maximum context length exceeded','context_limit'],['fetch failed ECONNRESET','network'],['request timed out','timeout'],['invalid tool result sequence','tool_protocol'],['aborted','cancelled'],['unspecified failure','sdk_or_provider_unknown']])assert.equal(modelDiagnostic(error).category,category);
 const prior=modelDiagnostic('sdk failed after tool execution',{last_http_status:200,last_call_id:'v:model:2',http_status:null});
 assert.equal(prior.http_status,null);assert.equal(prior.last_http_status,200);
});
