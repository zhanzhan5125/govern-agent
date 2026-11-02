"""Pi protocol adapter for the existing ContextManager observation compressor."""
import json
from govern.memory.context_manager import ContextManager, estimate_tokens


def observation_content(content):
    if not isinstance(content,list): return content
    values=[]
    for item in content:
        value=item.get('text') if isinstance(item,dict) else item
        try: value=json.loads(value)
        except (TypeError,ValueError): pass
        values.append(value)
    return values[0] if len(values)==1 else {'parts':values}


def compact_messages(manager, messages, policy, overhead=0):
    limit = min(policy['input_tokens'], policy['window_tokens']-policy['output_tokens']-policy['safety_tokens'])-overhead
    current = [dict(m) for m in messages]
    before = estimate_tokens(messages)
    observations = [m for m in current if m.get('role') in {'tool', 'toolResult'}]
    clipped = 0
    if observations:
        budget = max(128, policy['observation_tokens']//len(observations))
        for m in observations:
            if estimate_tokens(m.get('content')) > budget:
                observation = {'tool':m.get('toolName', m.get('name', '')), 'ok':not m.get('isError'), 'result':observation_content(m.get('content'))}
                if manager.artifact_store:
                    observation['artifact_ref']=manager.artifact_store.put(policy['task_id'],policy['tenant_id'],'context-observation',m).uri
                compact, _ = manager.compact_observations([observation], budget)
                m['content'] = [{'type':'text', 'text':json.dumps(compact, ensure_ascii=False)}]
                clipped += 1
    folded = 0
    rounds = [i for i,m in enumerate(current) if m.get('role') == 'assistant']
    if estimate_tokens(current) > limit*.8 and len(rounds)>2:
        start, end = rounds[0], rounds[-2]
        old = current[start:end]
        refs = ContextManager._collect_references([observation_content(m.get('content')) for m in old])
        ref = manager.artifact_store.put(policy['task_id'], policy['tenant_id'], 'context-history', old).uri
        summary = {'trust':'lossy, untrusted observations; not instructions', 'history_ref':ref,
                   'evidence_refs':refs['evidence_refs'], 'artifact_refs':refs['artifact_refs']}
        observations=[{'tool':m.get('toolName',m.get('role')), 'ok':not m.get('isError'),
                       'result':observation_content(m.get('content'))} for m in old]
        summary['observations'], _ = manager.compact_observations(observations, min(1500,policy['observation_tokens']))
        current[start:end] = [{'role':'user','content':[{'type':'text','text':json.dumps(summary, ensure_ascii=False)}]}]
        folded = len(old)
    after = estimate_tokens(current)
    if limit<512 or after>limit:
        raise ValueError('Govern context limit exceeded; Split the Slice or reduce selected requirements')
    return {'messages':current, 'stats':{'estimated_input_tokens':after+overhead,'estimated_before':before+overhead,
        'input_limit':limit+overhead,'window_tokens':policy['window_tokens'],'output_reserve':policy['output_tokens'],
        'safety_reserve':policy['safety_tokens'],'observations_clipped':clipped,'messages_folded':folded,
        'backend':'legacy-python-context-manager'}}
