"""Offline annotation validation and simulated material accounting; no quality judge."""
import hashlib
from persona_runtime.archive import fingerprint

ARMS={'control':'character-selector-v3','experiment':'character-selector-v4'}


def body_hash(text):return hashlib.sha256(text.encode()).hexdigest()


def validate_labels(data,annotation):
    cases={c['id']:c for c in data['cases']}
    bodies={(role,e['id']):e['text'] for role,char in data['characters'].items() for e in char['examples']}
    expected={(c,role,key) for c in cases for role,key in bodies};seen=set()
    for r in annotation['rows']:
        key=(r['case_id'],r['role_id'],r['material_id'])
        if key not in expected or key in seen:raise ValueError('label_key_invalid')
        seen.add(key);text=bodies[key[1:]];case=cases[key[0]]
        if r['body_hash']!=body_hash(text):raise ValueError('label_body_changed')
        if type(r['allowed']) is not bool:raise ValueError('label_type')
        if not r['sample_quote'] or r['sample_quote'] not in text:raise ValueError('sample_quote_invalid')
        if not r['transferable_focus'] or r['transferable_focus'] not in text:raise ValueError('focus_quote_invalid')
        if r['input_quotes']!=case['members'] or r['reference_quote']!=(case['reference'] or {}).get('text'):raise ValueError('input_quote_invalid')
        if not all(r[k] for k in ('required_premise','input_support','reason','forbidden_facts')):raise ValueError('label_evidence_missing')
    if seen!=expected:raise ValueError('label_missing')
    if set(cases)!=set(annotation['case_rules']):raise ValueError('case_rules_missing')
    lore={l['id'] for c in data['characters'].values() for l in c['lorebook']}
    for rule in annotation['case_rules'].values():
        a,b=set(rule['allowed_lore']),set(rule['excluded_lore'])
        if a&b or a|b!=lore:raise ValueError('lore_rules_invalid')
    return {'cases':len(cases),'roles':len(data['characters']),'labels':len(seen),'body_keyed':True}


def material_row(item,pool,observation,labels):
    rules=[r for r in labels['rows'] if r['case_id']==item['case'] and r['role_id']==item['role']]
    sources={r['id']:r for r in pool['payload']['sources'] if r['kind']=='example'}
    for r in rules:
        if r['material_id'] in sources and r['body_hash']!=body_hash(sources[r['material_id']]['text']):raise ValueError('candidate_label_body_changed')
    allowed={r['material_id'] for r in rules if r['allowed']};roots=set(pool['payload']['roots']['example'])
    returned=observation.get('returned_example_ids');included=observation['included_example_ids']
    return {'key':item['key'],'case':item['case'],'role':item['role'],'arm':item['arm'],
        'requested_prompt':observation.get('requested_prompt'),'actual_prompt':observation.get('applied_prompt'),
        'valid_selector':observation.get('applied_prompt')==ARMS[item['arm']] and observation.get('effective_mode')=='hybrid_llm',
        'candidate_examples':list(pool['payload']['roots']['example']),'allowed_in_authority':sorted(allowed),
        'available':sorted(allowed&roots),'missing_candidates':sorted(allowed-roots),
        'returned':returned,'included':included,'applicable_selected':sorted(allowed&set(returned or [])),
        'applicable_included':sorted(allowed&set(included)),
        'inapplicable_selected':None if returned is None else sorted(set(returned)-allowed),
        'inapplicable_included':sorted(set(included)-allowed),'dropped':sorted(set(returned or [])-set(included)),
        'fallback':observation.get('fallback_reason'),'lore':observation['lore_ids'],
        'unrelated_lore':sorted(set(observation['lore_ids'])&set(labels['case_rules'][item['case']]['excluded_lore'])),
        'selection_attempt_id':observation.get('selection_attempt_id')}


def statistics(rows):
    result={}
    for arm in ARMS:
        all_rows=[r for r in rows if r['arm']==arm];valid=[r for r in all_rows if r['valid_selector']]
        eligible=[r for r in valid if r['available']]
        result[arm]={'requests':len(all_rows),'valid_selector_requests':len(valid),'fallback_requests':len(all_rows)-len(valid),
            'eligible_requests':len(eligible),'selected_requests':sum(bool(r['applicable_selected']) for r in eligible),
            'included_requests':sum(bool(r['applicable_included']) for r in eligible),
            'inapplicable_selected_requests':sum(bool(r['inapplicable_selected']) for r in valid),
            'inapplicable_included_requests':sum(bool(r['inapplicable_included']) for r in valid),
            'no_applicable_authority':sum(not r['allowed_in_authority'] for r in valid),
            'no_applicable_candidate':sum(bool(r['allowed_in_authority']) and not r['available'] for r in valid),
            'partly_missing_candidates':sum(bool(r['missing_candidates']) for r in valid),
            'dropped_requests':sum(bool(r['dropped']) for r in valid),'unrelated_lore_requests':sum(bool(r['unrelated_lore']) for r in valid)}
    return result
