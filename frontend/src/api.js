export async function api(path,body,timeout=120000){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
 try{
  const token=sessionStorage.getItem('govern-token');
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:controller.signal});
  let value;try{value=await response.json();}catch{throw Error(`后端返回非JSON响应（HTTP ${response.status}），请检查服务和地址`);}
  if(!response.ok){const error=Error(value.error||value.detail||`HTTP ${response.status}`);error.status=response.status;throw error;}
  return value;
 }catch(error){if(error.name==='AbortError')throw Error('请求等待超时，请刷新任务列表确认是否已提交，勿重复点击');if(error instanceof TypeError)throw Error('无法连接后端，请检查本地Govern服务是否运行');throw error;}
 finally{clearTimeout(timer);}
}
