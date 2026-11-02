import unittest
from unittest.mock import patch
from types import SimpleNamespace
from govern.review.frozen_search import search_frozen
from govern.review.workbench import GovernWorkbench


class FrozenSearchTests(unittest.TestCase):
    def setUp(self):
        self.path='AIedu 前端/src/views/StudentAnswer.vue'
        self.snapshot={'snapshot_id':'snap-aiedu','base_files':{self.path:'changeProblem(item) {\n old_only()\n}\n','tests/unchanged.spec.js':'test("saved answer", check)\n'},
            'head_files':{self.path:'load() {\n check()\n}\n','tests/unchanged.spec.js':'test("saved answer", check)\n'}}
    def test_search_both_versions_and_unchanged_test_paths(self):
        for fallback in (False,True):
            with patch('govern.review.frozen_search.shutil.which',return_value=None) if fallback else patch('govern.review.frozen_search.fnmatch.fnmatchcase',wraps=__import__('fnmatch').fnmatchcase):
                found=search_frozen(self.snapshot,{'query':'changeProblem','side':'both'})
                self.assertEqual(['base','head'],found['searched_sides'])
                self.assertEqual(self.path,found['hits'][0]['path']);self.assertEqual('base',found['hits'][0]['side'])
                self.assertFalse(search_frozen(self.snapshot,{'query':'old_only','side':'head'})['hits'])
                tests=search_frozen(self.snapshot,{'query':'','path_glob':'**/*.spec.*','side':'both'})
                self.assertEqual(2,len(tests['hits']));self.assertEqual('tests/unchanged.spec.js',tests['hits'][0]['path'])
                self.assertFalse(search_frozen(self.snapshot,{'query':'*.spec.*'})['hits'])
    def test_default_head_truncation_and_invalid_side(self):
        self.snapshot['head_files']['many.py']='needle\n'*50
        result=search_frozen(self.snapshot,{'query':'needle'})
        self.assertEqual(['head'],result['searched_sides']);self.assertEqual(40,len(result['hits']));self.assertTrue(result['truncated'])
        with self.assertRaises(ValueError):search_frozen(self.snapshot,{'query':'x','side':'worktree'})
    def test_read_full_definition_and_pagination_metadata(self):
        w=GovernWorkbench.__new__(GovernWorkbench);w.artifacts=SimpleNamespace(get=lambda *a:{},put=lambda *a:SimpleNamespace(uri='artifact://test-observation'))
        frozen={'policy_ref':'artifact://policy'}
        result=w.tool('t','default',frozen,self.snapshot,'contract_test','read_file',{'path':self.path,'side':'base','start_line':1,'end_line':1})
        self.assertEqual('changeProblem(item) {',result['content']);self.assertTrue(result['has_more']);self.assertEqual(2,result['next_start_line'])
        result=w.tool('t','default',frozen,self.snapshot,'contract_test','read_file',{'path':self.path,'side':'base','start_line':2,'end_line':100})
        self.assertFalse(result['has_more']);self.assertIsNone(result['next_start_line']);self.assertEqual(3,result['end_line'])
    def test_ripgrep_windows_paths_become_readable_frozen_paths(self):
        import json
        data={'type':'match','data':{'path':{'text':'.\\'+self.path.replace('/','\\')},'line_number':1,'lines':{'text':'load() {'}}}
        with patch('govern.review.frozen_search.shutil.which',return_value='rg'),patch('govern.review.frozen_search.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(data))):
            hit=search_frozen(self.snapshot,{'query':'load'})['hits'][0]
            self.assertEqual(self.path,hit['path'])
