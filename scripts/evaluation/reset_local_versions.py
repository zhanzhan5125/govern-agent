"""Explicit, backed-up local test maintenance; never invoked by the web API."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from seed_local_evolution import SOURCE,SCENARIOS,FixtureReviewer,cases_for
from govern.storage.store import TaskStore
from govern.policy.feedback_evolution import GovernEvolution,GovernCandidatePolicy,skill
from govern.policy.evolution_pipeline import EvolutionPipeline
from govern.policy.evolution_replay import EvolutionDataset,EvolutionReplay
from govern.evaluation.govern_dataset import GovernDataset


def reset_and_seed(db,expected_ids,overlay=None):
    store=TaskStore(str(db));tenant='default'
    actual={c['id'] for c in store.list_evolution_candidates(tenant,200) if c['name']=='govern-review'}
    if actual!=set(expected_ids):raise ValueError('strategy records changed; recheck exact deletion targets')
    # Remove only the explicitly enumerated strategy stream, preserving review/feedback data.
    with store._candidate_transaction() as conn:
        triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='evolution_candidate_events'").fetchall()
        for t in triggers:
            if t['name'] not in {'evolution_candidate_events_no_delete','evolution_candidate_events_no_update'}:raise ValueError('unknown audit trigger')
            conn.execute('DROP TRIGGER '+t['name'])
        for identifier in expected_ids:
            for table in ('evolution_candidate_events','evolution_release_outcomes','evolution_replay_leases'):
                conn.execute(f'DELETE FROM {table} WHERE candidate_id=? AND tenant_id=?',(identifier,tenant))
            conn.execute('DELETE FROM evolution_candidates WHERE id=? AND tenant_id=? AND name=?',(identifier,tenant,'govern-review'))
        conn.execute('DELETE FROM evolution_release_heads WHERE tenant_id=? AND name=?',(tenant,'govern-review'))
        conn.execute('DELETE FROM govern_evolution_batches WHERE tenant_id=?',(tenant,))
        conn.execute('DELETE FROM govern_evolution_sequences WHERE tenant_id=?',(tenant,))
        for t in triggers:conn.execute(t['sql'])
    all_cases=sum([cases_for(stage) for stage,*_ in SCENARIOS],[])
    dataset=GovernDataset(all_cases)
    pipeline=GovernEvolution.__new__(GovernEvolution)
    EvolutionPipeline.__init__(pipeline,store,dataset,reviewer_factory=lambda kind,content:FixtureReviewer(content),
        policy=GovernCandidatePolicy(),baseline_provider=lambda *args:{'content':skill(overlay or {}),'version':'govern-1'},
        runtime_identity={'runtime':SOURCE,'model':None})
    base=copy.deepcopy(pipeline._baseline(tenant,'skill','govern-review')['content']);content=copy.deepcopy(base)
    feedback=[]
    for task in store.list_tasks(200,tenant):
        row=store.get(task['id'],tenant)
        if row['input'].get('source')==SOURCE:
            feedback.extend(store.load_checkpoints(task['id']).get('govern-feedback',{}).get('state',{}).get('history',[]))
    batch=store.start_govern_evolution_batch(tenant,'local-test-runner')
    for stage,*rest in SCENARIOS:content['files'][f'stages/{stage}.md']+='\n'+rest[-1]+'\n'
    candidate=pipeline.propose(tenant,'skill','govern-review',content,'local-test-runner',{
        'source':SOURCE,'batch_id':batch['id'],'batch_version':batch['version'],
        'stages':[s[0] for s in SCENARIOS],'feedback_context':feedback,'feedback_ids':[f['feedback_id'] for f in feedback],
        'stage_rationales':{s[0]:s[5] for s in SCENARIOS},'rationale':'同一版本更新正确性、安全性及契约测试三个Skill；确定性离线验证。'})
    for phase,target in [('validation','VALIDATION_PASSED'),('holdout','HOLDOUT_PASSED')]:
        report=EvolutionReplay(pipeline.reviewer_factory,pipeline.policy).compare('skill',base,content,dataset.cases(phase),phase)
        report.update(source=SOURCE,measurement_kind='deterministic-fixture',model_called=False)
        if not report['passed']:raise ValueError('fixture evaluation failed')
        candidate=store.transition_evolution_candidate(candidate['id'],tenant,candidate['revision'],target,'local-test-evaluator','离线测试评测通过',report)
    candidate=pipeline.approve(candidate['id'],tenant,candidate['revision'],'user-authorized-test-runner','用户授权测试审批；等待页面确认发布')
    store.finish_govern_evolution_batch(batch['id'],tenant,'GENERATED')
    return {'deleted_ids':list(expected_ids),'candidate_id':candidate['id'],'status':candidate['status'],'stages':candidate['lineage']['stages'],'model_calls':0}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--expected-ids',required=True);p.add_argument('--confirm-reset-local-test',action='store_true');args=p.parse_args()
    if not args.confirm_reset_local_test:p.error('explicit confirmation required')
    db=Path(args.db).resolve()
    if not db.is_file():p.error('existing local test SQLite database required')
    backup=db.with_name(db.name+'.before-version-reset-'+uuid.uuid4().hex[:8]+'.backup')
    with sqlite3.connect(str(db)) as src,sqlite3.connect(str(backup)) as dst:src.backup(dst)
    config=json.loads((ROOT/'.govern/config.json').read_text(encoding='utf-8'))
    result=reset_and_seed(db,args.expected_ids.split(','),config.get('policy_overrides',{}))
    print(json.dumps({'backup':str(backup),**result},ensure_ascii=False,indent=2))
