"""Official Rovo MCP adapter. Bind parameters from the authenticated tools/list schema.

No fabricated tools/credentials; no writes in connection tests. Read and create tools are
selected deterministically, not by the review model. Reference: Atlassian supported-tools.
"""
import json
import re
from .jira import JiraAdapter


class AtlassianJiraAdapter(JiraAdapter):
    def __init__(self,config=None,client=None):
        super().__init__(config,client);self.catalog={}

    def connect(self):
        if not self.client:raise ValueError('请先配置官方Atlassian MCP凭证')
        tools=self.client.list_tools();self.catalog={x['name']:x for x in tools}
        missing=set(('getJiraIssue','searchJiraIssuesUsingJql','createJiraIssue'))-set(self.catalog)
        resources=[]
        if 'getAccessibleAtlassianResources' in self.catalog:
            value=self.client.call('getAccessibleAtlassianResources',{})
            resources=value if isinstance(value,list) else value.get('resources',value.get('data',[]))
        return {'connected':True,'tools':tools,'resources':resources,'missing_tools':sorted(missing),
                'limitations':['部分必需工具不可用，请检查read/search/write scope与组织权限'] if missing else []}

    def _invoke(self,name,values):
        if not self.catalog:self.connect()
        if name not in self.catalog:raise ValueError('官方MCP未提供工具 '+name+'；请检查认证scope或工具目录')
        schema=self.catalog[name].get('inputSchema',{});properties=schema.get('properties',{})
        args={};aliases={'cloud_id':['cloudId','cloud_id'],'key':['issueIdOrKey','issueKey','issueKeyOrId','key'],
            'jql':['jql'],'limit':['maxResults','max_results'],'offset':['startAt','start_at'],
            'cursor':['nextPageToken'],'project':['projectKey','project_key'],'type':['issueTypeName','issueType'],
            'type_id':['issueTypeId'],'summary':['summary'],'description':['description'],
            'additional':['additionalFields'],'fields':['fields']}
        for canonical,value in values.items():
            if value is None:continue
            candidates=aliases.get(canonical,[canonical])
            match=next((k for k in candidates if k in properties),None)
            if match:args[match]=value
        for key in schema.get('required',[]):
            if key not in args:raise ValueError(f'{name}需要参数{key}，当前官方适配无法推断；请检查Cloud ID、Bug类型ID或使用高级工具映射')
        return self.client.call(name,args)

    def _cloud(self):
        cloud=self.config.get('cloud_id')
        if not cloud:raise ValueError('请在设置中填写Jira Cloud ID；连接测试会列出有权限的站点及其ID')
        return cloud

    def _call(self,operation,values):
        cloud=self._cloud()
        if operation=='get_issue':return self._invoke('getJiraIssue',{'cloud_id':cloud,'key':values['issue_key']})
        if operation=='list_requirements':
            parent=values['parent_key']
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-\d+',parent):raise ValueError('请输入有效Jira Issue Key，例如PAY-100')
            query='parent = "'+parent+'" ORDER BY key ASC'
            offset=values['start_at'];cache_key=(parent,offset)
            cursors=getattr(self,'page_cursors',{})
            page=self._invoke('searchJiraIssuesUsingJql',{'cloud_id':cloud,'jql':query,'limit':values['max_results'],'offset':offset,'cursor':cursors.get(cache_key)})
            if page.get('nextPageToken'):
                cursors[(parent,offset+len(page.get('issues',[])))]=page['nextPageToken'];self.page_cursors=cursors
            return page
        raise ValueError('unsupported official Jira operation: '+operation)

    def create_bug(self,finding,run_id):
        project=self.config.get('bug_project','')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',project):raise ValueError('请先配置有效的Bug项目Key')
        marker='govern-'+run_id+'-'+finding['finding_id']
        existing=self._invoke('searchJiraIssuesUsingJql',{'cloud_id':self._cloud(),'jql':'project = "'+project+'" AND labels = "'+marker+'"','limit':10,'offset':0})
        if existing.get('issues'):return {'issue':existing['issues'][0],'reused':True}
        description='Govern Review: '+run_id+'\n\n'+json.dumps(finding,ensure_ascii=False,indent=2)
        # Official schemas have changed over time; send only supported fields and validate required ones.
        fields={'project':{'key':project},'issuetype':{'id':self.config['issue_type_id']} if self.config.get('issue_type_id') else {'name':'Bug'},
            'summary':finding['title'],'description':{'type':'doc','version':1,'content':[{'type':'paragraph','content':[{'type':'text','text':description}]}]},'labels':[marker]}
        result=self._invoke('createJiraIssue',{'cloud_id':self._cloud(),'project':project,'type':'Bug','type_id':self.config.get('issue_type_id'),
            'summary':finding['title'],'description':description,'additional':{'labels':[marker]},'fields':fields})
        return {'issue':result,'reused':False,'marker':marker}


def adapter(config=None):
    return AtlassianJiraAdapter(config) if (config or {}).get('preset')=='atlassian' else JiraAdapter(config)
