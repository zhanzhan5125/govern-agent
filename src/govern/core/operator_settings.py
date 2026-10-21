"""Local operator settings. Credentials are write-only through the HTTP API."""
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request
import urllib.error
import urllib.parse
import base64
from govern.integrations.atlassian import adapter
from govern.runtime.verification import VerificationGateway, ExecutionProfile


def public_settings(w):
    from govern.policy.auto_evolution import configuration
    model=w.service.llm_config or {};jira=w.config.get('jira',{});mcp=jira.get('mcp',{})
    return {'model':{'provider':model.get('provider','custom'),'base_url':model.get('base_url',''),
        'model':model.get('model',''),'key_configured':bool(model.get('api_key')),
        'configured':all(model.get(k) for k in ('base_url','model','api_key'))},
        'jira':{'configured':bool(w.jira.client),'transport':'http' if mcp.get('url') else 'stdio',
            'requirements_ready':bool(w.jira.client) and (bool(jira.get('cloud_id')) if jira.get('preset')=='atlassian' else all(jira.get('tools',{}).get(k) for k in ('get_issue','list_requirements'))),
            'preset':jira.get('preset','custom'),'cloud_id':jira.get('cloud_id',''),'email':jira.get('email',''),'auth':jira.get('auth','basic'),
            'url':mcp.get('url',''),'command':mcp.get('command',''),'args':mcp.get('args',[]),
            'has_auth':bool(mcp.get('headers') or mcp.get('env')),'bug_project':jira.get('bug_project',''),
            'issue_type_id':jira.get('issue_type_id',''),'issue_type_name':jira.get('issue_type_name','Bug'),
            'acceptance_field':jira.get('acceptance_field','acceptance_criteria'),'tools':jira.get('tools',{})},
        'runtime':{'node':w.bridge.command[0],'ripgrep':bool(shutil.which('rg')),
            'docker_available':w.verification.backend.available(),'profiles':{k:vars(v) for k,v in w.verification.profiles.items()}},
        'storage':{'database':'PostgreSQL' if w.service.settings.database_url else 'SQLite',
            'config_path':os.environ.get('GOVERN_CONFIG','not configured'),'queue':w.service.queue.backend},
        'model_prices':w.config.get('model_prices',{}),'auto_evolution':configuration(w.config)}


def save_settings(w,payload):
    with w.lock:
        auto_only=set(payload)=={'auto_evolution'}
        if not auto_only and getattr(getattr(w,'auto_evolution',None),'running',False):raise ValueError('自动进化正在执行，请等待完成后修改连接配置')
        generation_lock=getattr(getattr(w,'evolution',None),'generation_lock',None)
        if not auto_only and generation_lock and generation_lock.locked():raise ValueError('候选正在生成，请等待完成后修改配置')
        if any(t['state'] in {'PENDING','EXECUTING','PLANNING','VALIDATING'} for t in w.store.list_tasks(100)):
            raise ValueError('有任务正在运行，请等待完成或取消后再修改连接配置')
        config=json.loads(json.dumps(w.config))
        if 'auto_evolution' in payload:
            from govern.policy.auto_evolution import validate
            config['auto_evolution']=validate(payload['auto_evolution'])
        if 'model' in payload:
            supplied=payload['model'];previous=w.service.llm_config or {}
            model={k:str(supplied.get(k,previous.get(k,''))).strip() for k in ('base_url','model')}
            url=urllib.parse.urlsplit(model['base_url'])
            if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query:
                raise ValueError('模型地址应为不包含凭证、查询参数的HTTP(S) Base URL')
            previous_url=urllib.parse.urlsplit(previous.get('base_url',''))
            if previous.get('api_key') and (url.scheme,url.netloc)!=(previous_url.scheme,previous_url.netloc) and not supplied.get('api_key'):
                raise ValueError('更换模型API域名时需要提供新密钥，不会将原有密钥复用到其他服务')
            model.update(provider='custom',api_key=supplied.get('api_key') or previous.get('api_key',''),headers=previous.get('headers',{}))
            if not model['model'] or not model['api_key']:raise ValueError('需要模型名称及API密钥')
            config['model']=model
        if 'jira' in payload:
            supplied=payload['jira'];old=config.get('jira',{})
            if supplied.get('enabled') is False:config.pop('jira',None)
            else:
                mcp=json.loads(json.dumps(old.get('mcp',{})))
                if supplied.get('preset')=='atlassian':
                    mcp={'url':'https://mcp.atlassian.com/v2/mcp','headers':old.get('mcp',{}).get('headers',{})}
                    if supplied.get('auth','basic')=='basic' and supplied.get('api_token'):
                        if not supplied.get('email'):raise ValueError('个人API Token认证需要Atlassian账号Email')
                        credentials=(supplied['email']+':'+supplied['api_token']).encode()
                        mcp['headers']={'Authorization':'Basic '+base64.b64encode(credentials).decode()}
                    elif supplied.get('bearer_token'):mcp['headers']={'Authorization':'Bearer '+supplied['bearer_token']}
                    if not mcp['headers']:raise ValueError('请先提供官方MCP个人API Token或服务账号凭证；仅填写URL不能连接Jira')
                elif supplied.get('transport')=='stdio':
                    if not supplied.get('command'):raise ValueError('请输入MCP启动命令的可执行文件路径')
                    mcp.pop('url',None);mcp.update(command=supplied['command'],args=supplied.get('args',[]))
                else:
                    url=urllib.parse.urlsplit(supplied.get('url',''))
                    if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query:raise ValueError('请输入有效MCP HTTP URL，不要将凭证放在URL中')
                    mcp.pop('command',None);mcp.pop('args',None);mcp['url']=supplied['url']
                    if supplied.get('bearer_token'):mcp['headers']={'Authorization':'Bearer '+supplied['bearer_token']}
                tools=supplied.get('tools',old.get('tools',{}))
                if not isinstance(tools,dict):raise ValueError('工具映射必须是JSON对象')
                config['jira']={'mcp':mcp,'tools':tools,'bug_project':supplied.get('bug_project',''),
                    'issue_type_id':str(supplied.get('issue_type_id',old.get('issue_type_id',''))).strip(),
                    'issue_type_name':str(supplied.get('issue_type_name',old.get('issue_type_name','Bug'))).strip() or 'Bug',
                    'preset':supplied.get('preset','custom'),'cloud_id':supplied.get('cloud_id',''),
                    'email':supplied.get('email',''),'auth':supplied.get('auth','basic'),
                    'acceptance_field':supplied.get('acceptance_field') or 'acceptance_criteria',
                    'priority_map':old.get('priority_map',{'P0':'Highest','P1':'High','P2':'Medium'})}
        if 'model_prices' in payload:
            prices=payload['model_prices']
            if any(not isinstance(v,(int,float)) or v<0 for v in prices.values()):raise ValueError('单价必须为非负数字')
            config['model_prices']=prices
        location=Path(os.environ.get('GOVERN_CONFIG',Path.cwd()/'.govern/config.json'))
        location.parent.mkdir(parents=True,exist_ok=True)
        temporary=location.with_suffix('.tmp')
        temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,location)
        os.environ['GOVERN_CONFIG']=str(location)
        if auto_only:
            w.config=config
            if getattr(w,'auto_evolution',None):w.auto_evolution.wake.set()
            return public_settings(w)
        if w.jira.client:w.jira.client.close()
        w.config=config;w.jira=adapter(config.get('jira'))
        if config.get('model'):w.service.llm_config=config['model']
        # Changing configuration invalidates candidate runtime identities; tasks already froze their inputs.
        from govern.policy.feedback_evolution import GovernEvolution
        w.evolution=GovernEvolution(w)
        if getattr(w,'auto_evolution',None):w.auto_evolution.wake.set()
        return public_settings(w)


def test_model(w):
    model=w.service.llm_config or {}
    if not all(model.get(k) for k in ('base_url','api_key','model')):raise ValueError('请先配置并保存模型')
    started=time.monotonic()
    request=urllib.request.Request(model['base_url'].rstrip('/')+'/chat/completions',
        data=json.dumps({'model':model['model'],'messages':[{'role':'user','content':'Reply with OK only.'}],'max_tokens':32,'stream':False}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+model['api_key'],**model.get('headers',{})})
    try:
        with urllib.request.urlopen(request,timeout=45) as response:result=json.load(response)
        if not result.get('choices'):raise ValueError('服务返回了空choices，不能用于审查')
        return {'connected':True,'model':result.get('model',model['model']),'latency_ms':round((time.monotonic()-started)*1000),'usage':result.get('usage',{})}
    except urllib.error.HTTPError as exc:
        text=exc.read(1500).decode('utf-8','replace').replace(model['api_key'],'[REDACTED]')
        raise ValueError('模型连接失败 HTTP %s: %s'%(exc.code,text)) from None
