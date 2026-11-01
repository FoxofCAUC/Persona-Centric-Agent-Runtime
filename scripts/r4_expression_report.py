"""Read-only input analysis; writes only the new R4 package's derived reports."""
from collections import Counter
from scripts.r4_expression_realization import (OUT,ROOT,ARMS,VERSION,LIMIT,read,sha,public,verify_freeze,protected,
    Ledger,ledger_path,StopRun,voice_removed,fingerprint,atomic_json,RuntimePaths,Store,reconcile,validate_request)
from scripts.provider_usage_summary import usage_summary


def attribution(complete,same,explained,metadata):
    reasons=[]
    if not complete:reasons.append('incomplete_pair')
    if same:reasons.append('same_main_request')
    if not explained:reasons.append('non_voice_difference')
    for field in ('response_model','system_fingerprint'):
        values=[m.get(field) for m in metadata]
        if len(values)!=2 or not all(values):reasons.append(field+'_unknown')
        elif values[0]!=values[1]:reasons.append(field+'_changed')
    return {'eligible_for_directional_analysis':not reasons,'limitations':reasons,'effect_proven':False}


def report(root):
    prepared=verify_freeze(root);real=prepared['real'];items={i['key']:i for i in prepared['items']}
    path=ledger_path(root,real);rows=Ledger(path).rows() if path.exists() else []
    rows=[r for r in rows if r['batch']==root.name]
    if any(r['real']!=real for r in rows):raise StopRun('mixed_real_simulation_receipts')
    values={p.stem:read(p) for p in (root/'results').glob('*.json')}
    if any(v['real']!=real for v in values.values()):raise StopRun('mixed_real_simulation_results')
    complete={k:v for k,v in values.items() if v['turn']['status']=='completed'}
    successful={r['item']:r for r in rows if r['status']=='completed'}
    if len(successful)!=sum(r['status']=='completed' for r in rows):raise StopRun('successful_position_replayed')
    data=read(root/'fixture.json');cases={c['id']:c for c in data['cases']};pairs=[];anonymous=[];mapping={};materials=[]
    for key,value in complete.items():
        if key not in successful:raise StopRun('success_receipt_missing')
        main=next(m for m in value['manifests'] if m['task_type']=='dialogue')
        call=successful[key];validate_request(items[key],call['request'],main)
        materials.append({'key':key,'material':main['prompt_assembly']['material_selection'],
            'truncation':main.get('truncation_info'),'final_input_hash':call['input_hash'],
            'actual_voice':main['prompt_assembly']['effective']['expression'],
            'physical_calls':sum(r['item']==key for r in rows),'recovered':call['attempt_index']>0})
    for n,pair in enumerate(dict.fromkeys(i['pair'] for i in prepared['items']),1):
        keys=[pair+'-'+arm for arm in ARMS];full=all(k in complete for k in keys);explained=False;same=False
        if full:
            calls=[successful[k] for k in keys];manifests=[next(m for m in complete[k]['manifests'] if m['task_type']=='dialogue') for k in keys]
            controls=[voice_removed(c['request'],m,items[k]['voice']) for c,m,k in zip(calls,manifests,keys)]
            explained=controls[0]==controls[1];same=calls[0]['request']==calls[1]['request']
        metadata=[successful.get(k,{}).get('diagnostics') or {} for k in keys]
        row={'pair':pair,'complete':full,'same_main_request':same,'only_voice_difference':explained,'backend':metadata,
            **attribution(full,same,explained,metadata),'real':real,'primary':items[keys[0]]['case'] in read(root/'rubric.json')['primary_cases']}
        pairs.append(row)
        order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        label=f'Q{n:02}';mapping[label]=dict(zip('AB',order));case=cases[items[keys[0]]['case']]
        anonymous.append({'id':label,'members':case['members'],'reference':case['reference'],'history':case.get('history',[]),
            'examples':[e for e in data['characters'][items[keys[0]]['role']]['examples'] if e['id'] in items[keys[0]]['selected_examples']],
            **{side:complete[key]['turn']['reply'] if key in complete else None for side,key in zip('AB',order)}})
    stats={'version':prepared['version'],'real':real,'target_requests':48,'complete_requests':len(complete),'complete_pairs':sum(p['complete'] for p in pairs),
        'calls':len(rows),'stage_limit':prepared['generation_limit'],
        'stage_limit_mode':'unlimited' if prepared['generation_limit'] is None else 'bounded',
        'status_counts':dict(Counter(r['status'] for r in rows)),
        'retry_calls':sum(r['attempt_index']>0 for r in rows),'recovered_requests':sum(r['recovered'] for r in materials),
        'eligible_primary_pairs':[p['pair'] for p in pairs if real and p['primary'] and p['eligible_for_directional_analysis']],
        'simulated_attribution_handling_only':not real,
        'all_complete_differences_explained':all(p['only_voice_difference'] for p in pairs if p['complete']),
        'real_effect_status':'pending_machine_review' if real else 'not_run','paid_judge_calls':0,'qq':0,**usage_summary(rows)}
    if not real:stats['amount']='离线模拟，无费用'
    target=root/'analysis';target.mkdir(exist_ok=True)
    for name,value in (('metrics',stats),('request-differences',pairs),('material-differences',materials),('anonymous-comparison',anonymous),('anonymous-map',mapping)):
        atomic_json(target/(name+'.json'),value)
    lines=['# R4-A 匿名开发对照','', '机器开发分析材料，不是独立人工盲审。' if real else '离线模拟，不可用于评价真实效果。','']
    for row in anonymous:
        lines += ['## '+row['id'],'','输入：']+row['members']
        for history in row['history']:lines+=['','已有对话：',history['text'],history['reply']]
        if row['reference']:lines+=['','引用：'+row['reference']['reply']]
        lines+=['','实际样例：']+[e['text'] for e in row['examples']]+['','A：',row['A'] or '未完成','','B：',row['B'] or '未完成','']
    (target/'anonymous-comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    print(__import__('json').dumps(stats,ensure_ascii=False),flush=True);return stats


def verify(root):
    protected(full=True);prepared=verify_freeze(root);stats=report(root)
    rows=Ledger(ledger_path(root,prepared['real'])).check();active=[]
    for item in prepared['items']:
        value=read(root/'results'/(item['key']+'.json'))
        calls=[r for r in rows if r['batch']==root.name and r['item']==item['key']];reconcile(value,calls)
        if value['selection_attempts']:raise StopRun('unexpected_selection_call')
        with Store(RuntimePaths(__import__('pathlib').Path(item['runtime']))).connection() as db:
            active += [dict(r) for r in db.execute("SELECT turn_id,state FROM quota_reservations WHERE state='active'")]
    passed=stats['complete_requests']==48 and stats['complete_pairs']==24 and stats['all_complete_differences_explained'] and not active
    result={'passed':passed,'real':prepared['real'],'requests':stats['complete_requests'],'calls':stats['calls'],'active_reservations':len(active),
        'old_evidence_unchanged':True,'formal_unchanged':True,'real_effect_status':'not_evaluated_by_integrity_verifier'}
    atomic_json(root/'verification.json',result)
    if not passed:raise StopRun('verification_failed')
    return result
