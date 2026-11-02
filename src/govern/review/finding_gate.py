"""Final host-side gate: non-defects are audit observations, not actionable Findings.

References are checked against frozen files; this validates evidence existence, not
the truth of a model's semantic argument. Independent Validator review still matters.
"""
import json
import re


def coverage_text(value):
    if isinstance(value,str):return value
    if isinstance(value,dict):
        label=': '.join(coverage_text(v) for v in (value.get('dimension'),value.get('reason') or value.get('description')) if v)
        if label:return label+(' | evidence: '+json.dumps(value['evidence_refs'],ensure_ascii=False) if value.get('evidence_refs') else '')
    return json.dumps(value,ensure_ascii=False)


def normalize_coverage(coverage):
    result=dict(coverage or {})
    items=result.get('incomplete_dimensions',[])
    result['incomplete_details']=[*result.get('incomplete_details',[]),*[v for v in items if isinstance(v,dict)]]
    result['incomplete_dimensions']=[coverage_text(v) for v in items]
    return result


def valid_code_ref(ref,snapshot,side):
    if isinstance(ref,str):
        prefix='code://'+snapshot['snapshot_id']+'/'+side+'/'
        if not ref.startswith(prefix):return False
        path,sep,location=ref[len(prefix):].rpartition('#L')
        match=re.fullmatch(r'(\d+)(?:-L?(\d+))?',location)
        if not sep or not match:return False
        start=int(match[1]);end=int(match[2] or start)
    elif isinstance(ref,dict):
        if ref.get('snapshot_id')!=snapshot['snapshot_id'] or ref.get('side')!=side:return False
        path=ref.get('path');start=ref.get('start_line',ref.get('line'));end=ref.get('end_line',start)
    else:return False
    if not isinstance(path,str):return False
    text=snapshot.get(side+'_files',{}).get(path)
    return isinstance(text,str) and type(start) is int and type(end) is int and 0<start<=end<=len(text.splitlines())


def finding_rejection(finding,snapshot,units,requirements,policy):
    disposition=finding.get('disposition');cause=finding.get('change_causality') or {}
    if not isinstance(cause,dict):return 'invalid_change_causality'
    status=cause.get('status')
    if disposition and disposition!='actionable':return disposition
    if status in {'fixed_in_head','pre_existing','no_regression','unproven'}:return status
    if finding.get('static_verdict') in {'refuted','not_a_defect','fixed'}:return 'not_a_defect'
    if status!='test_gap':
        if re.search(r'确认无.*回归|无需修改|no regression|no fix required',finding.get('title',''),re.I):return 'no_regression'
        if finding.get('side')=='base' and re.search(r'head\s*版本修复方案正确|head.*(?:已修复|修复方案正确)|head.*fix.*correct',finding.get('recommendation',finding.get('fix','')),re.I):return 'fixed_in_head'
    if disposition!='actionable' or status not in {'introduced','worsened','test_gap'}:return 'needs_evidence'
    unit_ids={u['change_unit_id'] for u in units};linked=cause.get('change_unit_ids')
    if not isinstance(linked,list) or not linked or any(not isinstance(x,str) or x not in unit_ids for x in linked) or not isinstance(cause.get('reasoning'),str) or not cause['reasoning'].strip():return 'invalid_change_causality'
    head_refs=cause.get('head_evidence_refs')
    if not isinstance(head_refs,list) or not head_refs or not all(valid_code_ref(r,snapshot,'head') for r in head_refs):return 'missing_frozen_head_evidence'
    if status=='test_gap':
        gap=finding.get('test_gap') or {}
        if not isinstance(gap,dict) or not all(isinstance(gap.get(k),str) and gap[k].strip() for k in ('expected_behavior','missing_scenario','assertion_source','why_insufficient')):return 'unsupported_test_gap'
        if not isinstance(gap.get('existing_test_evidence_refs'),list) or not gap['existing_test_evidence_refs']:return 'unsupported_test_gap'
        if not all(valid_code_ref(r,snapshot,'head') for r in gap['existing_test_evidence_refs']):return 'unsupported_test_gap'
        sources={r['requirement_id'] for r in requirements}
        sources.update(a['criterion_id'] for r in requirements for a in r.get('acceptance_criteria',[]))
        sources.add('policy://'+policy['sha256'])
        source=gap['assertion_source']
        if not isinstance(source,str) or (source not in sources and not valid_code_ref(source,snapshot,'head')):return 'unsupported_test_gap'
    else:
        linked_units=[u for u in units if u['change_unit_id'] in linked]
        has_base=any(u['path'] in snapshot.get('base_files',{}) for u in linked_units)
        refs=cause.get('base_evidence_refs')
        if has_base and (not isinstance(refs,list) or not refs or not all(valid_code_ref(r,snapshot,'base') for r in refs)):return 'missing_frozen_base_comparison'
    return None
