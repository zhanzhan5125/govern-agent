import json
import tempfile
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from govern.core.operator_settings import public_settings,save_settings
from govern.integrations.atlassian import AtlassianJiraAdapter
from govern.review.repository import capture


class SettingsTests(unittest.TestCase):
    def test_save_model_is_local_and_key_is_write_only(self):
        w=SimpleNamespace(service=SimpleNamespace(llm_config={},settings=SimpleNamespace(database_url=''),queue=SimpleNamespace(backend='memory-acked')),
            store=SimpleNamespace(list_tasks=lambda *args:[]),lock=threading.RLock(),config={},jira=SimpleNamespace(client=None),
            bridge=SimpleNamespace(command=['node']),verification=SimpleNamespace(backend=SimpleNamespace(available=lambda:False),profiles={}))
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'GOVERN_CONFIG':str(Path(folder)/'config.json')}),patch('govern.policy.feedback_evolution.GovernEvolution',lambda _:object()):
            result=save_settings(w,{'model':{'base_url':'https://example.com/v1','model':'test','api_key':'FAKE-TEST-KEY'}})
            self.assertTrue(result['model']['configured']);self.assertNotIn('FAKE-TEST-KEY',json.dumps(result))
            self.assertEqual('FAKE-TEST-KEY',json.loads((Path(folder)/'config.json').read_text())['model']['api_key'])
            with self.assertRaisesRegex(ValueError,'Base URL'):save_settings(w,{'model':{'base_url':'https://secret@example.com/v1'}})
    def test_public_settings_never_echo_model_or_jira_secret(self):
        w=SimpleNamespace(service=SimpleNamespace(llm_config={'model':'m','base_url':'https://example.com/v1','api_key':'SECRET-MODEL'},
            settings=SimpleNamespace(database_url=''),queue=SimpleNamespace(backend='memory-acked')),
            config={'jira':{'mcp':{'url':'https://mcp.atlassian.com/v2/mcp','headers':{'Authorization':'SECRET-JIRA'}}}},
            jira=SimpleNamespace(client=object()),bridge=SimpleNamespace(command=['node']),
            verification=SimpleNamespace(backend=SimpleNamespace(available=lambda:False),profiles={}))
        value=json.dumps(public_settings(w))
        self.assertNotIn('SECRET',value);self.assertIn('key_configured',value)

    def test_official_adapter_uses_real_schema_and_scoped_jql(self):
        calls=[]
        schemas={'getJiraIssue':{'cloudId':{},'issueIdOrKey':{}},'searchJiraIssuesUsingJql':{'cloudId':{},'jql':{},'maxResults':{},'startAt':{}},
            'createJiraIssue':{'cloudId':{},'fields':{}}}
        class Client:
            def list_tools(self):return [{'name':k,'inputSchema':{'properties':v,'required':['cloudId']}} for k,v in schemas.items()]
            def call(self,name,args):
                calls.append((name,args))
                if name=='getJiraIssue':return {'key':args['issueIdOrKey'],'fields':{'summary':'Pay','description':'PRD','acceptance_criteria':['Must reject negative quantities']}}
                if name=='searchJiraIssuesUsingJql':return {'issues':[],'isLast':True}
                return {'key':'PAY-9'}
        adapter=AtlassianJiraAdapter({'cloud_id':'cloud1','bug_project':'PAY'},Client())
        catalog=adapter.requirements('PAY-1')
        self.assertEqual('PAY-1',catalog['requirements'][0]['requirement_id'])
        self.assertIn('parent = "PAY-1"',calls[1][1]['jql'])
        self.assertTrue(all(args['cloudId']=='cloud1' for _,args in calls))
        result=adapter.create_bug({'finding_id':'f1','title':'Wrong quantity'},'run1')
        self.assertEqual('PAY-9',result['issue']['key'])
        self.assertEqual(['govern-run1-f1'],calls[-1][1]['fields']['labels'])
        self.assertEqual('Bug',calls[-1][1]['fields']['issuetype']['name'])

    def test_missing_cloud_id_fails_with_actionable_error(self):
        adapter=AtlassianJiraAdapter({'preset':'atlassian'})
        with self.assertRaisesRegex(ValueError,'Cloud ID'):adapter.requirements('PAY-1')


if __name__=='__main__':unittest.main()
