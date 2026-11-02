import importlib.util
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from govern.storage.postgres_store import create_store
from govern.storage.store import TaskStore


class StorageSelectionTests(unittest.TestCase):
    def test_explicit_sqlite_and_invalid_url_never_silently_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'local.sqlite')
            self.assertIsInstance(create_store('', path), TaskStore)
            invalid = Path(directory) / 'unexpected.sqlite'
            with self.assertRaisesRegex(ValueError, 'Refusing silent SQLite fallback'):
                create_store('mysql://wrong', str(invalid))
            self.assertFalse(invalid.exists())

    def test_launcher_modes_keep_credentials_out_of_arguments_and_config(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/start_govern.py'
        spec = importlib.util.spec_from_file_location('govern_launcher', path)
        launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):launcher.configure_storage('postgres')
            os.environ['GOVERN_DATABASE_URL'] = 'postgresql://localhost/test'
            launcher.configure_storage('postgres')
            self.assertEqual(os.environ['GOVERN_DATABASE_URL'], os.environ['EVOAGENT_DATABASE_URL'])
            launcher.configure_storage('sqlite')
            self.assertEqual('', os.environ['EVOAGENT_DATABASE_URL'])

    def test_container_bootstrap_preserves_operator_config_and_escapes_database_credentials(self):
        path=Path(__file__).resolve().parents[2]/'scripts/start_container.py'
        spec=importlib.util.spec_from_file_location('container_launcher',path)
        launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{
            'GOVERN_STORAGE':'postgres','GOVERN_POSTGRES_USER':'u@',
            'GOVERN_POSTGRES_PASSWORD':'secret/:','GOVERN_POSTGRES_HOST':'postgres',
            'GOVERN_POSTGRES_DATABASE':'govern','GOVERN_CONFIG':str(Path(directory)/'config.json')
        },clear=True),patch('govern.api.api.run') as run:
            launcher.main();run.assert_called_once()
            self.assertEqual('postgresql://u%40:secret%2F%3A@postgres:5432/govern',os.environ['EVOAGENT_DATABASE_URL'])
            configuration=Path(os.environ['GOVERN_CONFIG'])
            self.assertNotIn('secret',configuration.read_text())
            value={'auto_evolution':{'enabled':False},'retained':'operator-setting',
                   'model':{'base_url':'http://127.0.0.1:9/v1','model':'test-only','api_key':'not-a-real-key'}}
            configuration.write_text(json.dumps(value),encoding='utf-8')
            os.environ['GOVERN_STORAGE']='sqlite';launcher.main()
            self.assertEqual('',os.environ['EVOAGENT_DATABASE_URL'])
            self.assertEqual(value,json.loads(configuration.read_text()))
            self.assertEqual('custom',os.environ['EVOAGENT_LLM_PROVIDER'])
            self.assertEqual('test-only',os.environ['EVOAGENT_LLM_MODEL'])
            self.assertEqual('not-a-real-key',os.environ['EVOAGENT_LLM_API_KEY'])
