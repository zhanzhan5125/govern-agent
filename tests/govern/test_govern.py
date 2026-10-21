import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import time
from govern.review.repository import capture, change_units, digest
from govern.policy.bundle import compile_bundle, render_report
from govern.runtime.verification import classify, VerificationGateway, ExecutionProfile, DockerBackend
from govern.runtime.artifacts import ArtifactStore
from govern.storage.store import TaskStore


class GovernTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.STDOUT).decode().strip()
    def repo(self):
        self.git('init','-b','main');self.git('config','user.email','test@example.com');self.git('config','user.name','Test')
        (self.root/'a.py').write_text('def pay(x):\n    return x\n')
        self.git('add','.');self.git('commit','-m','base')
    def test_scopes_capture_content_without_changing_worktree(self):
        self.repo();(self.root/'a.py').write_text('def pay(x):\n    return x+1\n');self.git('add','a.py')
        (self.root/'a.py').write_text('def pay(x):\n    return x+2\n')
        staged=capture(self.root,{'mode':'staged'});working=capture(self.root,{'mode':'working-tree'})
        self.assertIn('x+1',staged['head_files']['a.py']);self.assertIn('x+2',working['head_files']['a.py'])
        self.assertIsNone(staged['head_sha']);self.assertNotEqual(staged['snapshot_id'],working['snapshot_id'])
        self.assertEqual('pay',change_units(staged)[0]['symbol']['name'])
        self.assertIn('x+2',(self.root/'a.py').read_text())
    def test_deletion_and_untracked_have_units(self):
        self.repo();(self.root/'a.py').unlink();(self.root/'new.py').write_text('x=1\n')
        snapshot=capture(self.root,{'mode':'working-tree','include_untracked':True})
        self.assertEqual({'a.py','new.py'},{u['path'] for u in change_units(snapshot)})
    def test_committed_branch_and_range_use_immutable_shas(self):
        self.repo();base=self.git('rev-parse','HEAD');self.git('checkout','-b','feature')
        (self.root/'a.py').write_text('def pay(x):\n    return x+3\n');self.git('add','.');self.git('commit','-m','feature')
        head=self.git('rev-parse','HEAD')
        for mode in ('branch','range'):
            snapshot=capture(self.root,{'mode':mode,'base':'main','head':'feature'})
            self.assertEqual(base,snapshot['base_sha']);self.assertEqual(head,snapshot['head_sha'])
        self.assertEqual('feature',self.git('branch','--show-current'))
    def test_rename_without_changed_source_lines_stays_in_coverage(self):
        self.repo();self.git('mv','a.py','renamed.py')
        units=change_units(capture(self.root,{'mode':'staged'}))
        self.assertIn('renamed.py',{u['path'] for u in units})
        self.assertEqual('file_metadata',units[0]['symbol']['kind'])
    def test_credential_files_are_neither_frozen_nor_sent_in_diff(self):
        self.repo();(self.root/'.env').write_text('SECRET=OLD_PRIVATE\n');self.git('add','.env');self.git('commit','-m','credential fixture')
        (self.root/'.env').write_text('SECRET=NEW_PRIVATE\n');(self.root/'a.py').write_text('def pay(x):\n    return x+1\n')
        snapshot=capture(self.root,{'mode':'working-tree'})
        self.assertNotIn('.env',snapshot['head_files']);self.assertNotIn('.env',snapshot['base_files'])
        self.assertNotIn('PRIVATE',snapshot['diff']);self.assertIn('.env',snapshot['omitted_files'])
    def test_jira_pages_and_freezes_criteria_without_agent_issue_exploration(self):
        from govern.integrations.jira_mcp import JiraAdapter
        calls=[]
        class Client:
            def call(self,name,args):
                calls.append((name,args))
                if name=='get':return {'key':args['issue_key'],'fields':{'summary':'Payment','description':'Rules','acceptance_criteria':['Reject negative values']}}
                return {'issues':[{'key':'PAY-2'}],'total':1}
        adapter=JiraAdapter({'tools':{'get_issue':{'name':'get'},'list_requirements':{'name':'children'}}},Client())
        catalog=adapter.requirements('PAY-1')
        self.assertEqual({'PAY-1','PAY-2'},{r['requirement_id'] for r in catalog['requirements']})
        self.assertEqual('PAY-2:AC-1',catalog['requirements'][1]['acceptance_criteria'][0]['criterion_id'])
        self.assertTrue(catalog['complete']);self.assertEqual(3,len(calls))
    def test_report_always_includes_eight_required_fields(self):
        f={'title':'issue','path':'a.py','line':2,'severity':'high','business_priority':'P1','finding_id':'f1'}
        report=render_report('r1',{'findings':[f]})
        for field in compile_bundle()['report_fields']:self.assertIn(field,report)
        self.assertIn('未提供／未验证',report)
    def test_actual_changed_lines_not_hunk_context_anchor_and_large_symbol(self):
        self.repo()
        original='def before():\n    return 1\n\ndef changed():\n'+''.join('    x = %d\n'%i for i in range(260))+'    return x\n'
        (self.root/'a.py').write_text(original);self.git('add','.');self.git('commit','-m','large function')
        (self.root/'a.py').write_text(original.replace('x = 0\n','x = -1\n').replace('x = 259\n','x = -2\n'))
        snap=capture(self.root,{'mode':'working-tree'});units=change_units(snap)
        self.assertTrue(all(u['symbol']['name']=='changed' for u in units))
        head=[u for u in units if u['side']=='head']
        self.assertEqual(2,len(head));self.assertEqual({5,264},{n for u in head for n in u['changed_lines']})
        self.assertEqual([u['change_unit_id'] for u in units],[u['change_unit_id'] for u in change_units(snap)])
    def test_empty_or_environment_failure_is_not_passing_test(self):
        self.assertEqual('not_collected',classify({'exit_code':0,'collected':0}))
        self.assertEqual('not_run',classify({'exit_code':1,'collected':1,'errors':1}))
        self.assertEqual('failed',classify({'exit_code':1,'collected':1,'failures':1}))
        self.assertEqual('not_collected',classify({'exit_code':0,'collected':2,'skipped':2}))
    def test_gateway_ownership_role_and_unavailable(self):
        store=TaskStore(str(self.root/'db.sqlite'));store.create('t','repo',None,{})
        artifacts=ArtifactStore(store);snapshot={'snapshot_id':'s','base_files':{},'head_files':{}}
        ref=artifacts.put('t','default','code-snapshot',snapshot).uri
        plan=artifacts.put('t','default','verification-plan',{'hypothesis':'h','assertion_source':'req','target_symbols':['pay'],'selectors':['.govern-tests/test_pay.py']}).uri
        bundle=artifacts.put('t','default','verification-tests',{'test_pay.py':'assert True'}).uri
        class Missing:
            def available(self):return False
            def execute(self,*a):raise AssertionError('host fallback')
        gateway=VerificationGateway(artifacts,{'python-pytest':ExecutionProfile('x@sha256:'+'0'*64)},Missing())
        args={'finding_id':'f','snapshot_id':'s','test_plan_ref':plan,'test_bundle_ref':bundle,'execution_profile':'python-pytest','compare':'head'}
        with self.assertRaises(PermissionError):gateway.run('t','default','security',ref,args)
        self.assertEqual('unavailable',gateway.run('t','default','validator',ref,args)['execution_status'])
        with self.assertRaises(PermissionError):gateway.run('other','default','validator',ref,args)
        bad=artifacts.put('t','default','verification-plan',{'hypothesis':'h','assertion_source':'req','target_symbols':['pay'],'selectors':['a'],'mocked_symbols':['pay']}).uri
        with self.assertRaises(ValueError):gateway.run('t','default','validator',ref,{**args,'test_plan_ref':bad})
    def test_service_report_feedback_and_govern_candidate_reuse(self):
        from govern.core.config import Settings
        from govern.api.service import ReviewService
        from govern.policy.feedback_evolution import skill
        self.repo();(self.root/'a.py').write_text('def pay(x):\n    return x+1\n')
        settings=Settings(host='127.0.0.1',port=0,db_path=str(self.root/'test.db'),max_diff_bytes=100000,max_steps=8,
            timeout_seconds=10,llm_base_url='http://127.0.0.1:9/v1',llm_api_key='test-only',llm_model='test',
            github_webhook_secret='',github_token='',auto_post_review=False,skills_dir=str(self.root/'no-skills'))
        service=ReviewService(settings)
        class Bridge:
            def run(self,request,tool,event,**kwargs):
                return {'findings':[{'finding_id':'f1','title':'Changed result','path':'a.py','line':2,'side':'head',
                    'source_stage':'correctness','severity':'medium','business_priority':None,'source_module':'payments',
                    'related_tests':[],'description':'Result changed','impact_scope':'pay callers','recommendation':'Check contract',
                    'evidence_refs':['selected-diff'],'runtime_verdict':'not_applicable'}]}
        service.govern.bridge=Bridge()
        try:
            with self.assertRaises(PermissionError):service.govern.create({'source':{'path':str(self.root)},'scope':{'mode':'working-tree'}},'default')
            self.assertEqual([],service.store.list_tasks())
            run=service.govern.create({'source':{'path':str(self.root)},'scope':{'mode':'working-tree'},'verification_enabled':False,'allow_external_model':True},'default')
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                row=service.store.get(run['task_id'],'default')
                if row['state'] in {'SUCCESS','FAILED','CANCELLED'}:break
                time.sleep(.02)
            self.assertEqual('SUCCESS',row['state'],row.get('error'))
            for field in compile_bundle()['report_fields']:self.assertIn(field,row['report']['report_markdown'])
            record=service.govern.feedback(run['task_id'],'default',{'type':'false_positive','finding_id':'f1','reason':'Expected by contract'})
            self.assertEqual('correctness',record['source_stage'])
            missed=service.govern.feedback(run['task_id'],'default',{'type':'missed_issue','reason':'Check privilege',
                'finding':{'path':'a.py','line':2,'source_stage':'security'}})
            self.assertEqual('security',missed['source_stage']);self.assertTrue(missed['finding']['finding_id'])
            candidate=service.govern.evolution.propose('default','skill','govern-review',skill({'correctness':'Check explicit function contracts and frozen evidence.'}),'tester')
            self.assertEqual('STATIC_PASSED',candidate['status'])
            from govern.api.api import ApiHandler
            from http.server import ThreadingHTTPServer
            import threading
            import urllib.request
            handler=type('GovernTestHandler',(ApiHandler,),{'service':service,'settings':settings})
            server=ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:%d'%server.server_address[1]
            try:
                for path in ('/','/assets/govern.js','/assets/govern.css','/v1/govern/policy','/v1/govern/candidates'):
                    with urllib.request.urlopen(base+path,timeout=3) as response:self.assertEqual(200,response.status)
                with urllib.request.urlopen(base+'/v1/tasks/'+run['task_id']+'/report',timeout=3) as response:
                    document=response.read().decode()
                    for field in compile_bundle()['report_fields']:self.assertIn(field,document)
                before=len(service.store.list_tasks())
                request=urllib.request.Request(base+'/v1/govern/preview',data=json.dumps({'path':str(self.root),'scope':{'mode':'working-tree','paths':['a.py']}}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=5) as response:
                    preview=json.load(response)
                    self.assertTrue(preview['local_only']);self.assertFalse(preview['creates_task']);self.assertIn('return x+1',preview['diff'])
                self.assertEqual(before,len(service.store.list_tasks()))
                import urllib.error
                request=urllib.request.Request(base+'/v1/govern/reviews',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(request,timeout=3)
                self.assertEqual(403,failure.exception.code)
            finally:server.shutdown();thread.join(timeout=3);server.server_close()
        finally:service.queue.close()
    def test_release_gate_strict_five_points_and_sandbox_guardrails(self):
        from govern.policy.feedback_evolution import GovernCandidatePolicy
        from tests.evolution.test_evolution_governance import metrics
        policy=GovernCandidatePolicy()
        baseline=metrics(precision=.8,recall=.75,p01_total=4,p01_recall=.75,sandbox_duration_ms=100,verification_timeout_rate=0.)
        candidate={**baseline,'recall':.8,'f1':.82}
        self.assertFalse(policy.compare(baseline,candidate)['passed'])
        candidate['recall']=.81
        self.assertTrue(policy.compare(baseline,candidate)['passed'])
        self.assertFalse(policy.compare(baseline,{**candidate,'sandbox_duration_ms':121})['passed'])
        self.assertFalse(policy.compare(baseline,{**candidate,'verification_timeout_rate':.1})['passed'])
        self.assertFalse(policy.compare(baseline,{**candidate,'p01_recall':.7})['passed'])
        self.assertFalse(policy.compare(baseline,{**candidate,'p01_total':0,'p01_recall':None})['passed'])
    def test_docker_command_is_confined_and_uses_only_materialized_inputs(self):
        from unittest.mock import patch
        calls=[]
        class Process:
            returncode=0
            def __init__(self,command,stdout,**kwargs):
                calls.append(command);stdout.write(b'GOVERN_RESULT={"exit_code":0,"collected":1}\n');stdout.flush()
            def poll(self):return 0
            def wait(self,**kwargs):return 0
        with patch('govern.runtime.verification.subprocess.Popen',Process), patch('govern.runtime.verification.subprocess.run'):
            result=DockerBackend().execute({'a.py':'def pay(): return 1'},{'test_pay.py':'from a import pay\ndef test_pay(): assert pay()==1'},['.govern-tests/test_pay.py'],ExecutionProfile('registry/python@sha256:'+'a'*64))
        self.assertEqual('passed',result['test_status'])
        command=calls[0]
        for flag in ('--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--pull=never'):
            self.assertIn(flag,command)
        self.assertNotIn(str(self.root),' '.join(command));self.assertNotIn('docker.sock',' '.join(command))

if __name__=='__main__':unittest.main()
