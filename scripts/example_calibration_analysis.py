"""Pure R2 evaluation helpers. Annotation files never enter request assembly."""
from collections import Counter
from persona_runtime.archive import fingerprint
from scripts.report_expression_selection_r1 import body_without_character
from tests.example_calibration_fixture import text_hash


def validate_labels(data,labels,rubric):
    cases={c['id']:c for c in data['cases']}
    bodies={(role,e['id']):e['text'] for role,p in data['characters'].items() for e in p['examples']}
    expected={(c,role,key) for c in cases for role,key in bodies}
    seen=set()
    for row in labels['rows']:
        key=(row['case_id'],row['role_id'],row['material_id'])
        if key in seen or key not in expected:raise ValueError('label_key_invalid')
        seen.add(key)
        if row['body_hash']!=text_hash(bodies[key[1:]]):raise ValueError('label_body_changed')
        if type(row['allowed']) is not bool or type(row['narrow_allowed']) is not bool:raise ValueError('label_type')
        if row['narrow_allowed'] and not row['allowed']:raise ValueError('narrow_outside_primary')
        if not row['reason'] or not row['intent'] or not row['forbidden_facts']:raise ValueError('label_evidence_missing')
    if seen!=expected:raise ValueError('label_missing')
    if set(labels['case_rules'])!=set(cases):raise ValueError('case_rules_missing')
    lore={e['id'] for p in data['characters'].values() for e in p['lorebook']}
    for rule in labels['case_rules'].values():
        a,b=set(rule['allowed_lore']),set(rule['excluded_lore'])
        if a&b or a|b!=lore:raise ValueError('lore_labels_invalid')
    if set(rubric['categories'])!={'natural_transfer','fitting_reuse','forced_template','fact_transplant'}:raise ValueError('reuse_categories_invalid')
    for row in rubric['authored_reading_cases']:
        case=cases[row['case_id']];source=bodies[row['role_id'],row['material_id']]
        text='\n'.join(case['members'])+'\n'+((case['reference'] or {}).get('text',''))
        if not row['input_quote'] or row['input_quote'] not in text:raise ValueError('reading_input_quote_invalid')
        if not row['sample_quote'] or row['sample_quote'] not in source:raise ValueError('reading_sample_quote_invalid')
        if not row['reply_quote'] or row['reply_quote'] not in row['reply']:raise ValueError('reading_reply_quote_invalid')
        if row['category'] not in rubric['categories'] or not row['reason']:raise ValueError('reading_reason_missing')
    return {'labels':len(seen),'cases':len(cases),'roles':len(data['characters']),'reading_cases':len(rubric['authored_reading_cases'])}


def material_row(item,pool,observation,labels):
    rules=[r for r in labels['rows'] if r['case_id']==item['case'] and r['role_id']==item['role']]
    sources={r['id']:r for r in pool['payload']['sources'] if r['kind']=='example'}
    for r in rules:
        if r['material_id'] in sources and r['body_hash']!=text_hash(sources[r['material_id']]['text']):raise ValueError('candidate_label_body_changed')
    result={'key':item['key'],'case':item['case'],'role':item['role'],'arm':item['arm'],
        'candidate_examples':pool['payload']['roots']['example'],
        'returned':observation.get('returned_example_ids'),'included':observation['included_example_ids'],
        'fallback':observation.get('fallback_reason'),'actual_prompt':observation.get('applied_prompt'),
        'selection_attempt_id':observation.get('selection_attempt_id'),
        'lore':observation['lore_ids'],'unrelated_lore':sorted(set(observation['lore_ids'])&set(labels['case_rules'][item['case']]['excluded_lore']))}
    for name,field in (('primary','allowed'),('narrow','narrow_allowed')):
        allowed={r['material_id'] for r in rules if r[field]};roots=set(result['candidate_examples'])
        returned=set(result['returned'] or []);included=set(result['included'])
        result[name]={'allowed_in_authority':sorted(allowed),'available':sorted(allowed&roots),
            'missing_candidates':sorted(allowed-roots),'selected':sorted(allowed&returned),'included':sorted(allowed&included),
            'inapplicable_selected':sorted(returned-allowed),'inapplicable_included':sorted(included-allowed),
            'selected_then_dropped':sorted(returned-included),
            'no_applicable_authority':not allowed,'no_applicable_candidate':bool(allowed and not allowed&roots)}
    return result


def statistics(rows):
    result={}
    for scoring in ('primary','narrow'):
        result[scoring]={}
        for arm in ('control','experiment'):
            observed=[r for r in rows if r['arm']==arm]
            valid=[r for r in observed if r['actual_prompt']=='character-selector-'+('v2' if arm=='control' else 'v3')]
            eligible=[r for r in valid if r[scoring]['available']]
            result[scoring][arm]={'requests':len(observed),'valid_selector_requests':len(valid),'fallback_requests':len(observed)-len(valid),
                'eligible_requests':len(eligible),'applicable_candidate_items':sum(len(r[scoring]['available']) for r in valid),
                'selected_requests':sum(bool(r[scoring]['selected']) for r in eligible),'included_requests':sum(bool(r[scoring]['included']) for r in eligible),
                'selected_items':sum(len(r[scoring]['selected']) for r in valid),'included_items':sum(len(r[scoring]['included']) for r in valid),
                'inapplicable_selected_requests':sum(bool(r[scoring]['inapplicable_selected']) for r in valid),
                'inapplicable_included_requests':sum(bool(r[scoring]['inapplicable_included']) for r in valid),
                'dropped_requests':sum(bool(r[scoring]['selected_then_dropped']) for r in valid),
                'no_applicable_authority':sum(r[scoring]['no_applicable_authority'] for r in valid),
                'no_applicable_candidate':sum(r[scoring]['no_applicable_candidate'] for r in valid),
                'partly_missing_candidates':sum(bool(r[scoring]['missing_candidates']) for r in valid),
                'unrelated_lore_requests':sum(bool(r['unrelated_lore']) for r in valid)}
    return result


def attribution(complete,request_equal,explained,examples_changed,backends):
    """Categorize evidence, never turn a different reply into a causal win."""
    if not complete:return 'incomplete'
    if request_equal:return 'identical_request_output_difference_only'
    if not explained:return 'confounded_request'
    if not all(backends):return 'backend_unknown'
    if backends[0]!=backends[1]:return 'backend_changed'
    if examples_changed:return 'examples_only_directional_analysis_possible'
    return 'unexplained_difference'


def pair_difference(pair,items,values,calls):
    row={'pair':pair,'effect_proven':False,'quality_verdict':None}
    keys=[pair+'-'+a for a in ('control','experiment')]
    complete=all(k in values and values[k]['turn']['status']=='completed' and (k,'main') in calls for k in keys)
    row['complete']=complete
    if not complete:return row|{'classification':'incomplete'}
    manifests=[next(m for m in values[k]['manifests'] if m['task_type']=='dialogue') for k in keys]
    physical=[calls[(k,'main')] for k in keys]
    parts=[body_without_character(c['request']['messages'],m) for c,m in zip(physical,manifests)]
    params=[{k:v for k,v in c['request'].items() if k!='messages'} for c in physical]
    ia,ib=[items[k] for k in keys]
    row.update(non_character_messages_equal=parts[0][0]==parts[1][0],facts_and_lore_equal=parts[0][1]==parts[1][1],
        examples_changed=parts[0][2]!=parts[1][2],vendor_parameters_equal=params[0]==params[1],
        candidate_equal=ia['candidate_hash']==ib['candidate_hash'],selector_input_equal=ia['selector_messages'][1:]==ib['selector_messages'][1:],
        main_request_equal=physical[0]['request']==physical[1]['request'],
        final_hashes_match=all(m['input_snapshot_hash']==fingerprint(c['request']['messages'])==m['prompt_assembly']['input_snapshot_hash'] for m,c in zip(manifests,physical)))
    row['explained']=all(row[k] for k in ('non_character_messages_equal','facts_and_lore_equal','vendor_parameters_equal','candidate_equal','selector_input_equal','final_hashes_match'))
    row['backend_fingerprints']=[c.get('diagnostics',{}).get('system_fingerprint') for c in physical]
    row['classification']=attribution(True,row['main_request_equal'],row['explained'],row['examples_changed'],row['backend_fingerprints'])
    row['backend_status']='unknown' if not all(row['backend_fingerprints']) else 'same' if len(set(row['backend_fingerprints']))==1 else 'changed'
    row['observed_outputs_differ']=physical[0]['output']!=physical[1]['output']
    row['simulation']=True
    return row
