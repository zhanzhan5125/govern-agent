import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from govern.core.paths import project_root
from govern.evaluation.govern_dataset import load_govern_dataset
from govern.evaluation.evaluation_harness import load_jsonl
from govern.review.repository import change_units


class DatasetRegistrationTests(unittest.TestCase):
    def settings(self,core='',shadow=''):
        return SimpleNamespace(evolution_dataset_path=core,evolution_shadow_dataset_path=shadow)

    def test_builtin_100_cases_are_registered_without_faking_production_readiness(self):
        d=load_govern_dataset(self.settings());summary=d.summary()
        self.assertEqual({'validation':80,'holdout':20},summary['counts'])
        self.assertTrue(summary['mechanism_ready']);self.assertNotIn('production_ready',summary)
        self.assertEqual(['synthetic-controlled'],summary['source_kinds']);self.assertEqual(100,summary['frozen_context_cases'])
        self.assertEqual(0,summary['p01_labeled_findings']);self.assertEqual('bundled-controlled',summary['origin'])
        self.assertNotIn('production_gates',summary)
        self.assertNotIn('expected_findings',json.dumps(summary))

    def test_every_builtin_case_has_exact_before_and_after_context_for_pi(self):
        original={c['id']:c for c in load_jsonl(str(project_root()/'data/benchmarks/pr_diff_100.jsonl'))}
        d=load_govern_dataset(self.settings())
        for case in d.cases('validation')+d.cases('holdout'):
            self.assertEqual(original[case['id']]['source'],case['source'])
            self.assertEqual(original[case['id']]['expected_findings'],case['expected_findings'])
            self.assertEqual(original[case['id']]['diff'],case['diff'])
            snapshot={'snapshot_id':'test','diff':case['diff'],'base_files':case['before_files'],'head_files':case['after_files'],
                      'diff_sha256':hashlib.sha256(case['diff'].encode()).hexdigest()}
            self.assertTrue(change_units(snapshot))

    def test_explicit_dataset_takes_precedence_and_missing_file_is_not_silently_replaced(self):
        rows=load_jsonl(str(project_root()/'data/benchmarks/pr_diff_100.jsonl'))
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'custom.jsonl'
            file.write_text('\n'.join(json.dumps(c) for c in (rows[0],rows[-1])),encoding='utf-8')
            d=load_govern_dataset(self.settings(str(file)))
            self.assertEqual(2,d.summary()['total_cases']);self.assertEqual('configured',d.summary()['origin'])
            self.assertEqual(0,d.summary()['frozen_context_cases'])
            self.assertFalse(d.summary()['pi_context_ready']);self.assertFalse(d.summary()['mechanism_ready'])
            with self.assertRaises(FileNotFoundError):load_govern_dataset(self.settings(str(Path(folder)/'missing.jsonl')))

    def test_builtin_context_recovery_fails_closed_if_generator_no_longer_matches(self):
        rows=load_jsonl(str(project_root()/'data/benchmarks/pr_diff_100.jsonl'))
        modified=copy.deepcopy(rows);modified[0]['diff']+='\n# different corpus'
        with patch('govern.evaluation.govern_dataset.load_jsonl',return_value=modified):
            with self.assertRaisesRegex(ValueError,'differs from original generator'):load_govern_dataset(self.settings())

    def test_missing_pack_is_reported_as_empty_not_as_ready(self):
        with tempfile.TemporaryDirectory() as folder,patch('govern.evaluation.govern_dataset.project_root',return_value=Path(folder)):
            summary=load_govern_dataset(self.settings()).summary()
            self.assertEqual(0,summary['total_cases']);self.assertFalse(summary['mechanism_ready']);self.assertNotIn('production_ready',summary)


if __name__=='__main__':unittest.main()
