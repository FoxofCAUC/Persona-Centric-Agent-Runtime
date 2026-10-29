"""Offline M7.4 fixed-sample checks and descriptive metrics; no scoring/model calls."""
import argparse
import ast
import json
import re
import statistics
from pathlib import Path

from persona_runtime.config import atomic_json
from persona_runtime.reply_transport import render_parts


def code_check(text):
    blocks=re.findall(r'```(?:python|py)?\s*\n(.*?)```',text,re.S)
    tree=ast.parse(blocks[0] if blocks else text)
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='safe_divide']
    return bool(functions) and sum(isinstance(n,ast.Assert) for n in tree.body)==2


def automatic(case, turn):
    text=turn['reply'];batch=turn['reply_batch'];checks={'completed':turn['status']=='completed','one_attempt':turn['attempt_count']==1,
        'no_structure_leak':not any(key in text for key in ('"schema_version"','"cancel_if_new_input"','"behavior_intent"'))}
    if case['mode']=='natural_structured':
        checks['v2_valid']=batch['schema_version']==2 and 1<=len(batch['segments'])<=3
        checks['protected_required']=all(not s['cancel_if_new_input'] for s in batch['segments'] if s['segment_index']==1 or s['content_kind']!='chat')
        checks['transport_rebuild']=all(''.join(render_parts(s['text'],s['content_kind'])[1])==render_parts(s['text'],s['content_kind'])[0] for s in batch['segments'])
    if case['case']=='code':checks['complete_python_and_two_asserts']=code_check(text)
    elif case['case']=='exact':checks['exact_literals']=all(s in text for s in ('v0.7.4','123.45','https://example.org/a_b?x=12.345&v=0.7.4'))
    elif case['case']=='quote':checks['quote_preserved']='窗边两把蓝椅，预算300元。' in text
    elif case['case']=='character':checks['known_values']=('172' in text and '书店' in text);checks['unknown_not_assigned']=not re.search(r'(?:[ABO]|AB)\s*型',text)
    elif case['case']=='optional':checks['required_answer_not_optional']=any('5' in s['text'] and not s['cancel_if_new_input'] for s in batch['segments'])
    elif case['case']=='technical':checks['length_limit']=len(text)<=150
    return checks


def report(folder):
    state=json.loads((folder/'real-checkpoint.json').read_text(encoding='utf-8'));rows=[];versions=[];sessions=[]
    for case in state['schedule']:
        record=json.loads((folder/(case['slot']+'.json')).read_text(encoding='utf-8'));turn=record['turn']
        checks=automatic(case,turn);checks['one_manifest']=len(record['manifests'])==1
        versions.append({r['kind']:r['id'] for r in record['manifests'][0]['included_state_refs'] if r['kind'] in ('core','expression','relationship','character')})
        sessions.append(turn['session_id'])
        rows.append({'slot':case['slot'],'case':case['case'],'mode':case['mode'],'anonymous':case['anonymous'],'checks':checks,
                     'tokens':turn['usage'],'timings':turn['timings'],'fallback':turn['reply_batch']['fallback_reason'],
                     'planned_wait_ms':sum(s['wait_ms'] or 0 for s in turn['reply_batch']['segments'])})
    metrics={}
    for mode in ('legacy_full','natural_structured'):
        group=[r for r in rows if r['mode']==mode]
        metrics[mode]={'calls':len(group),'tokens_total':sum(r['tokens']['total_tokens'] for r in group),'amount':'unknown',
                       'planned_wait_ms_total':sum(r['planned_wait_ms'] for r in group),'desktop_actual_artificial_wait_ms':0}
        for key in ('queue_seconds','provider_seconds','persist_seconds','total_seconds'):
            values=[r['timings'][key] for r in group if r['timings'].get(key) is not None]
            metrics[mode][key]={'median':statistics.median(values),'mean':statistics.mean(values),'max':max(values)} if values else None
    result={'samples':rows,'metrics':metrics,'same_persona_versions':all(v==versions[0] for v in versions),
            'isolated_sessions':len(set(sessions))==24,'automatic_pass':all(all(r['checks'].values()) for r in rows),
            'machine_ratings':'pending','scope':'Small machine-reviewed sample, no subjective or cross-model claim'}
    rating_path=folder/'blind-ratings.json'
    if rating_path.exists():
        ratings=json.loads(rating_path.read_text(encoding='utf-8'));comparisons=[]
        for name in sorted({r['case'] for r in rows}):
            pair={r['mode']:ratings[r['anonymous']] for r in rows if r['case']==name}
            a=pair['natural_structured'];b=pair['legacy_full']
            comparisons.append({'case':name,'natural_not_worse':sum(a['scores'].values())>=sum(b['scores'].values()),'natural':a,'legacy':b})
        result['machine_ratings']={'comparisons':comparisons,'not_worse':sum(c['natural_not_worse'] for c in comparisons),
                                   'critical_errors':[k for k,v in ratings.items() if v.get('critical')]}
    atomic_json(folder/'quality-report.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('samples','machine_ratings')},ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();report(args.output)
