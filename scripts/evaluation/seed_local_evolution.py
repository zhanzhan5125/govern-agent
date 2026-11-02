"""Explicit offline integration fixtures. No model, Jira or sandbox execution."""
import argparse
import copy
import json
from pathlib import Path
import sys
import sqlite3
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from govern.storage.store import TaskStore, utc_now
from govern.core.models import Finding, Severity, TraceEvent, TaskState
from govern.review.workbench import GovernWorkbench, ReportValue
from govern.policy.feedback_evolution import GovernEvolution, GovernCandidatePolicy, skill, overrides
from govern.policy.evolution_pipeline import EvolutionPipeline
from govern.policy.evolution_replay import EvolutionDataset, EvolutionReplay
from govern.evaluation.govern_dataset import GovernDataset
from govern.policy.feedback_pool import pending_groups
from govern.policy.bundle import compile_bundle, ROLES
from govern.review.repository import digest

SOURCE = 'local-integration-fixture'
SCENARIOS = [
 ('correctness', 'P0', '资金重复扣款', 'missed_issue', 'missed_behavior', '扣款接口重试未复用幂等键，超时重试会重复扣款。',
  'For payment retries, trace the idempotency key from request to durable uniqueness enforcement. Distinguish timeout from transaction failure; require a reachable repeated-call witness before reporting duplicate debit. [check:payment]'),
 ('security', 'P0', '跨租户越权', 'missed_issue', 'missed_behavior', '查询仅按订单ID过滤，未将已认证租户加入查询条件。',
  'For tenant-scoped resources, trace authenticated tenant identity into every object query and mutation. Verify ownership at the actual data-access boundary; never infer authorization solely from a login guard. [check:tenant]'),
 ('contract_test', 'P1', '事务失败导致数据丢失', 'false_positive', 'insufficient_evidence', '原告警只依据缺少测试文件名推断缺陷，应验证失败回滚契约及实际断言。',
  'For transaction failure contracts, inspect rollback assertions and existing integration coverage, not test filenames. Report an independent test gap only with the expected behavior and missing assertion; do not claim data loss solely from absent tests. [check:transaction]'),
]


def cases_for(stage):
    key={'correctness':'payment','security':'tenant','contract_test':'transaction'}[stage]
    cases=[]
    for split in ('validation','holdout'):
        for i in range(8):
            # Baseline covers common risks; the candidate adds a boundary check.
            code = f'{"boundary" if i%2 else "common"}_{key}({i})'
            path=f'src/{key}.py'
            cases.append({'id':f'{stage}-{split}-{i}', 'repository':f'fixture/{stage}-{split}',
                'pull_request':i+1,'split':'holdout' if split=='shadow' else split,'fixture_phase':split,'source':{'kind':'synthetic-controlled','generator':SOURCE},
                'diff':f'--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-pass\n+{code} # {split}\n',
                'expected_findings':[{'path':path,'start_line':1,'end_line':1,'rule_id':'BUSINESS',
                    'cwe':'CWE-840','severity':'high','should_comment':True,'business_priority':'P1'}]})
    return cases


class FixtureReviewer:
    def __init__(self, content):
        self.guidance='\n'.join(content.get('files',{}).values());self.hits=0
    def review_case(self, case, parsed):
        line=parsed.added_lines[0];self.hits=0
        key=line.content.split('(',1)[0].removeprefix('boundary_')
        if line.content.startswith('boundary_') and '[check:'+key+']' not in self.guidance:return []
        self.hits=1
        return [Finding('CWE-840',Severity.HIGH,'业务边界风险','确定性契约检查',line.path,line.line,line.content,'补齐边界保护','增加边界断言')]
    def evaluation_execution(self):return dict(total_tokens=0,cost_usd=0,duration_ms=0,tool_calls=0)
    def evaluation_contract(self):return dict(telemetry_complete=True,skill_selected=True,permission_violations=0,budget_violations=0,required_gate_misses=0)
    def evaluation_measurements(self,case,findings):return {'p01_hits':self.hits}


def seed(db, tenant='default', overlay=None):
    store=TaskStore(str(db));original=store.evolution_release_head(tenant,'skill','govern-review')
    w=GovernWorkbench.__new__(GovernWorkbench);w.store=store;w.lock=threading.Lock()
    result=[]
    for stage,priority,title,kind,reason_code,reason,guidance in SCENARIOS:
        dataset_cases=cases_for(stage)
        dataset=GovernDataset(dataset_cases)
        pipeline=GovernEvolution.__new__(GovernEvolution)
        EvolutionPipeline.__init__(pipeline,store,dataset,
            reviewer_factory=lambda kind,content:FixtureReviewer(content),policy=GovernCandidatePolicy(),
            baseline_provider=lambda *args:{'content':skill(overlay or {}),'version':'govern-1'},
            runtime_identity={'runtime':SOURCE,'model':None})
        baseline=pipeline._baseline(tenant,'skill','govern-review')['content']
        tid=str(uuid.uuid4());fid='F-'+uuid.uuid4().hex[:10]
        finding={'finding_id':fid,'title':title,'business_priority':priority,'severity':'critical' if priority=='P0' else 'high',
          'source_stage':stage,'source_module':stage,'path':f'src/{stage}.py','line':1,'description':reason,
          'impact_scope':'核心交易及租户数据','recommendation':guidance.split(' [check:')[0],
          'related_tests':[{'path':f'tests/test_{stage}.py','name':'boundary_contract'}],
          'evidence_refs':[{'source':SOURCE,'ref':f'fixture://{stage}/boundary'}]}
        store.create(tid,f'fixture/business/{stage}',None,{'govern':True,'source':SOURCE},tenant)
        store.save_task_payload(tid,f'--- a/src/{stage}.py\n+++ b/src/{stage}.py\n@@ -1 +1 @@\n-pass\n+boundary_{stage}()\n')
        store.succeed(tid,ReportValue({'summary':title,'findings':[] if kind=='missed_issue' else [finding],
            'source':SOURCE,'coverage':{'incomplete_dimensions':[]},'execution':{'total_tokens':0}}),
            TraceEvent(1,TaskState.SUCCESS,'本地确定性测试输入已就绪',utc_now()))
        feedback=w.feedback(tid,tenant,{'type':kind,'finding_id':fid,'finding':finding,'reason':reason,'reason_code':reason_code})
        groups,pool=pending_groups(store,tenant,ROLES,[],digest(baseline),compile_bundle()['source_sha256'])
        assert any(x['feedback_id']==feedback['feedback_id'] for values in groups.values() for x in values)
        content=copy.deepcopy(baseline);content['files'][f'stages/{stage}.md']=overrides(baseline)[stage]+'\n'+guidance+'\n'
        candidate=pipeline.propose(tenant,'skill','govern-review',content,'local-test-runner',{
            'source':SOURCE,'stage':stage,'title':title,'rationale':reason,'feedback_ids':[feedback['feedback_id']],
            'feedback_context':[feedback],'task_ids':[tid],'pool':pool})
        # Real evaluator calculates metrics from deterministic outputs; no invented LLM usage.
        for phase,target in [('validation','VALIDATION_PASSED'),('holdout','HOLDOUT_PASSED')]:
            report=EvolutionReplay(pipeline.reviewer_factory,pipeline.policy).compare('skill',baseline,content,dataset.cases(phase),phase)
            report.update(source=SOURCE,measurement_kind='deterministic-fixture',model_called=False)
            if not report['passed']:raise RuntimeError(json.dumps(report,ensure_ascii=False))
            candidate=store.transition_evolution_candidate(candidate['id'],tenant,candidate['revision'],target,'local-test-evaluator',phase+' 确定性契约回归通过',report)
        candidate=pipeline.approve(candidate['id'],tenant,candidate['revision'],'user-authorized-test-runner','按用户授权执行本地测试审批；非人工实测评审')
        head=store.evolution_release_head(tenant,'skill','govern-review')
        active=store.publish_evolution_candidate(candidate['id'],tenant,candidate['revision'],head['generation'],'local-test-runner','用户授权本地测试激活',local_test=True)
        restore=store.get_evolution_candidate(active['reports']['release']['previous_candidate_id'],tenant)
        store.publish_evolution_candidate(restore['id'],tenant,restore['revision'],active['reports']['release']['generation'],'local-test-runner','测试完成，恢复激活前策略',rollback=True)
        result.append({'candidate_id':candidate['id'],'task_id':tid,'stage':stage,'title':title})
    final=store.evolution_release_head(tenant,'skill','govern-review')
    active=store.get_evolution_candidate(final['candidate_id'],tenant)
    expected=store.get_evolution_candidate(original['candidate_id'],tenant)['content'] if original['candidate_id'] else skill(overlay or {})
    # Initial baseline normalization may add schema_version; compare effective stage content.
    assert overrides(active['content'])==overrides(expected),'original stage guidance was not restored'
    return {'source':SOURCE,'model_calls':0,'candidates':result,'restored_release':final}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply-local-test',action='store_true');parser.add_argument('--db',required=True)
    args=parser.parse_args()
    if not args.apply_local_test:parser.error('explicit --apply-local-test required')
    db_path=Path(args.db).resolve()
    if not db_path.is_file():parser.error('existing local test database required')
    backup_path=db_path.with_name(db_path.name+'.before-fixtures-'+uuid.uuid4().hex[:8]+'.backup')
    with sqlite3.connect(str(db_path)) as current, sqlite3.connect(str(backup_path)) as backup:
        current.backup(backup)
    config_path=ROOT/'.govern/config.json'
    config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    print(json.dumps({'backup':str(backup_path),**seed(db_path,overlay=config.get('policy_overrides',{}))},ensure_ascii=False,indent=2))
