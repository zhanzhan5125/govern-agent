"""Offline integration checks; no application database or model is touched."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from govern.memory.memory import MemoryManager
from govern.memory.context_manager import ContextManager, estimate_tokens
from govern.memory.pi_context import compact_messages
from govern.runtime.artifacts import ArtifactStore
from govern.storage.store import TaskStore
from govern.review.workbench import GovernWorkbench
from govern.policy.failure_diagnosis import feedback_target
from govern.policy.feedback_evolution import GovernCandidatePolicy
from govern.core.models import TraceEvent, TaskState
from govern.storage.store import utc_now
from govern.review.workbench import ReportValue
import threading


class ConvergenceTests(TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=TaskStore(str(Path(tmp.name)/'test.sqlite'))
        self.store.create('task','repo',None,{'govern':True},'tenant')
        self.artifacts=ArtifactStore(self.store)
        self.manager=ContextManager(artifact_store=self.artifacts)
        self.memory=MemoryManager(self.store,artifact_store=self.artifacts)
        self.w=GovernWorkbench.__new__(GovernWorkbench)
        self.w.store=self.store;self.w.artifacts=self.artifacts
        self.w.service=SimpleNamespace(memory=self.memory,context_manager=self.manager)

    def test_memory_recall_scoped_checkpointed_and_evaluation_excluded(self):
        item=self.memory.remember('tenant','repo','semantic','rule','saved answer must survive reload')
        self.memory.remember('other','repo','semantic','rule','saved answer other tenant')
        self.memory.remember('tenant','other','semantic','rule','saved answer other repo')
        row=self.store.get('task','tenant');snapshot={'diff':'saved answer','head_files':{'answer.py':'saved'}}
        catalog=self.w.review_memories('task','tenant',row,snapshot,{})
        self.assertEqual([item['id']],[m['memory_id'] for m in catalog['items']])
        reused=self.w.review_memories('task','tenant',row,snapshot,self.store.load_checkpoints('task'))
        self.assertEqual(catalog,reused)
        self.assertEqual({},self.w.review_memories('task','tenant',{**row,'input':{'evaluation':True}},snapshot,{}))

    def test_python_compression_preserves_pairs_refs_and_full_history(self):
        policy={**self.w.context_policy(),'task_id':'task','tenant_id':'tenant'}
        initial=[{'role':'system','content':'policy'},{'role':'user','content':'frozen Slice'}]
        messages=list(initial)
        for i in range(12):
            messages.extend([{'role':'assistant','content':[{'type':'text','text':'pending hypothesis. '*300},{'type':'toolCall','id':str(i)}]},
                {'role':'toolResult','toolCallId':str(i),'toolName':'read_file','content':[{'type':'text','text':json.dumps({'artifact_ref':'artifact://original','content':'saved answer '*2000})}]}])
        result=compact_messages(self.manager,messages,policy,500)
        self.assertEqual(initial,result['messages'][:2]);self.assertGreater(result['stats']['messages_folded'],0)
        self.assertLessEqual(result['stats']['estimated_input_tokens'],policy['input_tokens'])
        summary=json.loads(result['messages'][2]['content'][0]['text'])
        original=self.artifacts.get(summary['history_ref'].removeprefix('artifact://'),'tenant')
        self.assertTrue(original);self.assertIn('artifact://original',json.dumps(summary))
        for m in result['messages']:
            if m.get('role')=='toolResult':self.assertTrue(any(a.get('role')=='assistant' and '"id": "'+m['toolCallId']+'"' in json.dumps(a) for a in result['messages']))
        self.assertEqual('legacy-python-context-manager',result['stats']['backend'])
        with self.assertRaisesRegex(ValueError,'Split the Slice'):
            compact_messages(self.manager,[{'role':'user','content':'中'*22000}],policy)

    def test_failure_routes_keep_engineering_and_repo_rules_out_of_global_skill(self):
        for code,target in [('permission_denied','tool'),('budget_exhausted','workflow'),('invalid_label','dataset'),('repository_convention','memory'),('missed_behavior','prompt_skill')]:
            self.assertEqual(target,feedback_target(code))

    def test_feedback_reuses_memory_lifecycle_without_publishing_policy(self):
        self.w.lock=threading.RLock()
        finding={'finding_id':'F1','path':'answer.py','line':1,'source_stage':'correctness','title':'saved answer'}
        self.store.succeed('task',ReportValue({'findings':[finding]}),TraceEvent(1,TaskState.SUCCESS,'offline fixture',utc_now()))
        before=self.store.evolution_release_head('tenant','skill','govern-review')
        self.w.feedback('task','tenant',{'type':'false_positive','finding_id':'F1','reason':'saved answer is intentionally retained','reason_code':'repository_convention'})
        recalled=self.memory.recall('tenant','repo','saved answer')
        self.assertTrue(recalled)
        self.assertEqual('VERIFIED',recalled[0]['status'])
        self.assertEqual('correctness',recalled[0]['metadata']['finding']['source_stage'])
        from govern.policy.feedback_pool import pending_groups
        groups,pool=pending_groups(self.store,'tenant',['correctness'],[],'baseline','source')
        self.assertEqual({},dict(groups));self.assertEqual(1,pool['diagnosis_routes']['memory'])
        self.assertEqual(before,self.store.evolution_release_head('tenant','skill','govern-review'))

    def test_unlabelled_business_metrics_are_not_applicable_not_fabricated(self):
        # Test the business-gate contribution independently of the unchanged
        # legacy cost, telemetry and quality checks.
        values={'p01_total':0,'precision':.8,'recall':.7,'sandbox_duration_ms':0,'verification_timeout_rate':0}
        before={**values,'cases':4,'f1':.7,'high_severity_recall':.8,'clean_accuracy':1,'evidence_accuracy':1,'success_rate':1,
            'total_tokens':100,'cost_usd':1,'duration_ms':10,'tool_calls':1,'permission_violations':0,'budget_violations':0,'invalid_findings':0,'required_gate_misses':0,'telemetry_complete':True}
        report=GovernCandidatePolicy().compare(before,{**before,'recall':.8,'f1':.8})
        self.assertTrue(report['passed'],report);self.assertIn('p01_recall',report['not_applicable'])
        report=GovernCandidatePolicy().compare({**before,'p01_total':2,'p01_recall':1},{**before,'p01_total':2,'p01_recall':.5,'recall':.8,'f1':.8})
        self.assertFalse(report['passed']);self.assertFalse(report['gates']['p01_recall_non_regression'])
