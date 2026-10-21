import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from govern.storage.store import TaskStore
from govern.policy.feedback_evolution import skill
from govern.policy.auto_evolution import AutomaticEvolution, validate


class AutomaticEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        store=TaskStore(str(Path(self.temp.name)/'auto.sqlite'))
        pipeline=SimpleNamespace(dataset=SimpleNamespace(cases=lambda p:[]),
            _baseline=lambda *a:{'content':skill({})},generation_lock=threading.Lock(),
            generate=Mock(return_value={'candidates':[{'id':'C1','revision':2}]}),
            evaluate=Mock(return_value={'status':'HOLDOUT_PASSED'}),approve=Mock(),activate=Mock())
        self.w=SimpleNamespace(store=store,config={'auto_evolution':{'enabled':True,'threshold':2,'cooldown_minutes':1}},evolution=pipeline)
        self.allowed=False;self.now=1000
        self.auto=AutomaticEvolution(self.w,allowed=lambda:self.allowed,clock=lambda:self.now)

    def add_feedback(self,name,stage='security',kind='false_positive',tenant='default'):
        store=self.w.store;store.create(name,'live-'+name,None,{'govern':True},tenant_id=tenant)
        with store._connect() as conn:conn.execute("UPDATE tasks SET state='SUCCESS' WHERE id=?",(name,))
        f={'feedback_id':name,'type':kind,'source_stage':stage,'reason_code':'insufficient_evidence',
           'reason':'Confirmed wrong assumption','finding':{'finding_id':'F-'+name,'source_stage':stage,'path':'a.py','line':1}}
        store.save_checkpoint(name,'govern-feedback',{'history':[f]},'completed')
        store.record_failure_case(name,kind,{'finding':f['finding'],'feedback_id':name})

    def test_threshold_and_authorization_are_independent(self):
        self.add_feedback('one');self.assertEqual('below_threshold',self.auto.check('default')['status'])
        self.add_feedback('two');self.assertEqual('waiting_authorization',self.auto.check('default')['status'])
        self.w.evolution.generate.assert_not_called();self.w.evolution.evaluate.assert_not_called()
        self.assertFalse(self.auto.status('default')['model_calls_authorized'])
        self.allowed=True
        state=self.auto.check('default')
        self.assertEqual('awaiting_human_review',state['status'])
        self.w.evolution.generate.assert_called_once_with('default','auto-feedback-trigger',stages=['security'])
        self.w.evolution.evaluate.assert_called_once_with('C1','default',2)
        self.w.evolution.approve.assert_not_called();self.w.evolution.activate.assert_not_called()

    def test_cooldown_persists_across_controller_restart(self):
        self.add_feedback('one');self.add_feedback('two');self.allowed=True
        self.auto.check('default');self.now+=5
        restarted=AutomaticEvolution(self.w,allowed=lambda:True,clock=lambda:self.now)
        self.assertEqual('cooldown',restarted.check('default')['status'])
        self.assertEqual(1,self.w.evolution.generate.call_count)
        self.now+=60;self.assertEqual('awaiting_human_review',restarted.check('default')['status'])

    def test_failure_is_not_retried_in_tight_loop(self):
        self.add_feedback('one');self.add_feedback('two');self.allowed=True
        self.w.evolution.generate.side_effect=RuntimeError('secret internal error')
        state=self.auto.check('default');self.assertEqual('failed',state['status'])
        self.assertNotIn('secret',str(state));self.assertEqual('cooldown',self.auto.check('default')['status'])
        self.assertEqual(1,self.w.evolution.generate.call_count)

    def test_counts_are_stage_and_tenant_scoped_and_accepted_is_excluded(self):
        self.add_feedback('one');self.add_feedback('two','correctness')
        self.add_feedback('accepted',kind='accepted');self.add_feedback('other',tenant='another')
        state=self.auto.check('default');self.assertEqual('below_threshold',state['status'])
        self.assertEqual({'security':1,'correctness':1},state['pending_by_stage'])

    def test_disabled_does_not_generate(self):
        self.w.config['auto_evolution']['enabled']=False;self.allowed=True
        self.assertEqual('disabled',self.auto.check('default')['status'])
        self.w.evolution.generate.assert_not_called()

    def test_evaluation_failure_is_not_presented_as_ready_for_approval(self):
        self.add_feedback('one');self.add_feedback('two');self.allowed=True
        self.w.evolution.evaluate.side_effect=ValueError('Dataset unavailable')
        result=self.auto.check('default')
        self.assertEqual('evaluation_error',result['status'])
        self.assertEqual('ValueError',result['evaluation'][0]['error_type'])
        self.w.evolution.approve.assert_not_called()

    def test_rejected_evaluation_is_not_presented_as_ready_for_approval(self):
        self.add_feedback('one');self.add_feedback('two');self.allowed=True
        self.w.evolution.evaluate.return_value={'status':'REJECTED'}
        self.assertEqual('evaluation_rejected',self.auto.check('default')['status'])

    def test_manual_in_flight_blocks_auto_and_disabling_stops_evaluation(self):
        self.add_feedback('one');self.add_feedback('two');self.allowed=True
        self.w.evolution.generation_lock.acquire()
        self.assertEqual('manual_generation_running',self.auto.check('default')['status'])
        self.w.evolution.generation_lock.release()
        def disable(*a,**k):
            self.w.config['auto_evolution']['enabled']=False
            return {'candidates':[{'id':'C1','revision':2}]}
        self.w.evolution.generate.side_effect=disable
        self.auto.check('default');self.w.evolution.evaluate.assert_not_called()

    def test_config_validation(self):
        self.assertFalse(validate({})['enabled'])
        for value in [{'threshold':True},{'threshold':0},{'threshold':101},{'enabled':'yes'},
                      {'cooldown_minutes':0},{'model_calls_authorized':True}]:
            with self.subTest(value=value),self.assertRaises(ValueError):validate(value)


if __name__=='__main__':unittest.main()
