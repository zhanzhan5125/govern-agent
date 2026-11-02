import concurrent.futures
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from govern.storage.store import TaskStore
from govern.policy.feedback_evolution import GovernEvolution

class EvolutionBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'test.db');self.store=TaskStore(self.db)
    def pipeline(self):
        p=GovernEvolution.__new__(GovernEvolution);p.store=self.store;p.generation_lock=threading.Lock()
        return p
    def test_batch_exists_before_generation_and_survives_no_changes(self):
        p=self.pipeline()
        def generate(tenant,actor,stages,batch):
            self.assertEqual('GENERATING',self.store.list_govern_evolution_batches(tenant)[0]['status'])
            return {'candidates':[],'reason':'no eligible feedback'}
        p._generate=generate
        self.assertEqual(1,p.generate('a','tester')['batch']['version'])
        self.assertEqual('NO_CHANGES',self.store.list_govern_evolution_batches('a')[0]['status'])
        self.assertEqual([],self.store.list_govern_evolution_batches('b'))
    def test_failure_recorded_without_persisting_model_exception(self):
        p=self.pipeline();p._generate=Mock(side_effect=ValueError('secret credential'))
        with self.assertRaises(ValueError):p.generate('a','tester')
        row=self.store.list_govern_evolution_batches('a')[0]
        self.assertEqual('FAILED',row['status']);self.assertNotIn('secret',str(row))
    def test_sequence_is_atomic_and_persists(self):
        def start(i):return self.store.start_govern_evolution_batch('a','tester')['version']
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(range(1,9)),sorted(pool.map(start,range(8))))
        self.assertEqual(9,TaskStore(self.db).start_govern_evolution_batch('a','tester')['version'])
