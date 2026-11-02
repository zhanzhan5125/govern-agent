import importlib.util
from pathlib import Path
import tempfile
import unittest
from govern.storage.store import TaskStore


spec=importlib.util.spec_from_file_location('prune_history',Path(__file__).resolve().parents[2]/'scripts/maintenance/prune_review_history.py')
pruner=importlib.util.module_from_spec(spec);spec.loader.exec_module(pruner)


class PruneReviewHistoryTests(unittest.TestCase):
    def test_explicit_cleanup_preserves_keep_evidence_tenant_and_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite';store=TaskStore(str(db))
            for identifier,tenant,govern in [('keep','default',True),('remove','default',True),('other','another',True),('non-review','default',False)]:
                store.create(identifier,'repo',None,{'govern':govern},tenant)
                with store._connect() as conn:conn.execute("UPDATE tasks SET state='FAILED' WHERE id=?",(identifier,))
                store.save_checkpoint(identifier,'input',{'preserved':identifier},'completed')
                store.append_runtime_event(identifier,'local','test',0,1,'',{'safe':True})
            self.assertEqual(1,pruner.prune(db,['keep'])['deleted_count'])
            self.assertIsNotNone(store.get('remove'))
            with self.assertRaises(ValueError):pruner.prune(db,['missing'],apply=True)
            with self.assertRaises(ValueError):pruner.prune(db,['keep'],apply=True,expected_deleted=2)
            before=store.get('keep');pruner.prune(db,['keep'],apply=True,expected_deleted=1)
            self.assertEqual(before,store.get('keep'));self.assertIsNone(store.get('remove'))
            self.assertIsNotNone(store.get('other'));self.assertIsNotNone(store.get('non-review'))
            with store._connect() as conn:
                self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())
                with self.assertRaises(Exception):conn.execute("DELETE FROM runtime_journal WHERE task_id='keep'")

    def test_active_review_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite';store=TaskStore(str(db))
            for identifier in ['keep','active']:store.create(identifier,'repo',None,{'govern':True})
            with self.assertRaisesRegex(ValueError,'active review'):pruner.prune(db,['keep'],apply=True)
            self.assertIsNotNone(store.get('active'))
