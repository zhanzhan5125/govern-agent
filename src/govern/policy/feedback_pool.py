"""Bounded live-feedback context; full records remain in failure/checkpoint storage."""
import json
from collections import defaultdict
from govern.review.repository import digest

PENDING = {'DRAFT','STATIC_PASSED','VALIDATION_PASSED','HOLDOUT_PASSED','HUMAN_APPROVED','SHADOW'}
PUBLISHED = {'ACTIVE','SUPERSEDED'}


def compact(value, depth=0):
    if depth > 5:return '[deep evidence omitted; consult original feedback]'
    if isinstance(value,str):return value if len(value)<=1200 else value[:1200]+' [excerpt]'
    if isinstance(value,list):return [compact(x,depth+1) for x in value[:8]]
    if isinstance(value,dict):return {str(k)[:100]:compact(v,depth+1) for k,v in list(value.items())[:20]}
    return value


def bounded_examples(examples, max_cases=10, max_bytes=24000):
    # Round-robin repositories to avoid one busy repository monopolizing a group.
    buckets=defaultdict(list)
    for example in examples:buckets[example['repository']].append(example)
    selected=[]; used=2
    while buckets and len(selected)<max_cases:
        for repo in list(buckets):
            value=buckets[repo].pop(0)
            if not buckets[repo]:del buckets[repo]
            size=len(json.dumps(value,ensure_ascii=False).encode('utf-8'))+2
            if used+size<=max_bytes:
                selected.append(value);used+=size
            if len(selected)>=max_cases:break
    return selected


def pending_groups(store, tenant, roles, protected, baseline_hash, source_hash, max_pending=100):
    protected_repos={x['repository'] for x in protected}
    protected_diffs={digest(x['diff']) for x in protected}
    blocked=set();blocked_fingerprints=set()
    for link in store.evolution_feedback_links(tenant):
        published=link['status'] in PUBLISHED
        current_pending=(link['status'] in PENDING and
                         link.get('baseline_content_sha256')==baseline_hash and
                         link.get('policy_source_sha256')==source_hash)
        if published or current_pending:
            blocked.update(link.get('feedback_ids',[]))
            blocked_fingerprints.update(link.get('case_fingerprints',[]))
    groups=defaultdict(list);seen=set(); selected=0;scanned=0
    # Paginated database traversal, rather than list_tasks(200). No history deletion.
    for failure in store.iter_feedback_failures(tenant):
        scanned+=1; payload=failure.get('payload') or {}; fid=payload.get('feedback_id')
        if not fid or fid in blocked:continue
        task=store.get(failure['task_id'],tenant)
        if not task or task.get('state')!='SUCCESS':continue
        inp=task.get('input') or {}
        if not inp.get('govern') or inp.get('evaluation') or task.get('repository') in protected_repos:continue
        if digest(store.get_task_payload(task['id']) or '') in protected_diffs:continue
        history=store.load_checkpoints(task['id']).get('govern-feedback',{}).get('state',{}).get('history',[])
        finding_id=(payload.get('finding') or {}).get('finding_id')
        latest=next((f for f in reversed(history) if
                     (f.get('finding') or {}).get('finding_id')==finding_id),None) if finding_id else None
        if not latest or latest['feedback_id']!=fid:continue
        if latest['type'] not in {'false_positive','missed_issue'} or latest['source_stage'] not in roles:continue
        if latest.get('reason_code') in {'tool_unavailable','permission_denied','budget_exhausted','role_timeout','snapshot_missing','environment_error'}:continue
        signature=digest([task['id'],finding_id,latest['type'],latest['source_stage'],latest['reason_code'],latest['reason']])
        if signature in seen or signature in blocked_fingerprints:continue
        seen.add(signature)
        finding=latest['finding']
        example={'feedback_id':fid,'failure_case_id':failure['id'],'task_id':task['id'],'case_fingerprint':signature,
                 'repository':task['repository'],'reason':compact(latest['reason']),
                 'finding':compact({k:finding[k] for k in ('finding_id','path','line','side','title','description',
                     'source_stage','rule_id','evidence_refs','related_tests','impact_scope') if k in finding})}
        groups[(latest['source_stage'],latest['type'],latest['reason_code'])].append(example)
        selected+=1
        if selected>=max_pending:break
    return groups,{'scanned_failure_records':scanned,'eligible_in_window':selected,
                   'pending_window_limit':max_pending,'history_preserved':True}
