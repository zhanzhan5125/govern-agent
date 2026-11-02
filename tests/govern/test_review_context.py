"""Sanitized AIedu-shaped regression: space/Chinese paths, Vue reload + saved-answer test."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from govern.review.repository import capture,change_units,diff_path


class ReviewContextTests(unittest.TestCase):
    def test_pi_context_policy_reuses_context_manager_settings(self):
        from types import SimpleNamespace
        from govern.review.workbench import GovernWorkbench
        manager=SimpleNamespace(context_window_tokens=16384,input_token_budget=10000,observation_token_budget=2000,recent_observations=3)
        p=GovernWorkbench.context_policy(SimpleNamespace(service=SimpleNamespace(context_manager=manager)))
        self.assertEqual(16384,p['window_tokens']);self.assertEqual(10000,p['input_tokens'])
        self.assertEqual(2000,p['observation_tokens']);self.assertEqual(3,p['recent_observations'])
        self.assertEqual(4000,p['output_tokens']);self.assertEqual(512,p['safety_tokens'])

    def test_git_path_separators_and_c_quoting(self):
        self.assertEqual('AIedu 后端/tests/test_api.py',diff_path('b/AIedu 后端/tests/test_api.py\t'))
        self.assertEqual('中文 name.py',diff_path('"a/\\344\\270\\255\\346\\226\\207 name.py"\t'))
        self.assertEqual('a\tb.py',diff_path('"b/a\\tb.py"'))
        with self.assertRaises(ValueError):diff_path('"unterminated')

    def test_saved_answer_reload_diff_has_valid_function_context_not_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):return subprocess.check_output(['git','-C',folder,*args],stderr=subprocess.STDOUT).decode().strip()
            git('init','-b','main');git('config','user.name','Test');git('config','user.email','test@example.com');git('config','core.quotePath','false')
            test=root/'AIedu FastAPI后端/tests/test_api.py';test.parent.mkdir(parents=True)
            vue=root/'AIedu 前端/src/views/StudentAnswer.vue';vue.parent.mkdir(parents=True)
            test.write_text('def test_saved_assignment_answers():\n    saved = "draft"\n    assert saved\n',encoding='utf-8')
            vue.write_text('<template><div/></template>\n<script>\nexport default { methods: {\nload() {\n this.changeProblem(1)\n}\n}}\n</script>\n',encoding='utf-8')
            git('add','.');git('commit','-m','base')
            test.write_text('def test_saved_assignment_answers():\n    saved = "draft"\n    assert saved == "draft"\n    assert saved != ""\n',encoding='utf-8')
            vue.write_text('<template><div/></template>\n<script>\nexport default { methods: {\nload() {\n const firstQuestion = this.tableData[0]\n this.content = firstQuestion.answer\n}\n}}\n</script>\n',encoding='utf-8')
            snapshot=capture(root,{'mode':'working-tree'});units=change_units(snapshot)
            self.assertTrue(any(u['symbol']['kind']=='FunctionDef' for u in units))
            self.assertTrue(any(u['symbol']['kind']=='method_definition' and u['symbol']['name']=='load' for u in units))
            for u in units:
                self.assertNotEqual('file_metadata',u['symbol']['kind'])
                self.assertFalse(u['path'].endswith('\t'))
                self.assertTrue(u['context']);self.assertLessEqual(u['context_start'],u['context_end'])
                self.assertLessEqual(u['context_end'],len(snapshot[u['side']+'_files'][u['path']].splitlines()))
            bad={**snapshot,'head_files':{}}
            with self.assertRaisesRegex(ValueError,'unavailable'):change_units(bad)

    @unittest.skipUnless(os.environ.get('GOVERN_AIEDU_REPO'),'opt-in read-only real repository regression')
    def test_actual_frozen_aiedu_range_read_only(self):
        s=capture(os.environ['GOVERN_AIEDU_REPO'],{'mode':'range','base':'27f8f34476b5fa7730383cad0a80a8757c906f20','head':'e207cba83dcbdc1484f196453e114652572c9680'})
        self.assertEqual('b0684729bb96e5c80cd392e4a969583abc84fde1bbb9ee2cc20f8d2783815d80',s['diff_sha256'])
        units=change_units(s)
        self.assertTrue(units);self.assertTrue(all(u['context'] and u['context_start']<=u['context_end'] for u in units))
        self.assertTrue(all(u['symbol']['kind']!='file_metadata' for u in units))
        self.assertTrue(any(u['symbol']['name']=='load' for u in units))

    @unittest.skipUnless(os.environ.get('GOVERN_AIEDU_REPO') and os.environ.get('GOVERN_NODE'),'opt-in read-only repository and Pi planning regression')
    def test_actual_aiedu_slices_without_model_configuration(self):
        from govern.runtime.pi_bridge import PiBridge
        snapshot=capture(os.environ['GOVERN_AIEDU_REPO'],{'mode':'range','base':'27f8f34476b5fa7730383cad0a80a8757c906f20','head':'e207cba83dcbdc1484f196453e114652572c9680'})
        units=change_units(snapshot)
        def deny(*args):raise AssertionError('Pure planning must not call tools or models')
        result=PiBridge().run({'method':'plan_review','input':{'snapshot_id':snapshot['snapshot_id'],'units':units}},deny,lambda _:None)
        self.assertEqual(4,len(units));self.assertEqual(1,len(result['slices']))
        self.assertEqual(3,len(result['slices'][0]['evidence_pack']))
        self.assertEqual({u['change_unit_id'] for u in units},set(result['slices'][0]['unit_ids']))


if __name__=='__main__':unittest.main()
