import tempfile
from pathlib import Path
import unittest
from govern.storage.store import TaskStore
from govern.policy.candidate_policy import CandidateConflict
from tests.govern.test_local_evolution_fixture import fixture

class DefaultReleaseTests(unittest.TestCase):
    def test_policy_center_skills_follow_active_release_and_rollback_not_latest_candidate(self):
        from govern.review.workbench import GovernWorkbench
        from govern.policy.bundle import compile_bundle,applicable_overrides
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'state.sqlite';result=fixture.seed(db);store=TaskStore(str(db))
            w=GovernWorkbench.__new__(GovernWorkbench);w.store=store;w.config={}
            initial=w.active_policy('default')
            self.assertTrue(initial['active_release']['is_default'])
            default_id=initial['active_release']['candidate_id']
            candidate=store.get_evolution_candidate(result['candidates'][0]['candidate_id'],'default')
            store.publish_evolution_candidate(candidate['id'],'default',candidate['revision'],
                initial['active_release']['generation'],'tester','isolated rollback test',rollback=True)
            active=w.active_policy('default')
            self.assertEqual(candidate['id'],active['active_release']['candidate_id'])
            self.assertFalse(active['active_release']['is_default'])
            expected=compile_bundle(applicable_overrides(candidate,compile_bundle()))
            self.assertEqual(expected['effective_skills'],active['effective_skills'])
            self.assertEqual(expected['sha256'],active['sha256'])
            self.assertNotEqual(initial['effective_skills'],active['effective_skills'])
            import copy
            pending=copy.deepcopy(candidate['content'])
            pending['files']['stages/validator.md']='UNPUBLISHED DRAFT MUST NOT BE DISPLAYED'
            draft=store.create_evolution_candidate('default','skill','govern-review',pending,
                {'content':candidate['content']},candidate['policy'],{'source':'test-pending'},'tester')
            self.assertEqual('DRAFT',draft['status'])
            self.assertEqual(active['effective_skills'],w.active_policy('default')['effective_skills'])
            store.restore_govern_default('default',active['active_release']['generation'],'tester')
            restored=w.active_policy('default')
            self.assertEqual(default_id,restored['active_release']['candidate_id'])
            self.assertEqual(initial['effective_skills'],restored['effective_skills'])

    def test_default_is_stable_restore_is_cas_protected_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'state.sqlite';result=fixture.seed(db);store=TaskStore(str(db))
            default=store.govern_default_release('default');self.assertTrue(default['is_active'])
            c=store.get_evolution_candidate(result['candidates'][0]['candidate_id'],'default')
            head=store.evolution_release_head('default','skill','govern-review')
            active=store.publish_evolution_candidate(c['id'],'default',c['revision'],head['generation'],'tester','restore test release',rollback=True)
            self.assertEqual(default['candidate_id'],store.govern_default_release('default')['candidate_id'])
            with self.assertRaises(CandidateConflict):store.restore_govern_default('default',head['generation'],'tester')
            generation=active['reports']['release']['generation']
            restored=store.restore_govern_default('default',generation,'tester')
            self.assertTrue(restored['changed']);self.assertTrue(restored['default']['is_active'])
            self.assertFalse(store.restore_govern_default('default',generation+1,'tester')['changed'])
            self.assertIsNone(store.govern_default_release('another')['candidate_id'])
            self.assertTrue(store.govern_default_release('another')['is_active'])
