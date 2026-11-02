from pathlib import Path
import tempfile
import unittest
from govern.storage.store import TaskStore
from govern.evaluation.govern_dataset import GovernDataset,load_govern_dataset
from govern.policy.feedback_evolution import GovernEvolution,GovernCandidatePolicy,skill
from govern.policy.evolution_pipeline import EvolutionPipeline
from tests.govern.test_local_evolution_fixture import fixture
from types import SimpleNamespace


class TwoSplitReleaseTests(unittest.TestCase):
    def test_small_synthetic_dataset_publishes_only_after_evaluation_and_human_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=TaskStore(str(Path(tmp)/'state.db'))
            dataset=GovernDataset(fixture.cases_for('security'))
            pipeline=GovernEvolution.__new__(GovernEvolution)
            EvolutionPipeline.__init__(pipeline,store,dataset,reviewer_factory=lambda kind,content:fixture.FixtureReviewer(content),
                policy=GovernCandidatePolicy(),baseline_provider=lambda *a:{'content':skill({}),'version':'initial'},runtime_identity={'runtime':'offline-test','model':None})
            c=pipeline.propose('default','skill','govern-review',skill({'security':'Check tenant ownership. [check:tenant]'}),'tester',{'stage':'security'})
            with self.assertRaises(ValueError):pipeline.activate(c['id'],'default',c['revision'],'tester','not evaluated')
            c=pipeline.evaluate(c['id'],'default',c['revision'])
            self.assertEqual('HOLDOUT_PASSED',c['status'])
            with self.assertRaises(ValueError):pipeline.activate(c['id'],'default',c['revision'],'tester','no approval')
            c=pipeline.approve(c['id'],'default',c['revision'],'human-tester','reviewed metrics')
            c=pipeline.activate(c['id'],'default',c['revision'],'human-tester','confirm publication')
            self.assertEqual('ACTIVE',c['status']);self.assertNotIn('SHADOW',c['reports'])
            self.assertEqual({'validation':8,'holdout':8},dataset.summary()['counts'])
            with self.assertRaises(ValueError):pipeline.shadow(c['id'],'default',c['revision'])
    def test_train_is_not_a_govern_split_and_shadow_configuration_is_not_loaded(self):
        rows=fixture.cases_for('security');rows[0]['split']='train'
        with self.assertRaisesRegex(ValueError,'only validation and holdout'):GovernDataset(rows)
        data=load_govern_dataset(SimpleNamespace(evolution_dataset_path='',evolution_shadow_dataset_path='does-not-exist.jsonl'))
        self.assertEqual({'validation':80,'holdout':20},data.summary()['counts'])
        self.assertNotIn('production_gates',data.summary())
