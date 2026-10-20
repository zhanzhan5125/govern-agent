import { createInterface } from 'node:readline';
import { GovernRuntime, type Json } from './runtime.js';
const send=(v:Json)=>process.stdout.write(JSON.stringify(v)+'\n');
const pending=new Map<string,{resolve:(v:any)=>void,reject:(e:Error)=>void}>();
let runtime:GovernRuntime|undefined,counter=0;
const lines=createInterface({input:process.stdin});
lines.on('line',line=>{
  try{
    const m=JSON.parse(line);
    if(m.type==='cancel'){runtime?.cancel();return;}
    if(m.type==='tool_result'){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p?.reject(Error(m.error));else p?.resolve(m.result);return;}
    if(m.type!=='request'||m.protocol!==1||runtime)throw Error('invalid request/protocol');
    runtime=new GovernRuntime(m,{event:e=>send({type:'event',...e}),tool:(role,name,args)=>new Promise((resolve,reject)=>{
      const id=String(++counter);pending.set(id,{resolve,reject});send({type:'tool_request',id,role,name,arguments:args});
    })});
    runtime.run().then(result=>send({type:'result',result})).catch(e=>send({type:'error',error:String(e)}));
  }catch(e){send({type:'error',error:String(e)});}
});
lines.on('close',()=>runtime?.cancel());
