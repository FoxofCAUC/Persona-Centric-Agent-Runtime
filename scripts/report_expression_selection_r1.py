"""Inspect saved artificial evidence only; never generate or judge via a model."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from scripts.expression_selection_r1 import OUT, ARMS, read, verify, Ledger, reconcile, StopRun


def body_without_character(messages,manifest):
    result=[dict(m) for m in messages]
    block=next(b for b in manifest['prompt_assembly']['blocks'] if b['name']=='character')
    start=block['start_character'];end=start+block['characters']
    result[0]['content']=result[0]['content'][:start]+result[0]['content'][end:]
    text=messages[0]['content'][start:end]
    parts=[json.loads(line) for line in text.splitlines() if line.startswith('{')]
    return result,[p for p in parts if p['kind']!='example'],[p for p in parts if p['kind']=='example']


def report(root):
    frozen=read(root/'prepared.json');verify(root,frozen);ledger=Ledger(root/'ledger.json');accounting_error=None
    try:reconcile(root,frozen,ledger)
    except StopRun as exc:
        if not (root/'stop-reconciliation.json').exists():raise
        accounting_error=str(exc)
    fixture=read(root/'fixture.json');cases={c['id']:c for c in fixture['cases']}
    incomplete={r['item']:r for r in read(root/'local-incomplete-items.json')} if (root/'local-incomplete-items.json').exists() else {}
    calls={(c['item'],c['kind']):c for c in ledger.data['calls']};items={i['key']:i for i in frozen['items']}
    material=[];results={};paths=[]
    for key,item in items.items():
        path=root/'results'/(key+'.json')
        if not path.exists():continue
        value=read(path);results[key]=value
        manifest=next((m for m in value['manifests'] if m['task_type']=='dialogue' and 'prompt_assembly' in m),None)
        if manifest:
            observation=manifest['prompt_assembly']['material_selection'];case=cases[item['case']]
            returned=observation['returned_example_ids'];included=observation['included_example_ids'];available=item['candidate_available_examples']
            valid=observation['applied_prompt']==ARMS[item['arm']]
            material.append({'key':key,'case':item['case'],'role':item['role'],'arm':item['arm'],
                'candidate_examples':read(Path(item['candidate_file']))['payload']['roots']['example'],
                'applicable_candidates':available,'candidate_limitation':bool(case['allowed_examples'] and not available),
                'valid_selector_sample':valid,'returned':returned,'included':included,
                'applicable_selected':sorted(set(returned or ())&set(case['allowed_examples'])),
                'applicable_included':sorted(set(included)&set(case['allowed_examples'])),
                'inapplicable_selected':sorted(set(returned or ())&set(case['excluded_examples'])),
                'inapplicable_included':sorted(set(included)&set(case['excluded_examples'])),
                'unrelated_lore':sorted(set(observation['lore_ids'])&set(case['excluded_lore'])),
                'diagnostic':observation,'final_input_hash':manifest['input_snapshot_hash']})
            paths.append({'item':key,'selector':observation['requested_prompt'],'effective':observation['applied_prompt'],
                'mode':observation['effective_mode'],'fallback':observation['fallback_reason'],
                'voice':manifest['prompt_assembly']['effective']['expression'],
                'format':manifest['prompt_assembly']['effective']['format'],
                'capsule':manifest['prompt_assembly']['effective']['capsule'],
                'selector_call':observation['selection_attempt_id'],'main_hash':manifest['input_snapshot_hash']})
    differences=[];anonymous=[];mapping={};markdown=['# R1 匿名开发对照材料','',
        '仅隐去选择器臂标签，供机器开发分析或可选用户复核；不是独立人工盲审。保留完整输出，不按质量替换。','']
    for number,pair in enumerate(dict.fromkeys(i['pair'] for i in frozen['items']),1):
        keys=[pair+'-'+a for a in ARMS]
        pair_results={k:results.get(k,incomplete.get(k,{'turn':{'status':'not_called','reply':None},'manifests':[]})) for k in keys}
        a,b=[pair_results[k] for k in keys];ia,ib=[items[k] for k in keys]
        mains=[next((m for m in r['manifests'] if m['task_type']=='dialogue' and 'prompt_assembly' in m),None) for r in (a,b)]
        row={'pair':pair,'complete':all(r['turn']['status']=='completed' for r in (a,b))}
        if all(mains) and all((k,'main') in calls for k in keys):
            ca,cb=[calls[(k,'main')] for k in keys];ma,mb=mains
            na,fa,ea=body_without_character(ca['request']['messages'],ma);nb,fb,eb=body_without_character(cb['request']['messages'],mb)
            row.update(comparison_available=True,non_character_messages_equal=na==nb,facts_and_lore_equal=fa==fb,
                vendor_parameters_equal={k:v for k,v in ca['request'].items() if k!='messages'}=={k:v for k,v in cb['request'].items() if k!='messages'},
                examples_changed=ea!=eb,main_request_equal=ca['request']==cb['request'],
                control_hash=ca['input_hash'],experiment_hash=cb['input_hash'],
                change_source='expression_examples' if ea!=eb else 'none',
                candidate_hash_equal=ia['candidate_hash']==ib['candidate_hash'],
                selector_input_equal=ia['selector_messages'][1:]==ib['selector_messages'][1:])
            row['explained']=all(row[k] for k in ('non_character_messages_equal','facts_and_lore_equal','vendor_parameters_equal','candidate_hash_equal','selector_input_equal'))
        else:row.update(comparison_available=False,explained=None,reason='incomplete_main_evidence')
        differences.append(row)
        label='P'+str(number).zfill(2);order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        mapping[label]={'A':order[0],'B':order[1]}
        case=cases[ia['case']];entry={'id':label,'members':case['members'],'reference':case['reference'],
            'role_reaction':'关注具体细节与条件' if ia['role']=='detail' else '坦率表达立场与取舍',
            'A':pair_results[order[0]]['turn'].get('reply'),'B':pair_results[order[1]]['turn'].get('reply'),
            'statuses':{label:pair_results[key]['turn']['status'] for label,key in zip('AB',order)}}
        anonymous.append(entry);markdown+=['## '+label,'', '角色反应参考：'+entry['role_reaction'],'']
        if case['reference']:markdown+=['合法引用：'+case['reference']['text'],'']
        markdown+=['输入：']+['- '+m for m in case['members']]+['','A：',entry['A'] or '（无完整回复；'+entry['statuses']['A']+'）','','B：',entry['B'] or '（无完整回复；'+entry['statuses']['B']+'）','']
    stats={}
    for arm in ARMS:
        rows=[r for r in material if r['arm']==arm];valid=[r for r in rows if r['valid_selector_sample']];eligible=[r for r in valid if r['applicable_candidates']]
        denominator=sum(len(r['applicable_candidates']) for r in eligible)
        stats[arm]={'observed_main_requests':len(rows),'valid_selector_requests':len(valid),'fallback_requests':len(rows)-len(valid),
            'eligible_requests':len(eligible),'applicable_candidate_count':denominator,
            'requests_selecting_applicable':sum(bool(r['applicable_selected']) for r in eligible),
            'requests_including_applicable':sum(bool(r['applicable_included']) for r in eligible),
            'applicable_selected_count':sum(len(r['applicable_selected']) for r in eligible),
            'applicable_included_count':sum(len(r['applicable_included']) for r in eligible),
            'requests_including_inapplicable':sum(bool(r['inapplicable_included']) for r in valid),
            'requests_including_unrelated_lore':sum(bool(r['unrelated_lore']) for r in valid),
            'candidate_limitations':[r['key'] for r in rows if r['candidate_limitation']]}
    usage=Counter()
    for call in ledger.data['calls']:
        for k,v in (call.get('usage') or {}).items():
            if type(v) is int:usage[k]+=v
    report={'calls':len(ledger.data['calls']),'calls_by_kind':dict(Counter(c['kind'] for c in ledger.data['calls'])),
        'failed_calls':sum(c['status']=='failed' for c in ledger.data['calls']),'items':len(results),'complete':len(results)==32,
        'successful_replies':sum(r['turn']['status']=='completed' for r in results.values()),
        'accounting_error':accounting_error,'effect_verification_complete':len(results)==32 and all(r['turn']['status']=='completed' for r in results.values()) and not accounting_error,
        'comparison_pairs':len(differences),'compared_request_pairs':sum(r['comparison_available'] for r in differences),
        'complete_reply_pairs':sum(r['complete'] for r in differences),
        'all_available_differences_explained':all(r['explained'] for r in differences if r['comparison_available']),
        'changed_main_requests':sum(r.get('examples_changed',False) for r in differences), 'selection':stats,
        'usage':dict(usage),'amount':'未估价；以供应商账单为准','paid_judge_calls':0,'qq':0}
    for name,value in [('material-differences',material),('request-differences',differences),('effective-paths',paths),
                       ('anonymous-comparison',anonymous),('anonymous-map',mapping),('metrics',report)]:atomic_json(root/(name+'.json'),value)
    (root/'anonymous-comparison.md').write_text('\n'.join(markdown),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--simulation',action='store_true');a=p.parse_args()
    report(OUT/('simulation' if a.simulation else 'experiment'))
