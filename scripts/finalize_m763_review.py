"""Compute frozen M7.6.3 quality counts after blind review; no model calls."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.evaluation_metrics import quality_gates, wilson
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe


def control_invariant_result(case, b_task, c_task, b_action, c_action, c_output):
    """Judge the frozen answer/task contract, not equality of style labels.

    Task ratings remain the user's frozen judgments. Exact required strings are
    only an additional machine check, never a replacement for semantic review.
    """
    required=case.get('required_substrings',[])
    if (not isinstance(required,list) or any(not isinstance(v,str) or not v for v in required)
            or not isinstance(c_output,str) or not c_output.strip()):
        raise ValueError('quality_control_evidence_missing')
    missing=sum(value not in c_output for value in required)
    return {'case_id':case['id'],'b_task':b_task,'c_task':c_task,
            'action_label_difference':b_action!=c_action,
            'required_string_checks':len(required),'missing_required_strings':missing,
            'invariant_violation':c_task!='pass' or missing>0}


def summarize(home, evidence, package_id):
    home=safe(home);evidence=safe(evidence,home/'exports/acceptance')
    fixture_path=safe(evidence/'independent-v1.json',evidence)
    mechanical_path=safe(evidence/'mechanical-screen-final.json',evidence)
    fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
    mechanical=json.loads(mechanical_path.read_text(encoding='utf-8'))
    fixture_hash=sha256(fixture_path.read_bytes()).hexdigest()
    if mechanical.get('fixture_sha256')!=fixture_hash or mechanical.get('model_calls')!=0:
        raise ValueError('quality_mechanical_binding')
    cases={case['id']:case for case in fixture['cases']}
    decisions={row['case_id']:(row.get('decision') or {}).get('chosen_action') for row in mechanical['rows']}
    if len(cases)!=100 or set(decisions)!=set(cases):raise ValueError('quality_case_binding')

    store=EvaluationStore(RuntimePaths(home))
    with store.connect(package_id) as db:
        meta={key:json.loads(value) for key,value in db.execute('SELECT key,value FROM metadata')}
        rows=[dict(row) for row in db.execute(
            'SELECT c.id,c.ordinal,c.left_arm,r.revision,r.body FROM cases c JOIN reviews r ON r.case_id=c.id ORDER BY c.ordinal')]
        raw_review_rows=[dict(row) for row in db.execute('SELECT * FROM reviews ORDER BY case_id')]
        provenance=dict(db.execute("SELECT kind,count(*) FROM audit WHERE kind IN ('review','review_ai_approved','review_ai_modified') GROUP BY kind"))
        outputs={(row['case_id'],row['arm']):row['output'] for row in db.execute(
            "SELECT case_id,arm,output FROM slots WHERE arm='C' AND state='success'")}
    if meta.get('frozen') is not True or len(rows)!=100 or set(row['id'] for row in rows)!=set(cases):
        raise ValueError('quality_review_incomplete')

    judged=[]
    for row in rows:
        review=json.loads(row['body']);cside='left' if row['left_arm']=='C' else 'right';bside='right' if cside=='left' else 'left'
        judged.append({'case_id':row['id'],'case':cases[row['id']],
                       'b_task':review[bside+'_task'],'c_task':review[cside+'_task'],
                       'b_action':review[bside+'_action'],'c_action':review[cside+'_action'],
                       'c_natural':review['natural']==cside,'chosen_action':decisions[row['id']]})

    persona=[row for row in judged if row['case']['group']=='persona']
    controls=[row for row in judged if row['case']['group']=='control']
    if len(persona)!=40 or len(controls)!=20:raise ValueError('quality_fixed_denominators')
    b_correct=sum(row['b_action'] in row['case']['expected_actions'] for row in persona)
    c_correct=sum(row['c_action'] in row['case']['expected_actions'] for row in persona)
    pairs=defaultdict(list)
    for row in persona:pairs[row['case']['pair_id']].append(row)
    if len(pairs)!=20 or any(len(value)!=2 for value in pairs.values()):raise ValueError('quality_pair_contract')
    reasonable=sum(all(item['c_action'] in item['case']['expected_actions'] for item in pair)
                   and len({item['c_action'] for item in pair})>1 for pair in pairs.values())
    control_results=[control_invariant_result(row['case'],row['b_task'],row['c_task'],
        row['b_action'],row['c_action'],outputs.get((row['case_id'],'C'))) for row in controls]
    unnecessary=sum(row['invariant_violation'] for row in control_results)
    counts={
        'situation_top1':mechanical['situation']['top1_count'],
        'situation_top2':mechanical['situation']['top2_count'],
        'implicit_gain':c_correct-b_correct,
        'reasonable_pairs':reasonable,
        'unnecessary_cases':unnecessary,
        'action_implementation':sum(bool(row['chosen_action']) and row['c_action']==row['chosen_action'] for row in judged),
        'natural_wins':sum(row['c_natural'] for row in judged),
        'b_task_success':sum(row['b_task']=='pass' for row in judged),
        'c_task_success':sum(row['c_task']=='pass' for row in judged),
        'critical_errors':sum(row['c_task']=='critical_error' for row in judged),
    }
    gates=quality_gates(counts)
    return {'version':'m763-human-quality-v2','package_id':package_id,'fixture_sha256':fixture_hash,
            'review_hash':fingerprint(raw_review_rows),'review_provenance':provenance,
            'counts':counts,'gates':gates,'quality_passed':all(gates.values()),
            'control_metric_policy':'frozen-task-and-answer-invariants-v1',
            'control_action_label_differences':sum(row['action_label_difference'] for row in control_results),
            'control_case_results':control_results,
            'naturalness_wilson_lower':wilson(counts['natural_wins']),
            'persona_action_correct':{'b':b_correct,'c':c_correct,'denominator':40},
            'model_calls':0,'review_frozen':True,
            'scope_note':'AI-assisted suggestions were explicitly accepted or modified by the user; this is not an unaided human-origin review.',
            'live_authorized':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--package-id',required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();home=args.home.resolve();evidence=args.evidence.resolve();report=args.report.resolve()
    safe(report,evidence)
    if report.exists():raise FileExistsError('quality_report_exists')
    result=summarize(home,evidence,args.package_id);atomic_json(report,result)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
