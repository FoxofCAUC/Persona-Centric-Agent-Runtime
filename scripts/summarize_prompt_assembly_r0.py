"""Build the body-free effective-path matrix from the artificial baseline cases."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['TEMP']=os.environ['TMP']='D:/Persona-Centric Agent Runtime/tmp'
from persona_runtime.context_m4 import build_context
from tests.prompt_baseline_fixture import cases,capture
from persona_runtime.archive import fingerprint

OUT=Path('D:/Persona-Centric Agent Runtime/exports/acceptance/M7.6.3-R0-prompt-baseline')


def main():
    expected=json.loads((ROOT/'tests/fixtures/prompt-baseline-r0.json').read_text(encoding='utf-8'))
    rows=[]
    for case in cases():
        observed={}
        def record(*args,**kwargs):
            result=build_context(*args,**kwargs)
            observed.update(result[1].get('prompt_assembly',{}))
            return result
        result=capture(case,builder=record)
        assert json.dumps(result,ensure_ascii=False)==json.dumps(expected['scenarios'][case['name']],ensure_ascii=False)
        cap=next((b for b in observed.get('blocks',[]) if b['name']=='capsule'),{})
        budget=result.get('details',{}).get('capsule_budget',{})
        rows.append({'scenario':case['name'],'outcome':result.get('error_code','ok'),
                     'requested_voice':'natural-dialogue-v2' if case.get('voice') else 'invalid' if case.get('invalid_voice') else None,
                     'expression_renderer':observed.get('expression_renderer'),
                     'format_renderer':observed.get('format_renderer'),
                     'provider_contract':result.get('provider_contract'),
                     'requested_capsule_mode':case.get('decision_mode','baseline'),
                     'capsule_present':bool(cap.get('present')),'capsule_budget_reason':budget.get('reason'),
                     'removed':result.get('details',{}).get('removed',[]),
                     'character_removed':(result.get('details',{}).get('character') or {}).get('removed',[]),
                     'input_characters':observed.get('input_characters'),
                     'input_snapshot_hash':result.get('input_snapshot_hash'),
                     'observation_hash_matches':result.get('input_snapshot_hash')==observed.get('input_snapshot_hash') if 'messages' in result else None})
    (OUT/'effective-paths.json').write_text(json.dumps({'artificial_only':True,'scenarios':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'matrix_scenarios':len(rows),'baseline_unchanged':True,'observations_match':all(r['observation_hash_matches'] is not False for r in rows)}))


if __name__=='__main__':main()
