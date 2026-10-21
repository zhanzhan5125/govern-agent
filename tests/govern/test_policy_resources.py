import copy
from pathlib import Path
import shutil
import tempfile
import json
import threading
import urllib.request
import urllib.error
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from govern.core.config import Settings
from govern.policy.bundle import compile_bundle, policy_root, save_manual_rule, select_fragments, ROLES
from govern.policy.candidate_policy import CandidateConflict
from govern.policy.feedback_evolution import GovernEvolution, skill
from govern.storage.store import TaskStore


class PolicyResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'policy'
        shutil.copytree(policy_root(), self.root)

    def test_separate_sources_and_role_specific_contracts(self):
        b = compile_bundle(root=self.root)
        self.assertEqual(set(ROLES), set(b['stages']))
        self.assertNotIn(b['risk_policy'], b['stages']['mapper'])
        self.assertNotIn(b['output_contract'], b['stages']['mapper'])
        for role in ROLES:
            self.assertIn(b['common_rules'], b['stages'][role])
            self.assertIn(b['stage_sources'][role], b['stages'][role])
            if role != 'mapper': self.assertIn(b['risk_policy'], b['stages'][role])

    def test_manual_change_updates_new_bundle_not_frozen_copy(self):
        before = compile_bundle(root=self.root)
        after = save_manual_rule('risk', before['risk_policy']+'\nOperator clarification.\n', before['source_sha256'], self.root)
        self.assertNotEqual(before['source_sha256'], after['source_sha256'])
        self.assertNotIn('Operator clarification', before['stages']['security'])
        self.assertIn('Operator clarification', after['stages']['security'])
        self.assertEqual(before['stage_sources'], after['stage_sources'])
        with self.assertRaises(CandidateConflict):
            save_manual_rule('common', 'stale edit', before['source_sha256'], self.root)

    def test_manual_edit_is_restricted_and_validated(self):
        b = compile_bundle(root=self.root)
        for section, text in [('stages/security', 'edit'), ('../manifest', 'edit'), ('common', ''), ('risk', '\x00')]:
            with self.subTest(section=section), self.assertRaises(ValueError):
                save_manual_rule(section, text, b['source_sha256'], self.root)

    def test_fragments_are_additive_and_role_scoped(self):
        b = compile_bundle(root=self.root)
        unit = select_fragments({'path':'migrations/002.sql','context':'ALTER TABLE orders ADD COLUMN state TEXT'}, b)
        self.assertIn('database', unit['policy_fragments']['correctness'])
        self.assertIn('migration', unit['policy_fragments']['contract_test'])
        self.assertEqual([], unit['policy_fragments']['security'])
        self.assertEqual('lexical', unit['fragment_selection_reasons']['database']['precision'])
        self.assertNotIn('Database specialist', b['stages']['correctness'])
        plain = select_fragments({'path':'docs/readme.md','context':'ordinary prose'}, b)
        self.assertTrue(all(not ids for ids in plain['policy_fragments'].values()))

    def test_stage_supplement_does_not_modify_protected_rules(self):
        base = compile_bundle(root=self.root)
        changed = compile_bundle({'security':'Check tenant ownership.'}, root=self.root)
        self.assertEqual(base['source_sha256'], changed['source_sha256'])
        self.assertNotEqual(base['sha256'], changed['sha256'])
        for role in ROLES:
            self.assertEqual(base['stages'][role] == changed['stages'][role], role != 'security')

    def pipeline(self):
        settings = Settings(host='127.0.0.1',port=0,db_path=str(Path(self.temp.name)/'state.db'),
            max_diff_bytes=10000,max_steps=8,timeout_seconds=10,llm_base_url='',llm_api_key='',llm_model='',
            github_webhook_secret='',github_token='',auto_post_review=False)
        w = SimpleNamespace(store=TaskStore(settings.db_path),config={},
            service=SimpleNamespace(settings=settings,llm_config={}), verification=SimpleNamespace(profiles={}))
        return GovernEvolution(w)

    def test_candidate_may_change_only_one_attributed_stage(self):
        p = self.pipeline()
        candidate = p.propose('default','skill','govern-review',skill({'security':'Check tenant ownership.'}),'tester',{'stage':'security'})
        self.assertEqual('security', candidate['lineage']['stage'])
        for content, lineage in [(skill({}),{}),(skill({'security':'A','mapper':'B'}),{}),
                                  (skill({'security':'A'}),{'stage':'correctness'})]:
            with self.assertRaises(ValueError): p.propose('default','skill','govern-review',content,'tester',lineage)

    def test_candidate_cannot_edit_protected_files_or_entrypoint(self):
        p = self.pipeline()
        for path in ['common/evidence.md','common/risk.md','manifest.json','fragments/database.md','SKILL.md']:
            content = skill({'security':'Check ownership.'}); content['files'][path]='unauthorized change'
            with self.subTest(path=path), self.assertRaises(ValueError):
                p.propose('default','skill','govern-review',content,'tester')

    def test_manual_rule_change_invalidates_candidate(self):
        p = self.pipeline()
        with patch('govern.policy.bundle.policy_root', return_value=self.root):
            candidate=p.propose('default','skill','govern-review',skill({'security':'Check ownership.'}),'tester')
            b=compile_bundle()
            save_manual_rule('common',b['common_rules']+'\nHuman rule.\n',b['source_sha256'])
            with self.assertRaises(CandidateConflict):p._get(candidate['id'],'default')

    def test_generic_pipeline_cannot_bypass_stage_scope(self):
        from govern.policy.evolution_pipeline import EvolutionPipeline
        p=self.pipeline()
        generic=EvolutionPipeline(p.store,p.dataset)
        with self.assertRaisesRegex(ValueError,'stage-scoped'):
            generic.propose('default','skill','govern-review',skill({'security':'A','mapper':'B'}),'tester')

    def test_candidate_rejects_stale_generation_baseline(self):
        p=self.pipeline()
        with self.assertRaises(CandidateConflict):
            p.propose('default','skill','govern-review',skill({'security':'Check ownership.'}),'tester',{'baseline_content_sha256':'old'})

    def test_manual_http_is_local_concurrent_and_audited(self):
        from govern.api.api import ApiHandler
        from http.server import ThreadingHTTPServer
        store=TaskStore(str(Path(self.temp.name)/'api.sqlite'))
        service=SimpleNamespace(store=store,govern=SimpleNamespace(lock=threading.RLock(),active_policy=lambda _:compile_bundle(root=self.root)))
        settings=SimpleNamespace(auth_required=False,default_tenant_id='default',max_diff_bytes=1024*1024)
        handler=type('PolicyHandler',(ApiHandler,),{'service':service,'settings':settings})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        address='http://127.0.0.1:'+str(server.server_port)+'/v1/govern/policy/manual'
        before=compile_bundle(root=self.root)
        payload=json.dumps({'section':'common','content':before['common_rules']+'\nHuman amendment.\n','expected_source_sha256':before['source_sha256']}).encode()
        def request(origin=None):
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            return urllib.request.urlopen(urllib.request.Request(address,data=payload,headers=headers))
        try:
            with patch('govern.policy.bundle.policy_root',return_value=self.root):
                with request() as response:self.assertEqual(200,response.status)
                with self.assertRaises(urllib.error.HTTPError) as stale:request()
                self.assertEqual(409,stale.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as cross:request('https://untrusted.example')
                self.assertEqual(403,cross.exception.code)
            self.assertEqual('policy.manual',store.list_audit('default')[0]['action'])
        finally:server.shutdown();server.server_close();thread.join()
