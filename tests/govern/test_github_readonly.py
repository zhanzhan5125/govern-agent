import base64
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, Mock
import urllib.error
from govern.integrations.github_readonly import ReadOnlyGitHubClient, NoRedirect, fetch_snapshot, client_for
from govern.core.operator_settings import save_settings, public_settings, test_github as check_github
from govern.core.operator_settings import settings_request_allowed
from govern.review.workbench import GovernWorkbench
from govern.storage.store import TaskStore


TOKEN='github_test_only_NOT_A_REAL_TOKEN'


class GitHubReadOnlyTests(TestCase):
    def test_container_settings_only_trust_exact_gateway_and_local_same_origin(self):
        with patch.dict(os.environ,{'GOVERN_SETTINGS_TRUSTED_PEER':'172.19.0.1'}):
            self.assertTrue(settings_request_allowed('172.19.0.1','127.0.0.1:8080','http://127.0.0.1:8080'))
            self.assertTrue(settings_request_allowed('127.0.0.1','localhost:8080',None))
            self.assertFalse(settings_request_allowed('172.19.0.2','127.0.0.1:8080','http://127.0.0.1:8080'))
            self.assertFalse(settings_request_allowed('172.19.0.1','127.0.0.1:8080',None))
            self.assertFalse(settings_request_allowed('172.19.0.1','attacker.test','http://attacker.test'))
            self.assertFalse(settings_request_allowed('172.19.0.1','127.0.0.1:8080','http://attacker.test'))
    def test_write_foreign_host_and_redirect_are_rejected_without_network(self):
        c=ReadOnlyGitHubClient(TOKEN);c._open=Mock()
        with self.assertRaises(PermissionError):c.post_comment('https://api.github.com/repos/a/b/issues/1','never sent')
        for method,url in [('POST','https://api.github.com/repos/a/b'),('GET','https://attacker.test/'),('GET','http://api.github.com/repos/a/b')]:
            with self.assertRaises(PermissionError):c._request(method,url)
        c._open.assert_not_called()
        with self.assertRaises(urllib.error.HTTPError):NoRedirect().redirect_request(SimpleNamespace(full_url='https://api.github.com/'),None,302,'redirect',{},'https://attacker.test/')

    def test_api_error_does_not_expose_provider_body_or_token(self):
        from govern.integrations.github import GitHubClient
        c=ReadOnlyGitHubClient(TOKEN)
        with patch.object(GitHubClient,'_request',side_effect=RuntimeError('GitHub API GET returned HTTP 404: '+TOKEN)):
            with self.assertRaises(ValueError) as error:c.get_pull_request('a/b',1)
        self.assertNotIn(TOKEN,str(error.exception));self.assertIn('404',str(error.exception))

    def test_git_credentials_are_ephemeral_no_prompt_no_trace_and_errors_are_safe(self):
        with patch.dict(os.environ,{'GIT_TRACE':'1','GIT_TRACE_CURL':'1','GIT_CURL_VERBOSE':'1'}),patch('govern.integrations.github_readonly.subprocess.run') as run:
            run.return_value=SimpleNamespace(returncode=0)
            fetch_snapshot('cache','owner/private','a'*40,'b'*40,TOKEN)
            args,kw=run.call_args
            self.assertNotIn(TOKEN,repr(args));self.assertNotIn('Authorization',repr(args))
            env=kw['env'];self.assertEqual('0',env['GIT_TERMINAL_PROMPT'])
            self.assertNotIn('GIT_TRACE_CURL',env);self.assertNotIn('GIT_CURL_VERBOSE',env)
            self.assertIn(base64.b64encode(('x-access-token:'+TOKEN).encode()).decode(),repr(env))
            self.assertNotIn('--global',repr(args));self.assertNotIn('push',args[0])
            run.return_value=SimpleNamespace(returncode=1,stderr=TOKEN.encode())
            with self.assertRaises(ValueError) as error:fetch_snapshot('cache','owner/private','a'*40,'b'*40,TOKEN)
            self.assertNotIn(TOKEN,str(error.exception))

    def workbench(self,root):
        w=GovernWorkbench.__new__(GovernWorkbench)
        w.config={};w.lock=threading.RLock();w.store=TaskStore(str(root/'test.sqlite'))
        w.service=SimpleNamespace(settings=SimpleNamespace(github_token='',database_url=''),llm_config={},queue=SimpleNamespace(backend='local'))
        w.jira=SimpleNamespace(client=None);w.bridge=SimpleNamespace(command=['node'])
        w.verification=SimpleNamespace(backend=SimpleNamespace(available=lambda:False),profiles={})
        return w

    def test_settings_are_write_only_blank_preserves_clear_removes_and_bad_headers_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);w=self.workbench(root)
            with patch.dict(os.environ,{'GOVERN_CONFIG':str(root/'config.json')}),patch('govern.policy.feedback_evolution.GovernEvolution',return_value=SimpleNamespace()):
                public=save_settings(w,{'github':{'token':TOKEN}})
                self.assertTrue(public['github']['token_configured']);self.assertNotIn(TOKEN,json.dumps(public))
                self.assertEqual(TOKEN,client_for(w).token)
                save_settings(w,{'github':{'token':''}});self.assertEqual(TOKEN,client_for(w).token)
                self.assertNotIn(TOKEN,json.dumps(public_settings(w)))
                with self.assertRaises(ValueError):save_settings(w,{'github':{'token':'bad\nheader'}})
                save_settings(w,{'github':{'clear_token':True}});self.assertEqual('',client_for(w).token)
                self.assertEqual('',json.loads((root/'config.json').read_text())['github']['token'])

    def test_private_pr_uses_same_credential_for_api_and_git_and_detects_moving_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=self.workbench(Path(tmp));w.config={'github':{'token':TOKEN},'git_cache':tmp}
            c=Mock(token=TOKEN);metadata={'base':{'sha':'a'*40},'head':{'sha':'b'*40}}
            c.get_pull_request.return_value=metadata
            with patch('govern.integrations.github_readonly.client_for',return_value=c),patch('govern.integrations.github_readonly.fetch_snapshot') as fetch,patch('govern.review.workbench.git'),patch('govern.review.workbench.capture',return_value={'diff':'','base_files':{},'head_files':{}}):
                snapshot,number=w._remote('https://github.com/owner/private/pull/42')
                self.assertEqual(42,number);self.assertNotIn(TOKEN,json.dumps(snapshot))
                self.assertEqual(TOKEN,fetch.call_args.args[-1]);self.assertEqual(2,c.get_pull_request.call_count)
                c.get_pull_request.side_effect=[metadata,{'base':{'sha':'c'*40},'head':{'sha':'b'*40}}]
                with self.assertRaisesRegex(ValueError,'changed'):w._remote('https://github.com/owner/private/pull/42')

    def test_connection_check_is_read_only_metadata_not_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=self.workbench(Path(tmp));c=Mock();c.get_repository.return_value={'full_name':'owner/private','private':True}
            with patch('govern.integrations.github_readonly.client_for',return_value=c):result=check_github(w,{'repository':'owner/private'})
            self.assertTrue(result['private']);c.get_repository.assert_called_once_with('owner/private')
