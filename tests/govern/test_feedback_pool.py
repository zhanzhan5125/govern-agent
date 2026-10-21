import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from govern.storage.store import TaskStore
from govern.policy.feedback_pool import pending_groups, bounded_examples


class FeedbackPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=TaskStore(str(Path(self.temp.name)/'pool.sqlite'))
        self.links=[]
        self.patcher=patch.object(self.store,'evolution_feedback_links',lambda tenant:self.links)
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def feedback(self,task='run',fid='fb1',kind='false_positive',reason='Incorrect condition',repo='live',tenant='default'):
        if self.store.get(task,tenant) is None:
            self.store.create(task,repo,None,{'govern':True},tenant_id=tenant)
            with self.store._connect() as c:c.execute("UPDATE tasks SET state='SUCCESS' WHERE id=?",(task,))
        history=self.store.load_checkpoints(task).get('govern-feedback',{}).get('state',{}).get('history',[])
        f={'feedback_id':fid,'type':kind,'source_stage':'correctness','reason_code':'insufficient_evidence',
           'reason':reason,'finding':{'finding_id':'F1','source_stage':'correctness','path':'a.py','line':1,'description':'d'*4000}}
        history.append(f);self.store.save_checkpoint(task,'govern-feedback',{'history':history},'completed')
        self.store.record_failure_case(task,kind,{'feedback_id':fid,'finding':f['finding']})
        return f

    def groups(self,protected=()):
        return pending_groups(self.store,'default',{'correctness'},protected,'base','source')[0]

    def test_latest_feedback_wins_and_history_remains(self):
        self.feedback();self.feedback(fid='fb2',kind='accepted')
        self.assertFalse(self.groups())
        self.assertEqual(2,len(list(self.store.iter_feedback_failures('default',1))))
        self.feedback(fid='fb3',reason='Revised failure')
        examples=next(iter(self.groups().values()))
        self.assertEqual(['fb3'],[e['feedback_id'] for e in examples])
        self.assertLess(len(examples[0]['finding']['description']),1300)

    def test_candidate_states_control_reuse_not_history_deletion(self):
        self.feedback()
        link={'feedback_ids':['fb1'],'baseline_content_sha256':'base','policy_source_sha256':'source'}
        for status in ['STATIC_PASSED','ACTIVE','SUPERSEDED']:
            self.links=[{**link,'status':status}];self.assertFalse(self.groups())
        for status in ['REJECTED','ROLLED_BACK']:
            self.links=[{**link,'status':status}];self.assertTrue(self.groups())
        self.links=[{**link,'status':'STATIC_PASSED','baseline_content_sha256':'stale'}]
        self.assertTrue(self.groups())
        self.assertEqual(1,len(list(self.store.iter_feedback_failures('default'))))

    def test_identical_feedback_revision_does_not_retrigger(self):
        self.feedback();example=next(iter(self.groups().values()))[0]
        self.links=[{'status':'ACTIVE','feedback_ids':['fb1'],'case_fingerprints':[example['case_fingerprint']]}]
        self.feedback(fid='duplicate')
        self.assertFalse(self.groups())
        self.feedback(fid='new-reason',reason='Different verified cause')
        self.assertTrue(self.groups())

    def test_keyset_crosses_200_records_and_is_tenant_scoped(self):
        self.feedback(task='old')
        for i in range(205):self.feedback(task='noise-'+str(i),fid='n'+str(i),kind='accepted')
        self.feedback(task='other',fid='private',tenant='another')
        examples=next(iter(self.groups().values()))
        self.assertEqual(['fb1'],[e['feedback_id'] for e in examples])

    def test_protected_repository_and_diff_are_excluded(self):
        self.feedback()
        self.assertFalse(self.groups([{'repository':'live','diff':'different'}]))
        payload=self.store.get_task_payload('run') or ''
        self.assertFalse(self.groups([{'repository':'benchmark','diff':payload}]))

    def test_sampling_is_bounded_and_repository_diverse(self):
        examples=[{'repository':'a','feedback_id':str(i),'reason':'x'*1200} for i in range(30)]
        examples.append({'repository':'b','feedback_id':'b','reason':'y'})
        selected=bounded_examples(examples,max_cases=10,max_bytes=5000)
        self.assertLessEqual(len(selected),10)
        self.assertIn('b',[e['repository'] for e in selected])
        self.assertLessEqual(len(json.dumps(selected,ensure_ascii=False).encode()),5000)


if __name__=='__main__':unittest.main()
