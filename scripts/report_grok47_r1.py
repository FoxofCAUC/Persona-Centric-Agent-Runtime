"""Read saved artificial Grok evidence. No dispatch, resume or result replacement."""
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.grok47_r1_gate import EVIDENCE,GATE
from scripts.expression_selection_r1 import read,sha,Store,RuntimePaths,ARMS,fingerprint
from scripts.retry_expression_selection_r1 import ReceiptLedger
from scripts.report_expression_selection_r1 import report as full_report,body_without_character
from persona_runtime.config import atomic_json


def usage(rows):
    result=Counter()
    for row in rows:
        value=row.get('usage') or {}
        for key in ('prompt_tokens','completion_tokens','total_tokens'):
            result[key]+=value.get(key,0)
        result['reasoning_tokens']+=value.get('completion_tokens_details',{}).get('reasoning_tokens',0)
    return dict(result)


def partial_analysis(root,prepared,completed,rows):
    """Only completed, dispatched main requests enter material/effect counts."""
    cases={c['id']:c for c in read(root/'fixture.json')['cases']}
    labels=read(EVIDENCE/'lenient-labels.json')['allowed'];plan=read(EVIDENCE/'analysis-plan.json')
    assert sha(root/'fixture.json')==plan['strict_fixture_sha256']
    assert sha(EVIDENCE/'lenient-labels.json')==plan['lenient_labels_sha256']
    material=[];diffs=[];anonymous=[];mapping={};md=['# Grok R1 匿名开发对照','',
        '仅隐藏臂标签，机器开发材料，不是独立人工盲审。超时和未执行项保留；不合并旧DeepSeek结果。','']
    items={i['key']:i for i in prepared['items']};calls={(r['item'],r['kind']):r for r in rows}
    for key,value in completed.items():
        item=items[key];case=cases[item['case']]
        m=next(m for m in value['manifests'] if m['task_type']=='dialogue')
        d=m['prompt_assembly']['material_selection'];pool=read(Path(item['candidate_file']))['payload']['roots']['example']
        row={'key':key,'case':item['case'],'role':item['role'],'arm':item['arm'],
            'returned':d['returned_example_ids'],'included':d['included_example_ids'],'lore':d['lore_ids'],
            'valid_selector':d['applied_prompt']==ARMS[item['arm']],'diagnostic':d,
            'final_input_hash':m['input_snapshot_hash'],'hash_matches':m['input_snapshot_hash']==calls[(key,'main')]['input_hash']}
        for name,allowed in [('strict',case['allowed_examples']),('lenient',labels[item['case']])]:
            row[name]={'available':sorted(set(pool)&set(allowed)),
                'selected':sorted(set(d['returned_example_ids'] or [])&set(allowed)),
                'included':sorted(set(d['included_example_ids'])&set(allowed)),
                'excluded':sorted(set(d['included_example_ids'])-set(allowed))}
        material.append(row)
    pairs=list(dict.fromkeys(i['pair'] for i in prepared['items']))
    for number,pair in enumerate(pairs,1):
        keys=[pair+'-'+a for a in ARMS];available=all(k in completed for k in keys)
        delta={'pair':pair,'complete':available}
        if available:
            manifests=[next(m for m in completed[k]['manifests'] if m['task_type']=='dialogue') for k in keys]
            mains=[calls[(k,'main')] for k in keys]
            a,b=[body_without_character(c['request']['messages'],m) for c,m in zip(mains,manifests)]
            delta.update(non_character_messages_equal=a[0]==b[0],facts_and_lore_equal=a[1]==b[1],examples_changed=a[2]!=b[2],
                main_request_equal=mains[0]['request']==mains[1]['request'],
                vendor_parameters_equal={k:v for k,v in mains[0]['request'].items() if k!='messages'}=={k:v for k,v in mains[1]['request'].items() if k!='messages'})
            delta['explained']=all(delta[k] for k in ('non_character_messages_equal','facts_and_lore_equal','vendor_parameters_equal'))
        else:delta.update(explained=None,reason='timeout_or_not_called')
        diffs.append(delta);order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        label='P'+str(number).zfill(2);mapping[label]=dict(zip('AB',order));case=cases[items[keys[0]]['case']]
        entry={'id':label,'members':case['members'],'reference':case['reference'],
            'role_reaction':'关注细节与条件' if items[keys[0]]['role']=='detail' else '坦率表达立场与取舍',
            **{side:completed[key]['turn']['reply'] if key in completed else None for side,key in zip('AB',order)}}
        anonymous.append(entry);md+=['## '+label,'',entry['role_reaction'],'']
        if case['reference']:md+=['引用：'+case['reference']['text'],'']
        md+=['输入：']+case['members']+['','A：',entry['A'] or '（未完成）','','B：',entry['B'] or '（未完成）','']
    matched={d['pair'] for d in diffs if d['complete']};stats={}
    for scope,subset in [('observed',material),('matched', [r for r in material if items[r['key']]['pair'] in matched])]:
        stats[scope]={}
        for scoring in ('strict','lenient'):
            stats[scope][scoring]={}
            for arm in ARMS:
                records=[r for r in subset if r['arm']==arm and r['valid_selector']];eligible=[r for r in records if r[scoring]['available']]
                stats[scope][scoring][arm]={'requests':len(records),'eligible':len(eligible),
                    'selected_requests':sum(bool(r[scoring]['selected']) for r in eligible),'included_requests':sum(bool(r[scoring]['included']) for r in eligible),
                    'candidate_items':sum(len(r[scoring]['available']) for r in records),'selected_items':sum(len(r[scoring]['selected']) for r in records),
                    'included_items':sum(len(r[scoring]['included']) for r in records),'excluded_requests':sum(bool(r[scoring]['excluded']) for r in records),
                    'unrelated_lore_requests':sum(bool(set(r['lore'])&set(cases[r['case']]['excluded_lore'])) for r in records)}
    for name,value in [('material-differences',material),('request-differences',diffs),('anonymous-comparison',anonymous),('anonymous-map',mapping),('selection-metrics',stats)]:
        atomic_json(root/(name+'.json'),value)
    (root/'anonymous-comparison.md').write_text('\n'.join(md),encoding='utf-8')


def report():
    root=EVIDENCE/'cohort';prepared=read(root/'prepared.json')
    rows=ReceiptLedger(root/'receipts',limit=64).rows()
    gate=ReceiptLedger(GATE/'receipts',limit=6).rows()
    completed={p.stem:read(p) for p in (root/'results').glob('*.json')}
    pending=[];accounting=[]
    for item in prepared['items']:
        key=item['key'];calls=[r for r in rows if r['item']==key]
        with Store(RuntimePaths(Path(item['runtime']))).connection() as db:
            for kind,table in (('selector','character_selection_attempts'),('main','attempts')):
                actual=db.execute('SELECT count(*) FROM '+table+' WHERE turn_id=?',(item['turn_id'],)).fetchone()[0]
                recorded=sum(c['kind']==kind for c in calls)
                accounting.append({'item':key,'kind':kind,'runtime_attempts':actual,'receipts':recorded,'match':actual==recorded})
        if key not in completed:
            pending.append({'item':key,'status':'failed' if any(c['status']=='failed' for c in calls) else 'incomplete' if calls else 'not_called',
                'calls':[c['ordinal'] for c in calls],'errors':[c.get('error') for c in calls if c['status']=='failed']})
    complete=len(completed)==32 and all(r['status']=='completed' for r in rows) and len(rows)==64
    full=None
    if complete:
        with (root/'report.log').open('w',encoding='utf-8') as stream:
            with redirect_stdout(stream):full=full_report(root)
        plan=read(EVIDENCE/'analysis-plan.json')
        if sha(EVIDENCE/'lenient-labels.json')!=plan['lenient_labels_sha256']:raise ValueError('analysis_labels_changed')
        if sha(root/'fixture.json')!=plan['strict_fixture_sha256']:raise ValueError('strict_labels_changed')
        labels=read(EVIDENCE/'lenient-labels.json')['allowed'];material=read(root/'material-differences.json')
        sensitivity={}
        for arm in ARMS:
            records=[]
            for row in material:
                if row['arm']!=arm or not row['valid_selector_sample']:continue
                allowed=set(labels[row['case']]);available=allowed&set(row['candidate_examples'])
                records.append({'key':row['key'],'available':sorted(available),'selected':sorted(allowed&set(row['returned'] or [])),
                    'included':sorted(allowed&set(row['included'])),'excluded':sorted(set(row['included'])-allowed)})
            eligible=[r for r in records if r['available']]
            sensitivity[arm]={'valid_requests':len(records),'eligible_requests':len(eligible),
                'selecting_applicable':sum(bool(r['selected']) for r in eligible),'including_applicable':sum(bool(r['included']) for r in eligible),
                'applicable_candidates':sum(len(r['available']) for r in records),'applicable_selected':sum(len(r['selected']) for r in records),
                'applicable_included':sum(len(r['included']) for r in records),'including_excluded':sum(bool(r['excluded']) for r in records),'records':records}
        atomic_json(root/'lenient-analysis.json',{'labels_changed':False,'independent_validation':False,'arms':sensitivity})
    else:partial_analysis(root,prepared,completed,rows)
    backend=[]
    for pair in dict.fromkeys(i['pair'] for i in prepared['items']):
        for kind in ('selector','main'):
            selected=[next((r for r in rows if r['item']==pair+'-'+arm and r['kind']==kind),None) for arm in ARMS]
            values=[r.get('diagnostics',{}).get('system_fingerprint') if r else None for r in selected]
            backend.append({'pair':pair,'kind':kind,'fingerprints':values,'match':bool(all(values) and values[0]==values[1])})
    output={'provider':'UniAPI gateway / grok-4.7; upstream identity not independently verified',
        'gate_passed':read(GATE/'content-review.json')['passed'] and read(GATE/'result.json')['protocol_passed'],
        'gate_calls':len(gate),'cohort_calls':len(rows),'total_calls':len(gate)+len(rows),'total_limit':70,
        'cohort_by_kind':dict(Counter(r['kind'] for r in rows)),'cohort_statuses':dict(Counter(r['status'] for r in rows)),
        'cohort_completed_requests':len(completed),'cohort_complete':complete,
        'completed_pairs':sum(all(pair+'-'+a in completed for a in ARMS) for pair in dict.fromkeys(i['pair'] for i in prepared['items'])),
        'runtime_receipts_match':all(r['match'] for r in accounting),'pending':pending,
        'blocked':read(EVIDENCE/'blocked.json') if (EVIDENCE/'blocked.json').exists() else None,
        'usage':{'gate':usage(gate),'cohort':usage(rows),'total':usage(gate+rows)},
        'unknown_usage_calls':sum(not r.get('usage') for r in gate+rows),
        'reasoning_tokens_are_subset_not_added_twice':True,'amount':'未知；按网关账单结算',
        'reported_response_models':sorted({r.get('diagnostics',{}).get('response_model','unknown') for r in rows}),
        'reported_system_fingerprints':sorted({r.get('diagnostics',{}).get('system_fingerprint','unknown') for r in rows}),
        'pair_backend_observations':backend,
        'retries':0,'paid_judges':0,'qq':0,'old_deepseek_results_merged':False}
    atomic_json(EVIDENCE/'accounting.json',accounting);atomic_json(EVIDENCE/'metrics.json',output)
    print(json.dumps(output,ensure_ascii=False,indent=2))
    return output


if __name__=='__main__':report()
