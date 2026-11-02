import copy
import unittest
from govern.review.finding_gate import finding_rejection,normalize_coverage
from govern.policy.bundle import render_report


class FindingGateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'snapshot_id':'snap-test','base_files':{'StudentAnswer.vue':'load() {\n changeProblem()\n}'},
            'head_files':{'StudentAnswer.vue':'load() {\n restoreSavedAnswer()\n}','tests/test_saved.py':'def test_saved():\n assert saved_answer'}}
        self.units=[{'change_unit_id':'u','path':'StudentAnswer.vue'}]
        self.requirements=[{'requirement_id':'KAN-5','acceptance_criteria':[{'criterion_id':'KAN-5:AC-1'}]}]
        self.policy={'sha256':'policy-test'}
        self.finding={'finding_id':'F-1','title':'New head regression','side':'head','path':'StudentAnswer.vue','line':2,
            'disposition':'actionable','change_causality':{'status':'introduced','change_unit_ids':['u'],
                'head_evidence_refs':['code://snap-test/head/StudentAnswer.vue#L2'],
                'base_evidence_refs':['code://snap-test/base/StudentAnswer.vue#L2'],'reasoning':'Changed initialization violates the retained contract.'}}

    def reject(self,f):return finding_rejection(f,self.snapshot,self.units,self.requirements,self.policy)

    def test_aiedu_old_bug_fixed_by_head_is_not_a_finding(self):
        historical={'finding_id':'CORR-001','side':'base','title':'load 首次加载覆盖第一题答案',
            'recommendation':'head 版本修复方案正确：首次加载时直接设置 p_now、调用 check()、设置 p_main/score/content。建议增加回归测试。'}
        self.assertEqual('fixed_in_head',self.reject(historical))
        for field in ('disposition','change_causality'):
            f=copy.deepcopy(self.finding)
            if field=='disposition':f[field]='fixed_in_head'
            else:f[field]['status']='fixed_in_head'
            self.assertEqual('fixed_in_head',self.reject(f))

    def test_aiedu_no_regression_and_no_fix_required_are_not_findings(self):
        f={'finding_id':'CORR-002','title':'head 版本 total_score 默认计算逻辑与 base 一致，确认无回归','recommendation':'无需修改。'}
        self.assertEqual('no_regression',self.reject(f))
        f['title']='No fix required';self.assertEqual('no_regression',self.reject(f))
        f=copy.deepcopy(self.finding);f['disposition']='pre_existing';self.assertEqual('pre_existing',self.reject(f))

    def test_new_head_defect_needs_real_frozen_evidence_and_unit_link(self):
        self.assertIsNone(self.reject(self.finding))
        f=copy.deepcopy(self.finding);f.pop('change_causality');self.assertEqual('needs_evidence',self.reject(f))
        for value in ('code://other/head/StudentAnswer.vue#L2','code://snap-test/head/StudentAnswer.vue#L999','code://snap-test/base/StudentAnswer.vue#L2'):
            f=copy.deepcopy(self.finding);f['change_causality']['head_evidence_refs']=[value]
            self.assertEqual('missing_frozen_head_evidence',self.reject(f))
        f=copy.deepcopy(self.finding);f['change_causality']['base_evidence_refs']=[]
        self.assertEqual('missing_frozen_base_comparison',self.reject(f))
        f=copy.deepcopy(self.finding);f['change_causality']['change_unit_ids']=['invented']
        self.assertEqual('invalid_change_causality',self.reject(f))

    def test_deleted_line_regression_may_use_base_anchor_with_independent_head_witness(self):
        self.snapshot['base_files']['removed.py']='def required_api(): pass'
        self.snapshot['head_files']['caller.py']='required_api()'
        self.units.append({'change_unit_id':'deleted','path':'removed.py'})
        f=copy.deepcopy(self.finding);f.update(side='base',path='removed.py',line=1)
        f['change_causality'].update(change_unit_ids=['deleted'],head_evidence_refs=['code://snap-test/head/caller.py#L1'],base_evidence_refs=['code://snap-test/base/removed.py#L1'],reasoning='Removing the API leaves the surviving head caller broken.')
        self.assertIsNone(self.reject(f))

    def test_test_gap_is_independent_and_a_suggested_test_alone_is_insufficient(self):
        f=copy.deepcopy(self.finding);f.update(title='Missing regression for restored saved answers',recommendation='head 版本修复方案正确，但已检查的测试没有覆盖首次加载。')
        f['change_causality']['status']='test_gap'
        self.assertEqual('unsupported_test_gap',self.reject(f))
        f['test_gap']={'expected_behavior':'preserve saved answer on load','missing_scenario':'reload with nonempty draft and empty editor',
            'assertion_source':'KAN-5:AC-1','why_insufficient':'Existing test checks API save only, not component initialization.',
            'existing_test_evidence_refs':['code://snap-test/head/tests/test_saved.py#L1-L2']}
        self.assertIsNone(self.reject(f))
        for source in ('KAN-5:invented','policy://other'):
            f['test_gap']['assertion_source']=source;self.assertEqual('unsupported_test_gap',self.reject(f))
        f['test_gap']['assertion_source']='policy://policy-test';self.assertIsNone(self.reject(f))

    def test_structured_coverage_remains_readable_and_preserves_raw_evidence(self):
        raw={'dimension':'authorization','reason':'Backend handler was not loaded','evidence_refs':[{'path':'StudentAnswer.vue','line':2}]}
        coverage=normalize_coverage({'incomplete_dimensions':[raw,'Runtime not executed']})
        self.assertIn('authorization: Backend handler was not loaded',coverage['incomplete_dimensions'][0])
        self.assertIn('StudentAnswer.vue',coverage['incomplete_dimensions'][0]);self.assertEqual([raw],coverage['incomplete_details'])
        document=render_report('run',{'findings':[],'coverage':coverage,'filtered_findings':[{'finding_id':'CORR-001','reason':'fixed_in_head','title':'Old repaired defect'}]})
        self.assertNotIn('[object Object]',document)
        self.assertIn('非缺陷说明与过滤记录',document);self.assertNotIn('## CORR-001',document)


if __name__=='__main__':unittest.main()
