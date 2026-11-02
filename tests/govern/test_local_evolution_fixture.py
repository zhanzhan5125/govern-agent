import importlib.util
from pathlib import Path
import tempfile
import unittest
from govern.storage.store import TaskStore

path=Path(__file__).resolve().parents[2]/'scripts/evaluation/seed_local_evolution.py'
spec=importlib.util.spec_from_file_location('local_fixture',path)
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class LocalFixtureTests(unittest.TestCase):
    def test_reset_seeds_one_bundle_and_preserves_feedback(self):
        reset_path=path.with_name('reset_local_versions.py')
        reset_spec=importlib.util.spec_from_file_location('reset_fixture',reset_path)
        reset=importlib.util.module_from_spec(reset_spec);reset_spec.loader.exec_module(reset)
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'test.sqlite';fixture.seed(db);store=TaskStore(str(db))
            ids=[x['id'] for x in store.list_evolution_candidates('default')]
            result=reset.reset_and_seed(db,ids)
            records=store.list_evolution_candidates('default')
            self.assertEqual(1,len(records));c=records[0]
            self.assertEqual('HUMAN_APPROVED',c['status'])
            self.assertEqual(3,len(c['lineage']['stages']))
            self.assertEqual(3,len(c['lineage']['feedback_context']))
            active=store.publish_evolution_candidate(c['id'],'default',c['revision'],0,'tester','human confirmation after Validation/Holdout')
            self.assertEqual('ACTIVE',active['status'])
            previous=store.get_evolution_candidate(active['reports']['release']['previous_candidate_id'],'default')
            store.publish_evolution_candidate(previous['id'],'default',previous['revision'],1,'tester','restore',rollback=True)
            self.assertEqual('ROLLED_BACK',store.get_evolution_candidate(c['id'],'default')['status'])

    def test_full_lifecycle_restores_baseline_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'test.sqlite'
            result=fixture.seed(db)
            self.assertEqual(0,result['model_calls'])
            self.assertEqual(3,len(result['candidates']))
            store=TaskStore(str(db))
            for c in result['candidates']:
                item=store.get_evolution_candidate(c['candidate_id'],'default')
                self.assertEqual('ROLLED_BACK',item['status'])
                self.assertEqual(fixture.SOURCE,item['lineage']['source'])
                self.assertNotIn('SHADOW',item['reports'])
                self.assertGreater(item['reports']['VALIDATION_PASSED']['candidate']['recall'],item['reports']['VALIDATION_PASSED']['baseline']['recall'])
                targets=[x['target'] for x in store.evolution_candidate_history(item['id'],'default')]
                self.assertIn('ACTIVE',targets)
                self.assertIn('HUMAN_APPROVED',targets)
            active=store.get_evolution_candidate(result['restored_release']['candidate_id'],'default')
            self.assertTrue(all(not v.strip() for v in fixture.overrides(active['content']).values()))
