"""Combine unchanged originals with the first valid supplemental result; no calls."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.retry_expression_selection_r1 import ORIGINAL,SUPPLEMENT,ReceiptLedger,check_original,read,sha
from scripts.expression_selection_r1 import ARMS,fingerprint
from scripts.report_expression_selection_r1 import body_without_character
from persona_runtime.config import atomic_json


def report():
    root=SUPPLEMENT/'real';out=SUPPLEMENT/'analysis';out.mkdir(exist_ok=True)
    check_original(read(root/'original-seal.json'))
    frozen=read(ORIGINAL/'prepared.json');fixture=read(ORIGINAL/'fixture.json')
    cases={c['id']:c for c in fixture['cases']};labels=read(root/'lenient-labels.json')
    old_calls=read(ORIGINAL/'ledger.json')['calls'];new_calls=ReceiptLedger(root/'receipts').rows()
    calls={(r['item'],r['kind']):r for r in old_calls}
    for row in new_calls:
        key=(row['item'],row['kind'])
        if key not in calls or calls[key]['status']!='completed':calls[key]=row
    values={};material=[];reuse=[]
    for item in frozen['items']:
        key=item['key'];prior=ORIGINAL/'results'/(key+'.json');extra=root/'results'/(key+'.json')
        if prior.exists() and read(prior)['turn']['status']=='completed':
            if extra.exists():raise ValueError('successful_original_replaced')
            result=read(prior);source='original_success';reuse.append({'item':key,'sha256':sha(prior)})
        elif extra.exists():result=read(extra);source='supplement'
        elif prior.exists():result=read(prior);source='original_failed'
        else:result={'turn':{'status':'not_completed','reply':None},'manifests':[]};source='unresolved'
        result=deepcopy(result);result['analysis_source']=source;values[key]=result
        manifest=next((m for m in result['manifests'] if m['task_type']=='dialogue' and m.get('prompt_assembly')),None)
        if not manifest:continue
        diag=manifest['prompt_assembly']['material_selection'];case=cases[item['case']]
        pool=read(Path(item['candidate_file']))['payload']['roots']['example'];included=diag['included_example_ids'];returned=diag['returned_example_ids']
        row={'key':key,'case':item['case'],'role':item['role'],'arm':item['arm'],'source':source,
             'valid_selector':diag['applied_prompt']==ARMS[item['arm']], 'returned':returned,'included':included,
             'lore':diag['lore_ids'],'unrelated_lore':sorted(set(diag['lore_ids'])&set(case['excluded_lore'])),
             'diagnostic':diag,'main_input_hash':manifest['input_snapshot_hash']}
        main=calls.get((key,'main'))
        row['final_hash_matches']=bool(main and main['input_hash']==manifest['input_snapshot_hash']==manifest['prompt_assembly']['input_snapshot_hash'])
        for name,allowed in [('frozen',case['allowed_examples']),('lenient',labels['allowed'][item['case']])]:
            row[name]={'available':sorted(set(pool)&set(allowed)),'selected':sorted(set(returned or ())&set(allowed)),
                       'included':sorted(set(included)&set(allowed)), 'excluded_included':sorted(set(included)-set(allowed))}
        material.append(row)
    differences=[];anonymous=[];mapping={};md=['# R1 续验匿名对照','',
        '机器开发分析材料，不是独立人工盲审。19条原完整回复保持，失败项只采用首次合同有效的续验回复；低质量完整输出不替换。','']
    for number,pair in enumerate(dict.fromkeys(i['pair'] for i in frozen['items']),1):
        items=[i for i in frozen['items'] if i['pair']==pair];keys=[i['key'] for i in items]
        pair_values=[values[k] for k in keys]
        manifests=[next((m for m in v['manifests'] if m['task_type']=='dialogue' and m.get('prompt_assembly')),None) for v in pair_values]
        mains=[calls.get((key,'main')) for key in keys]
        delta={'pair':pair,'complete':all(v['turn']['status']=='completed' for v in pair_values)}
        if all(manifests) and all(mains):
            a,b=[body_without_character(c['request']['messages'],m) for c,m in zip(mains,manifests)]
            delta.update(comparison_available=True,non_character_messages_equal=a[0]==b[0],facts_and_lore_equal=a[1]==b[1],examples_changed=a[2]!=b[2],
                main_request_equal=mains[0]['request']==mains[1]['request'],
                vendor_parameters_equal={k:v for k,v in mains[0]['request'].items() if k!='messages'}=={k:v for k,v in mains[1]['request'].items() if k!='messages'},
                candidate_hash_equal=items[0]['candidate_hash']==items[1]['candidate_hash'],selector_input_equal=items[0]['selector_messages'][1:]==items[1]['selector_messages'][1:])
            delta['explained']=all(delta[k] for k in ('non_character_messages_equal','facts_and_lore_equal','vendor_parameters_equal','candidate_hash_equal','selector_input_equal'))
        else:delta.update(comparison_available=False,explained=None)
        differences.append(delta)
        order=list(keys)
        if int(fingerprint(pair)[:2],16)%2:order.reverse()
        label='P'+str(number).zfill(2);mapping[label]={'A':order[0],'B':order[1]};case=cases[items[0]['case']]
        entry={'id':label,'members':case['members'],'reference':case['reference'],
            'role_reaction':'关注具体细节与条件' if items[0]['role']=='detail' else '坦率表达立场与取舍',
            **{side:values[key]['turn'].get('reply') for side,key in zip('AB',order)},
            'statuses':{side:values[key]['turn']['status'] for side,key in zip('AB',order)},
            'sources':{side:values[key]['analysis_source'] for side,key in zip('AB',order)}}
        anonymous.append(entry);md+=['## '+label,'',entry['role_reaction'],'']
        if case['reference']:md+=['合法引用：'+case['reference']['text'],'']
        md+=['输入：']+['- '+v for v in case['members']]+['','A：',entry['A'] or '（未完成）','','B：',entry['B'] or '（未完成）','']
    stats={}
    for scoring in ('frozen','lenient'):
        stats[scoring]={}
        for arm in ARMS:
            rows=[r for r in material if r['arm']==arm and r['valid_selector']]
            eligible=[r for r in rows if r[scoring]['available']]
            stats[scoring][arm]={'valid_requests':len(rows),'eligible_requests':len(eligible),
                'selecting_applicable':sum(bool(r[scoring]['selected']) for r in eligible),
                'including_applicable':sum(bool(r[scoring]['included']) for r in eligible),
                'applicable_candidates':sum(len(r[scoring]['available']) for r in rows),
                'applicable_selected':sum(len(r[scoring]['selected']) for r in rows),
                'applicable_included':sum(len(r[scoring]['included']) for r in rows),
                'including_excluded':sum(bool(r[scoring]['excluded_included']) for r in rows),
                'including_unrelated_lore':sum(bool(r['unrelated_lore']) for r in rows)}
    usage={}
    for name,rows in [('original',old_calls),('supplement',new_calls),('combined',old_calls+new_calls)]:
        total=Counter()
        for row in rows:
            for k,v in (row.get('usage') or {}).items():
                if type(v) is int:total[k]+=v
        usage[name]=dict(total)
    replay_checks=[]
    for item in read(root/'prepared.json')['items']:
        if item['operation']!='replay_main':continue
        old=next(c for c in old_calls if c['ordinal']==item['original_call_ordinal'])
        rows=[c for c in new_calls if c['item']==item['key']]
        replay_checks.append({'item':item['key'],'calls':len(rows),'attempted':bool(rows),'all_requests_equal':all(c['request']==old['request'] for c in rows),
                             'all_safe_models_equal':all(c['model']==old['model'] for c in rows)})
    result={'original_recorded_calls':len(old_calls),'original_unknown_dispatches':2,
        'supplement_calls':len(new_calls),'supplement_by_kind':dict(Counter(r['kind'] for r in new_calls)),
        'supplement_failures':sum(r['status']=='failed' for r in new_calls),
        'recorded_calls_combined':len(old_calls)+len(new_calls),'conservative_slots':58+len(new_calls),
        'supplement_limit':34,'cumulative_conservative_limit':92,
        'successful_originals_reused':len(reuse),'successful_replies':sum(v['turn']['status']=='completed' for v in values.values()),
        'complete_pairs':sum(d['complete'] for d in differences),
        'compared_request_pairs':sum(d['comparison_available'] for d in differences),
        'all_available_differences_explained':all(d['explained'] for d in differences if d['comparison_available']),
        'all_planned_requests_compared':all(d['comparison_available'] for d in differences),
        'changed_request_pairs':sum(d.get('examples_changed',False) for d in differences),
        'all_manifest_hashes_match':all(r['final_hash_matches'] for r in material),
        'original_failed_requests_replayed_identically':all(r['all_requests_equal'] and r['all_safe_models_equal'] for r in replay_checks),
        'selection':stats,'usage':usage,'paid_judge_calls':0,'qq':0,
        'historical_accounting_gap_retained':True,'lenient_analysis_is_sensitivity_not_independent_validation':True}
    for name,value in [('combined-results',values),('material-differences',material),('request-differences',differences),
                       ('anonymous-comparison',anonymous),('anonymous-map',mapping),('original-success-reuse',reuse),
                       ('replay-equivalence',replay_checks),('metrics',result)]:atomic_json(out/(name+'.json'),value)
    (out/'anonymous-comparison.md').write_text('\n'.join(md),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':report()
