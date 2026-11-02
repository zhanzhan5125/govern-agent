import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from govern.storage.store import TaskStore


class SQLiteContainerBackupTests(unittest.TestCase):
    def test_snapshot_preserves_history_configuration_and_policy_without_changing_source(self):
        script=Path(__file__).resolve().parents[2]/'scripts/maintenance/backup_sqlite_container.py'
        spec=importlib.util.spec_from_file_location('container_backup',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'.govern';state.mkdir()
            store=TaskStore(str(state/'govern.sqlite'));store.create('old-run','authorized/repo',None,{'govern':True})
            config={'model':{'api_key':'local-test-key'},'jira':{'retained':True}}
            (state/'config.json').write_text(json.dumps(config),encoding='utf-8')
            (root/'policies').mkdir();(root/'policies'/'stage.md').write_text('Original policy',encoding='utf-8')
            result=module.backup(root);target=Path(result['backup_directory'])
            self.assertTrue(target.is_relative_to(state/'migration-backup'))
            self.assertEqual(1,result['tasks']);self.assertEqual(config,json.loads((target/'config.json').read_text()))
            self.assertEqual('Original policy',(target/'policies'/'stage.md').read_text())
            self.assertNotIn('local-test-key',json.dumps(result))
            with closing(sqlite3.connect(target/'govern.sqlite')) as copied:
                self.assertEqual('ok',copied.execute('pragma quick_check').fetchone()[0])
                self.assertEqual('old-run',copied.execute('select id from tasks').fetchone()[0])
            self.assertEqual('old-run',store.list_tasks()[0]['id'])
