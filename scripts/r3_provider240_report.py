"""Provider/240-second continuation reports. Old cohort stays immutable and distinct."""
from collections import Counter
from pathlib import Path
from scripts.r3_provider240 import (OUT,OLD,ROOT,Ledger,read,sha,protected,verify_freeze,
    reconcile,ARMS,fingerprint,encoded,StopRun,VERSION)
from scripts.r3_prerequisite_analysis import material_row,statistics
from scripts.report_expression_selection_r1 import body_without_character
from persona_runtime.config import atomic_json


def boundary_statistics(rows,evaluation):
    conflicts={}
    for r in evaluation['premise_conflicts']:
        conflicts.setdefault(r['case_id']+'-'+r['role_id'],set()).add(r['material_id'])
    result={}
    for arm in ARMS:
        valid=[r for r in rows if r['arm']==arm and r['valid_selector']]
        result[arm]={'observed_valid_requests':len(valid),
          'premise_conflict_returned_requests':sum(bool(set(r['returned'] or [])&conflicts.get(r['case']+'-'+r['role'],set())) for r in valid),
          'premise_conflict_included_requests':sum(bool(set(r['included'])&conflicts.get(r['case']+'-'+r['role'],set())) for r in valid),
          'preservation':{group:{'observed_requests':sum(r['case']+'-'+r['role'] in pairs for r in valid),
             'any_applicable_included':sum(bool(r['applicable_included']) for r in valid if r['case']+'-'+r['role'] in pairs),
             'target_included':sum(bool(set(r['included'])&set(pairs[r['case']+'-'+r['role']])) for r in valid if r['case']+'-'+r['role'] in pairs)}
             for group,pairs in evaluation['preservation_targets'].items()}}
    return result


def selection_direction(rows,evaluation,complete):
    stats=statistics(rows);bounds=boundary_statistics(rows,evaluation);by={(r['case']+'-'+r['role'],r['arm']):r for r in rows}
    losses=[];target_replacements=[]
    for group,pairs in evaluation['preservation_targets'].items():
        for pair,ids in pairs.items():
            a,b=by.get((pair,'control')),by.get((pair,'experiment'))
            if not a or not b or not a['valid_selector'] or not b['valid_selector']:continue
            if a['applicable_included'] and not b['applicable_included']:losses.append({'group':group,'pair':pair})
            if set(a['included'])&set(ids) and not set(b['included'])&set(ids):target_replacements.append({'group':group,'pair':pair,'alternative_applicable':b['applicable_included']})
    c,e=bounds['control'],bounds['experiment'];a,b=stats['control'],stats['experiment']
    checks={'complete':complete,'premise_conflict_reduced':e['premise_conflict_included_requests']<c['premise_conflict_included_requests'],
       'all_inapplicable_not_increased':b['inapplicable_included_requests']<=a['inapplicable_included_requests'],
       'applicable_not_decreased':b['included_requests']>=a['included_requests'],
       'no_preservation_case_loss':not losses,'unrelated_lore_not_increased':b['unrelated_lore_requests']<=a['unrelated_lore_requests']}
    return {'checks':checks,'numerical_conditions_met':all(checks.values()),'preservation_losses':losses,
       'target_replacements_requiring_reading':target_replacements,'hard_boundary_reading':'pending',
       'causal_improvement_proven':False,'note':'These are descriptive counts; backend and quoted reply review remain separate gates.'}


def classification(complete,same,explained,changed,versions_valid,metadata):
    reasons=[]
    if not complete:reasons.append('incomplete_pair')
    if not versions_valid:reasons.append('selector_not_effective')
    if same:reasons.append('identical_main_request')
    if not explained:reasons.append('non_example_difference_or_missing_evidence')
    if not changed and not same:reasons.append('unexplained_main_difference')
    for kind,values in metadata.items():
        for field in ('response_model','system_fingerprint'):
            a,b=[v.get(field) for v in values]
            if not a or not b:reasons.append(kind+'_'+field+'_unknown')
            elif a!=b:reasons.append(kind+'_'+field+'_changed')
    return {'eligible_for_directional_analysis':not reasons,'limitations':reasons,'effect_proven':False}


def report(root=None,*,combined=True):
    root=root or OUT/'real'
    prepared=verify_freeze(root);is_real=prepared['real'];combined=combined and is_real
    items={i['key']:i for i in prepared['items']}
    rows=Ledger(root/'receipts').rows() if (root/'receipts').exists() else []
    if any(r['real']!=is_real for r in rows):raise StopRun('mixed_real_simulation_receipts')
    values={p.stem:read(p) for p in (root/'results').glob('*.json')}
    if any(v['real']!=is_real for v in values.values()):raise StopRun('mixed_real_simulation_results')
    new_rows=list(rows);new_values=dict(values)
    if combined:
        old_prepared=read(OLD/'real/prepared.json')
        merged={i['key']:dict(i,cohort='r3-real-v1') for i in old_prepared['items']}
        merged.update(items);items=merged
        prepared=dict(prepared,items=list(merged.values()))
        old_rows=[dict(r,cohort='r3-real-v1') for r in Ledger(OLD/'real/receipts').rows()]
        old_values={p.stem:read(p) for p in (OLD/'real/results').glob('*.json')}
        if any(not r['real'] for r in old_rows) or any(not v['real'] for v in old_values.values()):raise StopRun('old_cohort_not_real')
        values={k:v for k,v in old_values.items() if v['turn']['status']=='completed'}|values
        rows=old_rows+rows
    target=len(items);target_pairs=len({i['pair'] for i in items.values()})
    successful={(r['item'],r['kind']):r for r in rows if r['status']=='completed'}
    labels=read(root/'labels.json');fixture=read(root/'fixture.json');evaluation=read(root/'evaluation.json');cases={c['id']:c for c in fixture['cases']}
    complete={k:v for k,v in values.items() if v['turn']['status']=='completed'}
    material=[];manifests={}
    for key,value in complete.items():
        main=next(m for m in value['manifests'] if m['task_type']=='dialogue');manifests[key]=main
        selection=main['prompt_assembly']['material_selection'];item=items[key]
        row=material_row(item,read(Path(item['candidate_file'])),selection,labels)
        row.update(final_input_hash=main['input_snapshot_hash'],hash_matches=main['input_snapshot_hash']==successful[(key,'main')]['input_hash'],
            budget_and_source_diagnostics=main.get('character_retrieval'),
            recovered_by_retry=any(r['attempt_index']>0 for r in rows if r['item']==key and r.get('cohort')==item.get('cohort')),real=is_real,cohort=item.get('cohort'))
        row['returned_lore_ids']=__import__('json').loads(successful[(key,'selector')]['output'])['lore_ids']
        material.append(row)
    by_key={r['key']:r for r in material};diffs=[];anonymous=[];mapping={}
    for n,pair in enumerate(dict.fromkeys(i['pair'] for i in prepared['items']),1):
        keys=[pair+'-'+arm for arm in ARMS];full=all(k in complete for k in keys)
        row={'pair':pair,'complete':full,'real':is_real,'quality_verdict':None}
        metadata={kind:[successful.get((key,kind),{}).get('diagnostics') or {} for key in keys] for kind in ('selector','main')}
        if full:
            ca,cb=[successful[(key,'main')] for key in keys];ma,mb=[manifests[key] for key in keys]
            pa,pb=[body_without_character(c['request']['messages'],m) for c,m in ((ca,ma),(cb,mb))]
            row.update(non_character_messages_equal=pa[0]==pb[0],facts_and_lore_equal=pa[1]==pb[1],examples_changed=pa[2]!=pb[2],
                vendor_parameters_equal={k:v for k,v in ca['request'].items() if k!='messages'}=={k:v for k,v in cb['request'].items() if k!='messages'},
                candidate_equal=items[keys[0]]['candidate_hash']==items[keys[1]]['candidate_hash'],
                selector_payload_equal=items[keys[0]]['selector_messages'][1:]==items[keys[1]]['selector_messages'][1:],
                main_request_equal=ca['request']==cb['request'],final_hashes_match=all(by_key[k]['hash_matches'] for k in keys),
                selector_versions_valid=all(by_key[k]['actual_prompt']==ARMS[items[k]['arm']] for k in keys))
            row['fixed_facts_equal']=[p for p in pa[1] if p['kind']!='lore_group']==[p for p in pb[1] if p['kind']!='lore_group']
            row['integrity_explained']=all(row[k] for k in ('non_character_messages_equal','fixed_facts_equal','vendor_parameters_equal','candidate_equal','selector_payload_equal','final_hashes_match'))
            row['explained']=row['integrity_explained'] and row['facts_and_lore_equal']
            if not row['examples_changed'] and row['facts_and_lore_equal'] and not row['main_request_equal']:
                row['integrity_explained']=row['explained']=False
        row['cohorts']=[items[k].get('cohort') for k in keys]
        row['same_cohort']=len(set(row['cohorts']))==1
        if full and not row['same_cohort']:row['explained']=row['integrity_explained']=False
        row['backend_observations']=metadata
        row.update(classification(full,row.get('main_request_equal',False),row.get('explained',False),row.get('examples_changed',False),row.get('selector_versions_valid',False),metadata))
        row['first_attempt_pair']=full and all(not by_key[k]['recovered_by_retry'] for k in keys)
        diffs.append(row)
        order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        label=f'Q{n:02}';mapping[label]=dict(zip('AB',order));case=cases[items[keys[0]]['case']]
        anonymous.append({'id':label,'members':case['members'],'reference':case['reference'],'role':items[keys[0]]['role'],
            **{side:complete[key]['turn']['reply'] if key in complete else None for side,key in zip('AB',order)},
            'statuses':{side:values[key]['turn']['status'] if key in values else 'not_completed' for side,key in zip('AB',order)}})
    pairs={r['pair'] for r in diffs if r['complete']}
    matched=[r for r in material if items[r['key']]['pair'] in pairs]
    usage=Counter()
    for row in rows:
        u=row.get('usage') or {}
        for key in ('prompt_tokens','completion_tokens','total_tokens'):usage[key]+=u.get(key,0)
        usage['reasoning_tokens']+=u.get('completion_tokens_details',{}).get('reasoning_tokens',0)
    result={'version':VERSION,'real':is_real,'target_requests':target,'target_pairs':target_pairs,'complete_requests':len(complete),
        'complete_pairs':len(pairs),'complete':len(complete)==target,'calls':len(rows),'limit':384,
        'calls_by_kind':dict(Counter(r['kind'] for r in rows)),'calls_by_status':dict(Counter(r['status'] for r in rows)),
        'retry_calls':sum(r['attempt_index']>0 for r in rows),'recovered_requests':sum(r['recovered_by_retry'] for r in material),
        'unknown_usage_calls':sum(not r.get('usage') for r in rows),'known_usage':dict(usage),'reasoning_is_subset':True,
        'amount':'未知，以网关账单为准' if is_real else '离线模拟，无费用',
        'planned_denominators_per_arm':{'requests':target_pairs,'applicable_candidates':22 if combined else 15,'no_applicable_authority':1,'missing_only_applicable_candidate':1},
        'observed':statistics(material),'matched':statistics(matched),
        'boundary_statistics':boundary_statistics(matched,evaluation),
        'selection_direction':selection_direction(matched,evaluation,len(pairs)==target_pairs) if is_real else None,
        'first_attempt_matched':statistics([r for r in matched if next(d['first_attempt_pair'] for d in diffs if d['pair']==items[r['key']]['pair'])]),
        'directional_pairs':[r['pair'] for r in diffs if r['eligible_for_directional_analysis']],
        'all_observed_hashes_match':all(r['hash_matches'] for r in material),
        'all_observed_versions_valid':all(r['valid_selector'] for r in material),
        'all_complete_differences_explained':all(r.get('explained') for r in diffs if r['complete']),
        'all_integrity_checks_passed':all(r.get('integrity_explained') for r in diffs if r['complete']),
        'real_effect_status':'pending_machine_review' if is_real and len(complete)==target else 'incomplete' if is_real else 'not_run',
        'naturalness_improvement_proven':False,'paid_judge_calls':0,'qq':0}
    partial=[]
    for key,value in values.items():
        if key in complete:continue
        partial.append({'key':key,'turn_status':value['turn']['status'],'error_code':value['turn'].get('error_code'),
            'selector_completed':(key,'selector') in successful,'main_completed':(key,'main') in successful,
            'selection_result':successful.get((key,'selector'),{}).get('output'),
            'saved_dialogue_manifests':[m for m in value['manifests'] if m['task_type']=='dialogue'],
            'included_in_arm_effect_statistics':False})
    result['partial_requests']=len(partial)
    result['cohort_note']='原批次阻断保持；新授权更换供应商并延长选择等待，只补34条。合并统计仅描述结果，各配对在同一供应商/等待条件内比较。' if combined else '新供应商240秒独立补充包'
    result['combined']=combined
    result['new_calls']=len(new_rows)
    result['old_calls']=35 if combined else 0
    result['new_complete_requests']=sum(v['turn']['status']=='completed' for v in new_values.values())
    result['cohorts']={cohort:{'calls':sum(r.get('cohort')==cohort for r in rows),
        'requests':sum(r.get('cohort')==cohort for r in material),
        'matched_statistics':statistics([r for r in matched if r.get('cohort')==cohort]),
        'boundary_statistics':boundary_statistics([r for r in matched if r.get('cohort')==cohort],evaluation)}
        for cohort in dict.fromkeys(i.get('cohort') for i in items.values())}
    if combined:
        result['old_failed_request_retained']={'key':'P04-stance-experiment','source':str(OLD/'real/results/P04-stance-experiment.json'),'excluded_from_effect_denominators':True}
    analysis=root/'analysis' if combined or not is_real else root/'analysis-new'
    analysis.mkdir(exist_ok=True)

    for name,value in (('metrics',result),('material-differences',material),('request-differences',diffs),
                       ('anonymous-comparison',anonymous),('anonymous-map',mapping),('partial-requests',partial)):
        atomic_json(analysis/(name+'.json'),value)
    md=['# R3.1 匿名回复对照','', '真实开发对照，非独立人工盲审。' if is_real else '仅离线模拟，不能作效果证据。','']
    for a in anonymous:
        md+=['## '+a['id'],'','角色反应参考：'+a['role'],'']
        if a['reference']:md+=['引用：'+a['reference']['text'],'']
        md+=['输入：']+a['members']+['','A：',a['A'] or '（未完成）','','B：',a['B'] or '（未完成）','']
    (analysis/'anonymous-comparison.md').write_text('\n'.join(md),encoding='utf-8')
    print(encoded({k:result[k] for k in ('real','complete_requests','complete_pairs','calls','retry_calls','real_effect_status')}),flush=True)
    return result


def verify(root):
    protected(full=True);prepared=verify_freeze(root);metrics=report(root,combined=False)
    rows=Ledger(root/'receipts').check()
    for item in prepared['items']:
        path=root/'results'/(item['key']+'.json')
        if path.exists():
            value=read(path);calls=[r for r in rows if r['item']==item['key']];reconcile(value,calls)
            if value['turn']['status']=='completed':
                from scripts.r3_provider240 import validate_actual_material
                m=next(m for m in value['manifests'] if m['task_type']=='dialogue')
                main=next(r for r in calls if r['kind']=='main' and r['status']=='completed')
                selected=next(r for r in calls if r['kind']=='selector' and r['status']=='completed')
                material=m['prompt_assembly']['material_selection']
                if material['selection_attempt_id'] not in {s['id'] for s in value['selection_attempts']}:raise StopRun('selection_link_mismatch')
                if material['returned_example_ids']!=__import__('json').loads(selected['output'])['example_ids']:raise StopRun('returned_selection_mismatch')
                validate_actual_material(root,item,main['request']['messages'],m)
    if not metrics['complete'] or not metrics['all_integrity_checks_passed'] or not metrics['all_observed_hashes_match'] or not metrics['all_observed_versions_valid']:raise StopRun('comparison_not_complete')
    if not prepared['real']:
        from tests.prompt_baseline_fixture import capture_all
        oracle=read(ROOT/'tests/fixtures/prompt-baseline-r0.json');actual=capture_all()
        diff=[{'scenario':key,'equal':encoded(oracle['scenarios'].get(key))==encoded(value)} for key,value in actual.items()]
        passed=set(actual)==set(oracle['scenarios']) and all(r['equal'] for r in diff)
        atomic_json(OUT/'r0-differences.json',{'passed':passed,'scenarios':diff,'baseline_sha256':sha(ROOT/'tests/fixtures/prompt-baseline-r0.json')})
        if not passed:raise StopRun('R0_difference')
    value={'passed':True,'real':prepared['real'],'requests':34,'pairs':17,'calls':len(rows),'runtime_receipts_match':True,
        'final_input_hashes_match':True,'source_freeze_matches':True,'old_R3_1_unchanged':True,'formal_matches_authorized_baseline':True,'qq':0}
    atomic_json(root/'verification.json',value);print(encoded(value),flush=True);return value
