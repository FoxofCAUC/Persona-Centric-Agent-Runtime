"""v2 evaluation lifecycle: explicit new experiment, original run preserved."""
import argparse
import asyncio
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.instance import InstanceLock
from scripts.m763_provider_gate import DirectoryGate
from scripts.m763_r06 import run,PACKAGE
from scripts.m763_cohorts import save_once

REVIEW='m763-r06-natural-v2-review'


async def generate(home,out,repair_tests=None,repair_reason=None):
    prepared=json.loads((out/'prepared.json').read_text(encoding='utf-8'))
    if not prepared.get('prompt_comparison'):raise ValueError('v2_preparation_required')
    policy={'version':'failed-slot-retry-v1','package_id':PACKAGE,'immediate_retry_opportunities':3,
            'exhausted':'defer_until_other_slots_terminal_then_retry',
            'preserve_failed_attempts':True,'successful_slots_must_not_replay':True,
            'quality_poor_completed_outputs_not_retryable':True,
            'scope':'explicit user-authorized new v2 evaluation'}
    save_once(out/'retry-policy.json',policy)
    cfg=ConfigStore(RuntimePaths(home)).load();gate=DirectoryGate(out/'provider-check.json',cfg,out)
    if not gate.path.exists():
        checks=await gate.check_all()
        atomic_json(gate.path,{'normal':all(row['normal'] for row in checks),'model_calls':0,'checks':checks})
    await gate.ensure()
    await run(home,out,gate.path,repair_reason=repair_reason,repair_tests=repair_tests)


def stylistic_report(store):
    by_arm={'A':[],'B':[],'C':[]}
    with store.connect(PACKAGE) as db:
        for row in db.execute("SELECT arm,output FROM slots WHERE state='success'"):by_arm[row['arm']].append(row['output'])
    result={}
    for arm,texts in by_arm.items():
        prefixes=Counter(re.sub(r'\s+','',text)[:8] for text in texts)
        result[arm]={'outputs':len(texts),'service_offer':sum(bool(re.search(r'需要我.{0,50}[吗么]|如果你愿意|我可以帮你|要不要我',text)) for text in texts),
                     'generic_empathy_opening':sum(bool(re.match(r'听起来|能理解你的|我理解你的|这一定让你',text)) for text in texts),
                     'question_ending':sum(bool(re.search(r'[？?][”\"\s]*$',text)) for text in texts),
                     'unique_eight_character_openings':len(prefixes),'most_repeated_opening_count':max(prefixes.values(),default=0)}
    return {'version':'voice-v2-style-observation','counts':result,
            'note':'Substring counts are diagnostic hints, not naturalness or task verdicts. Question marks may be appropriate.','model_calls':0}


async def publish(home,out):
    from scripts.check_m763_r06 import inspect
    from scripts.screen_m763_r06 import screen
    from scripts.audit_m763_context_replay import audit
    from scripts.prepare_m763_review import copy_for_review,validate_context_replay
    inventory=inspect(out)
    if not inventory['machine_inventory_complete']:raise ValueError('v2_generation_incomplete')
    mechanical=screen(out);source=EvaluationStore(RuntimePaths(out))
    replay=await audit(home,out);validate_context_replay(source,replay)
    style=stylistic_report(source)
    for name,report in [('inventory-final.json',inventory),('mechanical-screen-final.json',mechanical),
                        ('context-replay-final.json',replay),('style-observations.json',style)]:
        save_once(out/name,report)
    binding={'version':'v2-prompt-comparison-publication','fixture_sha256':inventory['frozen_fixture_sha256'],
             'source_ledger_sha256':sha256(source.path(PACKAGE).read_bytes()).hexdigest(),
             'inventory':inventory,'context_replay_complete':True,'style_observations':style,
             'quality_gate_passed':False,'live_authorized':False,
             'comparison':'B=original M7 prompt; C=new voice prompt, sampling and Disposition. Shared author sources and selected material.'}
    target=EvaluationStore(RuntimePaths(home))
    if target.path(REVIEW).exists():
        with target.connect(REVIEW) as db:
            original=json.loads(db.execute("SELECT value FROM metadata WHERE key='review_publication'").fetchone()[0])
        if original!=binding:raise ValueError('v2_existing_publication_changed')
        return target.status(REVIEW)
    result=copy_for_review(source.path(PACKAGE),target,REVIEW,binding)
    save_once(out/'review-publication.json',result)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('generate','publish','status'))
    p.add_argument('--home',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--repair-tests',type=Path);p.add_argument('--repair-reason');a=p.parse_args()
    home=a.home.resolve();out=a.output.resolve()
    if not out.is_relative_to(home/'exports/acceptance'):raise ValueError('controlled_evidence_required')
    lock=InstanceLock(out/'execution.lock')
    try:
        if a.action=='generate':result=asyncio.run(generate(home,out,a.repair_tests,a.repair_reason))
        elif a.action=='publish':result=asyncio.run(publish(home,out))
        else:result=EvaluationStore(RuntimePaths(out)).status(PACKAGE)
        print(json.dumps(result,ensure_ascii=False))
    finally:lock.close()


if __name__=='__main__':main()
