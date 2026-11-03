"""Post-run CR2 evidence inspection. Never collects material or dispatches generation."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys
import types
import zipfile
import argparse
import hashlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.dialogue_context import require
from persona_runtime.config import atomic_json
from persona_runtime.provider_compat import build_request
from scripts.dialogue_context import OUT,read,sha,verify_freeze

VERSION='cr2-postrun-audit-v1'


def legacy_replay(root,item,saved):
    """Run the sealed pre-change renderer against the same artificial authority, no dispatch/index."""
    from persona_runtime.storage import Store
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.config import ConfigStore
    from persona_runtime.chat import ChatEngine
    from persona_runtime.archive import read_exact,turn_scope
    from persona_runtime.context_builders import pool
    from persona_runtime.archive_query import Budget
    from persona_runtime.character_selector_protocol import apply_selection
    from persona_runtime.turn_input import load_turn_input
    from scripts.dialogue_refactor import close,NoDispatch
    with zipfile.ZipFile(OUT.parent/'CR1-oracle-diagnosis/source-CR1-final.zip') as archive:
        text=archive.read('persona_runtime/context_m4.py')
    module=types.ModuleType('persona_runtime._cr2_prechange_renderer');module.__package__='persona_runtime'
    exec(compile(text,'<sealed CR1 context_m4>','exec'),module.__dict__)
    store=Store(RuntimePaths(root/'runtime'/(item['key']+'-legacy')))
    engine=ChatEngine(store,ConfigStore(store.paths),NoDispatch())
    try:
        with store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['attachment']['turn_id'],)).fetchone())
            _,corpus=pool(db,turn,Budget())
            chosen=[]
            for ref in saved['manifest']['evidence_refs']:
                if ref['kind'] not in ('recent','retrieval'):continue
                chosen.append({'group':read_exact(db,turn_scope(db,turn),{'kind':'turn','id':ref['turn_id']}),
                               'kind':ref['kind'],'rank':len(chosen)})
            evidence={'items':chosen,'pool':corpus['groups'],'orders':corpus['orders'],'context_mode':'working_set',
                      'retrieval':saved['manifest']['retrieval']}
            selected=json.loads(db.execute('SELECT selected_json FROM character_selection_attempts WHERE turn_id=?',(turn['id'],)).fetchone()[0])
            character=apply_selection(json.loads(turn['character_snapshot']),[m['text'] for m in load_turn_input(db,turn)],item['pool'],selected,4)
            messages,_=module.build_context(engine,db,turn,evidence,character)
        require(messages==saved['messages'],'cr2_legacy_renderer_difference')
        return {'equal':True,'input_hash':fingerprint(messages),'baseline':'sealed CR1 / bb68120 renderer'}
    finally:close(engine)


def blocks(value):
    result=defaultdict(list)
    for row in value['manifest']['prompt_assembly']['blocks']:
        text=value['messages'][row['message_index']]['content'][row['start_character']:row['start_character']+row['characters']]
        require(fingerprint(text)==row['content_hash'],'cr2_block_fingerprint_mismatch')
        result[row['name']].append(text)
    return dict(result)


def compare(old,new):
    require(old['manifest']['model_snapshot_hash']==new['manifest']['model_snapshot_hash'],'cr2_main_parameter_difference')
    left,right=blocks(old),blocks(new)
    changed={name for name in left.keys()|right.keys() if left.get(name)!=right.get(name)}
    allowed={'dialogue_context','character','history','memory_summary'}
    require(changed<=allowed,'cr2_unexplained_block_difference')
    require(left.get('current_input')==right.get('current_input'),'cr2_current_input_difference')
    require(left.get('explicit_references')==right.get('explicit_references'),'cr2_reference_difference')
    return {'identical':old['messages']==new['messages'],'changed_blocks':sorted(changed),
            'explanation':{'dialogue_context':'validated joint result projection',
                           'character':'local/direct versus joint/legacy selection; exact source IDs recorded',
                           'history':'frozen shared-budget pruning','memory_summary':'frozen shared-budget pruning'},
            'old_input_hash':old['input_hash'],'new_input_hash':new['input_hash'],
            'main_parameters_equal':True,'unexplained':[]}


def audit(root):
    root=Path(root);freeze=verify_freeze(root);diffs={};layers={};missing=[];guards=[];legacy=[]
    for key in freeze['items']:
        old=read(root/'results'/(key+'-legacy.json'));new=read(root/'results'/(key+'-automatic.json'))
        legacy.append({'key':key,**legacy_replay(root,read(root/'requests'/(key+'.json')),old)})
        diffs[key]=compare(old,new)
        p=new['pipeline'];pool=read(root/'requests'/(key+'.json'))['pool']
        require('CR2_HIDDEN_CANARY_DO_NOT_TRANSMIT' not in encoded(new['messages'])+encoded(p['shared'])+encoded(pool['payload']),'cr2_hidden_leak')
        require('expected_paths' not in encoded(p['shared']),'cr2_evaluation_label_leak')
        require(p['evidence_hash']==p['routing']['evidence_hash'],'cr2_consumer_binding')
        layers[key]={'route':p['routing'],'shared_source_ids':[s['id'] for s in p['shared']['sources']],
            'selection_returned':p['selection_returned'],'actually_included':p['character'],
            'gap_consumer':p['decision_input'],'consumer_hashes':p['consumers'],
            'final_input_hash':new['input_hash'],'budget':p['budget'],'truncation':p['truncation']}
        expected_returned=p['selection_returned']
        if expected_returned:
            for ident in expected_returned['example_ids']:require(ident in pool['payload']['roots']['example'],'cr2_selection_outside_pool')
        if not pool['payload']['roots']['example']:missing.append(key)
        # Compare wire parameters, not only the snapshot hash.
        wire=[]
        for arm in ('legacy','automatic'):
            rows=[read(f) for f in (root/'receipts'/(key+'-'+arm)).glob('*.reserve.json')]
            main=next(r for r in rows if r['slot'].endswith('/main'))
            request=build_request(main['model'],main['messages'])[0]
            require(fingerprint(request)==main['request_hash'],'cr2_wire_receipt_difference')
            wire.append({k:v for k,v in request.items() if k!='messages'})
            for row in rows:
                term=read(root/'receipts'/(key+'-'+arm)/f'{row["ordinal"]:04d}.terminal.json')
                require(term['status']=='completed' and term['reservation_hash']==fingerprint(row),'cr2_terminal_binding')
                require(term['retry_attempt']['request_hash']==row['request_hash'],'cr2_runtime_receipt_difference')
            store=__import__('persona_runtime.storage',fromlist=['Store']).Store(
                __import__('persona_runtime.paths',fromlist=['RuntimePaths']).RuntimePaths(root/'runtime'/(key+'-'+arm)))
            with store.connection() as db:
                active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
                running=db.execute("SELECT count(*) FROM attempts WHERE status='running'").fetchone()[0]
                native_selector=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0]
                native_observer=db.execute('SELECT count(*) FROM situation_observation_attempts').fetchone()[0]
            require(active==running==native_observer==0,'cr2_native_unsettled')
            require(native_selector==(1 if arm=='legacy' else 0),'cr2_hidden_legacy_selector')
            guards.append({'key':key,'arm':arm,'active':active,'running':running,'native_selector':native_selector,'native_observer':native_observer})
        require(wire[0]==wire[1],'cr2_wire_parameter_difference')
    folder=root/'analysis';folder.mkdir(exist_ok=True)
    atomic_json(folder/'differences.json',diffs);atomic_json(folder/'consumer-views.json',layers)
    atomic_json(folder/'native-terminal-checks.json',guards)
    atomic_json(folder/'legacy-equivalence.json',legacy)
    summary={'version':VERSION,'paired_requests':len(diffs),'identical_main_requests':sum(r['identical'] for r in diffs.values()),
        'explained_differences':sum(not r['identical'] for r in diffs.values()),'unexplained_differences':0,
        'wire_parameters_equal':True,'legacy_renderer_zero_diff':len(legacy),'missing_example_pools':missing,'native_active':0,
        'semantic_accuracy':'unverified','naturalness':'unverified','private_roles':'not_executed','QQ':'not_executed'}
    atomic_json(folder/'audit.json',summary);return summary


def verify_closure(*,worktree=False):
    closure=read(OUT/'closure.json')
    require(closure['stage_complete'] and closure['stage']=='CR2','cr2_stage_incomplete')
    for name,value in closure['evidence_hashes'].items():
        path=(OUT/name).resolve();require(path.is_relative_to(OUT.resolve()) and sha(path)==value,'cr2_sealed_evidence_changed')
    with zipfile.ZipFile(OUT/closure['source_archive']) as archive:
        require(set(archive.namelist())==set(closure['source_hashes']),'cr2_source_inventory_changed')
        for name,value in closure['source_hashes'].items():
            require(hashlib.sha256(archive.read(name)).hexdigest()==value,'cr2_archived_source_changed')
            if worktree:require(sha(ROOT/name)==value,'cr2_worktree_changed')
    return {'stage':'CR2','passed':True,'source_files':len(closure['source_hashes']),
            'evidence_files':len(closure['evidence_hashes']),'writes':0,'generation_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-closure',action='store_true');parser.add_argument('--worktree',action='store_true')
    args=parser.parse_args()
    from scripts.dialogue_refactor import offline
    with offline():
        if args.verify_closure:result=verify_closure(worktree=args.worktree)
        else:
            require(not args.worktree,'cr2_worktree_requires_closure')
            require(not (OUT/'closure.json').exists(),'cr2_stage_sealed');result=audit(OUT/'offline-v3')
        print(encoded(result))


if __name__=='__main__':main()
