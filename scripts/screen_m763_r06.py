"""Read-only mechanical screening; never fills human judgements or calls models.

Substring flags require semantic review. Neither passed substrings nor a chosen
decision proves task completion, action realization, or subjective naturalness.
"""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.config import atomic_json
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.turn_input import load_turn_input
from persona_runtime.disposition_core.engine import load_policy
from experiments.persona_evaluation.contracts import ranked_situations
from scripts.check_m763_r06 import readonly
from scripts.m763_r06 import PACKAGE
from scripts.m763_retries import effective_case


def output_flags(case,output):
    if not isinstance(output,str) or not output.strip():return ['missing_output']
    flags=['required_literal_missing:'+str(i) for i,text in enumerate(case.get('required_substrings',[])) if text not in output]
    if any('"'+key+'"' in output for key in ('behavior_intent','cancel_if_new_input','chosen_action','capsule_hash')):
        flags.append('possible_internal_structure_leak')
    if output.count('```')%2:flags.append('unbalanced_code_fence')
    return flags


def screen(out):
    fixture=json.loads((out/'independent-v1.json').read_text(encoding='utf-8'))
    prepared=json.loads((out/'prepared.json').read_text(encoding='utf-8'))
    actual_hash=sha256((out/'independent-v1.json').read_bytes()).hexdigest()
    if actual_hash!=prepared['fixture_sha256']:raise ValueError('screen_fixture_changed')
    expected={c['id']:c for c in fixture['cases']}
    if Counter(c['group'] for c in expected.values())!={'persona':40,'relationship':20,'information':20,'control':20}:
        raise ValueError('screen_fixed_coverage')
    policy_path=Path(__file__).resolve().parents[1]/'persona_runtime/disposition_core/rules.json'
    freeze=json.loads((out/'execution-freeze.json').read_text(encoding='utf-8'))
    if freeze['source_hashes']['persona_runtime/disposition_core/rules.json']!=sha256(policy_path.read_bytes()).hexdigest():
        raise ValueError('screen_rule_version_changed')
    policy=load_policy(policy_path);ledger=EvaluationStore(RuntimePaths(out))
    with ledger.connect(PACKAGE) as db:slots={(r['case_id'],r['arm']):dict(r) for r in db.execute('SELECT * FROM slots')}
    rows=[];top1=0;top2=0;ranked_count=0;observed=0
    for canonical in prepared['cases']:
        case=expected[canonical['case_id']];current=effective_case(out,canonical)
        result={'case_id':case['id'],'group':case['group'],'arms':{},'decision':None}
        for arm in 'ABC':
            slot=slots[(case['id'],arm)]
            result['arms'][arm]={'state':slot['state'],'flags':output_flags(case,slot['output']) if slot['state']=='success' else ['output_not_completed']}
        if slots[(case['id'],'C')]['state']=='success':
            with readonly(Path(canonical['runtime_roots']['C'])/'data/database/runtime.sqlite3') as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(current['turns']['C'],)).fetchone())
                row=db.execute('SELECT * FROM disposition_decisions WHERE turn_id=? AND execution_revision=?',(turn['id'],turn['execution_revision'])).fetchone()
                if row:
                    if row['persona_id']!=canonical['persona_id'] or row['attempt_id']!=slots[(case['id'],'C')]['attempt_id']:
                        raise ValueError('screen_decision_scope')
                    saved=json.loads(row['record_json']);record=saved.get('record')
                    result['decision']={'status':saved['status'],'applied_to_prompt':saved.get('applied_to_prompt',False),
                        'chosen_action':(record or {}).get('chosen_action'),'observation_status':saved.get('observation',{}).get('status'),
                        'relationship_status':saved.get('relationship_observation',{}).get('status')}
                    if record:
                        observed+=1
                        frame=SimpleNamespace(categories=record['categories'],evidence=[SimpleNamespace(**e) for e in record['evidence']],frame_origin=record['frame_origin'])
                        members=[SimpleNamespace(id=m['id']) for m in load_turn_input(db,turn)]
                        ranking=ranked_situations(frame,members,policy)
                        result['decision']['ranking']=ranking
                        if case['group'] in ('relationship','information'):
                            ranked_count+=1;labels=[x['category'] for x in ranking['candidates']]
                            top1+=bool(labels and labels[0] in case['expected_categories'])
                            top2+=bool(set(labels)&set(case['expected_categories']))
        rows.append(result)
    status=ledger.status(PACKAGE)
    return {'tool_version':'r06-mechanical-screen-v1','tool_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'fixture_sha256':actual_hash,'read_only':True,'model_calls':0,'ledger':status,'rows':rows,
        'situation':{'denominator':40,'observed_rankings':ranked_count,'top1_count':top1,'top2_count':top2},
        'observed_c_decisions':observed,'flagged_outputs':sum(bool(a['flags']) for r in rows for a in r['arms'].values()),
        'semantic_review_required':True,'human_review_answers_written':0,'quality_gate_passed':False,
        'notes':['Missing/failed/degraded cases stay in fixed denominators.','Output flags are review hints, not semantic verdicts.','No action realization or naturalness is inferred from JSON metadata.']}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();dest=a.report.resolve()
    if dest.exists() or not dest.is_relative_to(out):raise ValueError('new_controlled_report_required')
    result=screen(out);atomic_json(dest,result)
    print(json.dumps({k:result[k] for k in ('ledger','situation','observed_c_decisions','flagged_outputs','quality_gate_passed')}))
