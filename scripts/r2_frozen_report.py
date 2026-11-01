"""Reports exclusively from R2.1 receipts, never from R2 simulation outputs."""
from collections import Counter
from pathlib import Path
from scripts.r2_frozen_comparison import (OUT,ROOT,Ledger,read,sha,protected,verify_freeze,
    reconcile,ARMS,fingerprint,encoded,StopRun,VERSION)
from scripts.example_calibration_analysis import material_row,statistics
from scripts.report_expression_selection_r1 import body_without_character
from persona_runtime.config import atomic_json


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


def report(root):
    prepared=verify_freeze(root);is_real=prepared['real'];items={i['key']:i for i in prepared['items']}
    rows=Ledger(root/'receipts').rows() if (root/'receipts').exists() else []
    if any(r['real']!=is_real for r in rows):raise StopRun('mixed_real_simulation_receipts')
    values={p.stem:read(p) for p in (root/'results').glob('*.json')}
    if any(v['real']!=is_real for v in values.values()):raise StopRun('mixed_real_simulation_results')
    successful={(r['item'],r['kind']):r for r in rows if r['status']=='completed'}
    labels=read(root/'labels.json');fixture=read(root/'fixture.json');cases={c['id']:c for c in fixture['cases']}
    complete={k:v for k,v in values.items() if v['turn']['status']=='completed'}
    material=[];manifests={}
    for key,value in complete.items():
        main=next(m for m in value['manifests'] if m['task_type']=='dialogue');manifests[key]=main
        selection=main['prompt_assembly']['material_selection'];item=items[key]
        row=material_row(item,read(Path(item['candidate_file'])),selection,labels)
        row.update(final_input_hash=main['input_snapshot_hash'],hash_matches=main['input_snapshot_hash']==successful[(key,'main')]['input_hash'],
            budget_and_source_diagnostics=main.get('character_retrieval'),
            recovered_by_retry=any(r['attempt_index']>0 for r in rows if r['item']==key),real=is_real)
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
            row['explained']=all(row[k] for k in ('non_character_messages_equal','facts_and_lore_equal','vendor_parameters_equal','candidate_equal','selector_payload_equal','final_hashes_match'))
        row['backend_observations']=metadata
        row.update(classification(full,row.get('main_request_equal',False),row.get('explained',False),row.get('examples_changed',False),row.get('selector_versions_valid',False),metadata))
        row['first_attempt_pair']=full and all(not by_key[k]['recovered_by_retry'] for k in keys)
        diffs.append(row)
        order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        label=f'P{n:02}';mapping[label]=dict(zip('AB',order));case=cases[items[keys[0]]['case']]
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
    result={'version':VERSION,'real':is_real,'target_requests':32,'target_pairs':16,'complete_requests':len(complete),
        'complete_pairs':len(pairs),'complete':len(complete)==32,'calls':len(rows),'limit':256,
        'calls_by_kind':dict(Counter(r['kind'] for r in rows)),'calls_by_status':dict(Counter(r['status'] for r in rows)),
        'retry_calls':sum(r['attempt_index']>0 for r in rows),'recovered_requests':sum(r['recovered_by_retry'] for r in material),
        'unknown_usage_calls':sum(not r.get('usage') for r in rows),'known_usage':dict(usage),'reasoning_is_subset':True,
        'amount':'未知，以网关账单为准' if is_real else '离线模拟，无费用',
        'planned_denominators_per_arm':{'requests':16,'eligible_primary':10,'no_applicable_example':6,'eligible_narrow':5},
        'observed':statistics(material),'matched':statistics(matched),
        'first_attempt_matched':statistics([r for r in matched if next(d['first_attempt_pair'] for d in diffs if d['pair']==items[r['key']]['pair'])]),
        'directional_pairs':[r['pair'] for r in diffs if r['eligible_for_directional_analysis']],
        'all_observed_hashes_match':all(r['hash_matches'] for r in material),
        'all_complete_differences_explained':all(r.get('explained') for r in diffs if r['complete']),
        'real_effect_status':'pending_machine_review' if is_real and len(complete)==32 else 'incomplete' if is_real else 'not_run',
        'naturalness_improvement_proven':False,'paid_judge_calls':0,'qq':0}
    for name,value in (('metrics',result),('material-differences',material),('request-differences',diffs),
                       ('anonymous-comparison',anonymous),('anonymous-map',mapping)):
        atomic_json(root/'analysis'/(name+'.json'),value)
    md=['# R2.1 匿名回复对照','', '真实开发对照，非独立人工盲审。' if is_real else '仅离线模拟，不能作效果证据。','']
    for a in anonymous:
        md+=['## '+a['id'],'','角色反应参考：'+a['role'],'']
        if a['reference']:md+=['引用：'+a['reference']['text'],'']
        md+=['输入：']+a['members']+['','A：',a['A'] or '（未完成）','','B：',a['B'] or '（未完成）','']
    (root/'analysis/anonymous-comparison.md').write_text('\n'.join(md),encoding='utf-8')
    print(encoded({k:result[k] for k in ('real','complete_requests','complete_pairs','calls','retry_calls','real_effect_status')}),flush=True)
    return result


def verify(root):
    protected(full=True);prepared=verify_freeze(root);metrics=report(root)
    rows=Ledger(root/'receipts').check()
    for item in prepared['items']:
        path=root/'results'/(item['key']+'.json')
        if path.exists():reconcile(read(path),[r for r in rows if r['item']==item['key']])
    if not metrics['complete'] or not metrics['all_complete_differences_explained'] or not metrics['all_observed_hashes_match']:raise StopRun('comparison_not_complete')
    if not prepared['real']:
        from tests.prompt_baseline_fixture import capture_all
        oracle=read(ROOT/'tests/fixtures/prompt-baseline-r0.json');actual=capture_all()
        diff=[{'scenario':key,'equal':encoded(oracle['scenarios'].get(key))==encoded(value)} for key,value in actual.items()]
        passed=set(actual)==set(oracle['scenarios']) and all(r['equal'] for r in diff)
        atomic_json(OUT/'r0-differences.json',{'passed':passed,'scenarios':diff,'baseline_sha256':sha(ROOT/'tests/fixtures/prompt-baseline-r0.json')})
        if not passed:raise StopRun('R0_difference')
    value={'passed':True,'real':prepared['real'],'requests':32,'pairs':16,'calls':len(rows),'runtime_receipts_match':True,
        'final_input_hashes_match':True,'source_freeze_matches':True,'old_R2_unchanged':True,'formal_unchanged':True,'qq':0}
    atomic_json(root/'verification.json',value);print(encoded(value),flush=True);return value
