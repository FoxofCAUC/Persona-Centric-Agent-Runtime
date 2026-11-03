"""CR3-A artificial-only offline entry. There is deliberately no execute action."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import encoded, fingerprint
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.storage import Store
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_policy import choose
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.models import generic_persona, identifier
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.dialogue_pipeline import DialoguePipeline, collect
from persona_runtime.dialogue_context import route, SharedEvidence, SYSTEM
from persona_runtime.dialogue_communication_pipeline import CommunicationPipeline, VERSION, check_execution
from persona_runtime.communication_consumption import SYSTEM as SYSTEM_V2
from persona_runtime.private_personas import safe
from scripts import dialogue_context as base
from scripts.dialogue_refactor import offline, close, NoDispatch
from scripts.example_calibration_r2 import offline as secondary
from tests.dialogue_communication_fixture import materials, evaluation, case_role_body
from tests.dialogue_communication_responses import joint, reply

HOME = base.HOME
OUT = HOME/'exports/acceptance/PCAR-dialogue-refactor/CR3-A-communication-consumption'
read, sha, write_once, Ledger = base.read, base.sha, base.write_once, base.Ledger


def confined(root):
    return safe(Path(root).absolute(), OUT)


def sources():
    result = base.sources()
    for n in ('scripts/dialogue_communication.py', 'tests/dialogue_communication_fixture.py',
              'tests/dialogue_communication_responses.py'):
        result[n] = sha(ROOT/n)
    return result


def execution_contract():
    return {'version': VERSION, 'private': False, 'conditions': {
        'control': 'dialogue-understanding-v1', 'candidate': 'dialogue-understanding-v2'},
        'old_decision': 'baseline', 'observation': 'disabled', 'capsule': 'disabled'}


async def prepare(root):
    root = confined(root); base.require(not root.exists(), 'communication_prepare_exists');root.mkdir(parents=True)
    started_sources = sources()
    data = materials(); models = base.public_models(); runtime = HOME/'tmp/cr3-consumption'/uuid.uuid4().hex
    labels = evaluation()
    indexed = {(r['case_id'], r['role_id']): r for r in labels['labels']}
    base.require(len(indexed) == len(labels['labels']) == 24, 'communication_label_coverage')
    for case in data['cases']:
        for role, body in data['roles'].items():
            base.require(indexed[case['id'],role]['body_hash'] == fingerprint(case_role_body(case,role,body)), 'communication_label_body_changed')
    labels['evaluation_only_canary'] = 'CR3_EVALUATION_LABELS_DO_NOT_TRANSMIT'
    write_once(root/'materials.json', data);write_once(root/'evaluation-only.json', labels)
    write_once(root/'prompts.json', {'v1': SYSTEM, 'v2': SYSTEM_V2})
    write_once(root/'runtime-owner.json', {'private': False, 'home': str(runtime), 'artifact_root': str(root)})
    policy = choose('hybrid_llm')
    provider = base.IsolatedEmbeddingProvider(RuntimePaths(HOME),
        HOME/'models/character-embedding'/policy['config']['model_fingerprint'], root/'embedding-cache')
    diagnostic = provider.diagnose();write_once(root/'embedding.json', diagnostic)
    base.require(diagnostic['status'] == 'ready', 'communication_local_model_unavailable')
    seed = Store(RuntimePaths(runtime/'seed'));seed.initialize();base.configure(seed.paths, models)
    people = {};service = RuntimeService(seed);chars = CharacterService(seed)
    for role, row in data['roles'].items():
        spec = replace(generic_persona(), name='岑', expression_style=row['style'], initial_background='人工角色，普通朋友，无共同事件。')
        pid = service.create_persona(spec)['id'];service.use_persona(pid)
        character = empty_character();character['identity']['species'] = '人类'
        character['examples'] = [{'id':f'e{n+1}', 'scenario':'日常交流', 'keywords':['过程','取舍','细节'],
            'text':text, 'enabled':True} for n,text in enumerate(row['examples'])]
        character['lorebook'] = [{'id':'hidden','title':'不可见人工稿','category':'private','keywords':['更正'],
            'text':'CR3_HIDDEN_CANARY_DO_NOT_TRANSMIT','priority':0,'enabled':True,'visibility':'creator_only','dependencies':[]}]
        version = chars.update(pid, character, chars.current(pid)['id'], identifier())
        people[role] = {'persona_id':pid, 'character_version':version['id']}
    index = CharacterSemanticIndex(seed, provider)
    for person in people.values():index.build(person['persona_id'], person['character_version'])
    keys = []
    for case in data['cases']:
        for role, person in people.items():
            key = case['id']+'-'+role;store = base.clone(seed, runtime/'admitted'/key)
            cfg = base.configure(store.paths, models, case)
            write_once(store.paths.home/'execution-contract.json', execution_contract())
            attachment = await base.admit(store, cfg, person['persona_id'], case, role)
            attachment['communication_contract_hash'] = sha(store.paths.home/'execution-contract.json')
            engine = ChatEngine(store, cfg, NoDispatch())
            try:
                engine.cognitive.characters.index_factory = lambda _: index
                with store.connection() as db:turn = dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
                from persona_runtime.behavior import resolve_behavior
                scenarios = [s['act'] for s in resolve_behavior('\n\n'.join(case['members']))['signals']]
                pool = engine.cognitive.characters.local_pool(json.loads(turn['character_snapshot']),policy,case['members'],scenarios)
                attachment['pool_hash'] = fingerprint(pool);write_once(store.paths.home/'artificial-cr2.json',attachment)
                gathered = collect(engine, attachment['turn_id'], attachment)
                item = {'key':key,'case_id':case['id'],'role':role,'admitted':str(store.paths.home),
                    'attachment':attachment,'pool':pool,'shared_preview':gathered['shared'].document(),
                    'routing':route(gathered['shared']),'budget_preview':gathered['budget']}
                write_once(root/'requests'/(key+'.json'), item);keys.append(key)
            finally:close(engine)
        print(encoded({'prepared':case['id'],'real_calls':0}),flush=True)
    base.require(started_sources == sources(), 'communication_sources_changed_during_prepare')
    write_once(root/'freeze.json', {'version':VERSION,'sources':started_sources,'items':keys,
        'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json') if 'embedding-cache' not in p.parts}})
    return {'prepared':len(keys),'main_requests':48,'real_calls':0}


def verify_freeze(root):
    f = read(root/'freeze.json');base.require(f['sources']==sources(),'communication_execution_source_changed')
    base.require(read(root/'prompts.json') == {'v1': SYSTEM, 'v2': SYSTEM_V2}, 'communication_prompt_source_mismatch')
    for n,h in f['files'].items():base.require(sha(root/n)==h,'communication_frozen_file_changed')
    return f


class MockClient:
    def __init__(self, case_id, key, arm, role, ledger, fault=None):
        self.case_id,self.key,self.arm,self.role,self.ledger = case_id,key,arm,role,ledger
        self.fault=fault;self.counts={};self.received=[]

    async def complete(self, model, messages):
        base.require(not model.get('_character_selection'),'communication_hidden_selector')
        kind = 'joint' if model.get('_situation_observation') else 'main'
        self.counts[kind] = self.counts.get(kind,0)+1
        row = self.ledger.reserve(self.key+'/'+self.arm+'/'+kind,model,messages);self.received.append(deepcopy(row))
        if self.fault:
            value = self.fault(kind,self.counts[kind],messages)
            if inspect.isawaitable(value):await value
        if kind=='joint':
            payload=json.loads(messages[1]['content']);e=payload['evidence']|{'digest':payload['evidence_hash']}
            answer=encoded(joint(self.case_id,e,self.role,version=1 if self.arm=='control' else 2))
        else:answer=reply(self.case_id,self.role)
        write_once(self.ledger.root/f'{row["ordinal"]:04d}.response.json',
            {'simulation':True,'raw':answer,'input_hash':row['input_hash'],'usage':'mock placeholder, not supplier usage'})
        return ModelCompletion(answer,{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20},
            {'response_model':'mock-not-real','system_fingerprint':'mock','finish_reason':'stop'})


async def run_item(root,item,arm):
    key=item['key'];target=root/'results'/(key+'-'+arm+'.json')
    base.require(not target.exists(),'communication_success_never_replay')
    rowdir=root/'receipts'/(key+'-'+arm);base.require(not rowdir.exists(),'communication_no_automatic_resume')
    rowdir.mkdir(parents=True);ledger=Ledger(rowdir)
    runtime=Path(read(root/'runtime-owner.json')['home']);source=Store(RuntimePaths(Path(item['admitted'])))
    store=base.clone(source,runtime/'executed'/(key+'-'+arm))
    ConfigStore(store.paths).file.write_bytes(ConfigStore(source.paths).file.read_bytes())
    write_once(store.paths.home/'execution-contract.json',read(source.paths.home/'execution-contract.json'))
    attachment=item['attachment'];write_once(store.paths.home/'artificial-cr2.json',attachment)
    client=MockClient(item['case_id'],key,arm,item['role'],ledger)
    cls=DialoguePipeline if arm=='control' else CommunicationPipeline
    pipeline=cls(attachment,item['pool'],ledger.audit,expected_guard=item['shared_preview']['source_guard'])
    engine=ChatEngine(store,ConfigStore(store.paths),client,dialogue_pipeline=pipeline,generation_audit_sink=ledger.audit)
    try:
        check_execution(engine,attachment,arm)
        await engine._run(attachment['turn_id'])
        with store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
            for x in db.execute('SELECT usage_json FROM attempts WHERE turn_id=?',(turn['id'],)):
                if x[0]:ledger.audit(json.loads(x[0]))
            manifest=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid DESC",(turn['id'],)).fetchone()[0]) if turn['status']=='completed' else None
            active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
            old_select=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0]
            old_observe=db.execute('SELECT count(*) FROM situation_observation_attempts').fetchone()[0]
        base.require(turn['status']=='completed','communication_request_failed_'+str(turn.get('error_code')))
        base.require(not active and not old_select and not old_observe,'communication_hidden_or_unsettled')
        base.require(all(x['status']=='completed' for x in ledger.rows),'communication_receipt_incomplete')
        main=next(x for x in ledger.rows if x['slot'].endswith('/main'))
        base.require(main['input_hash']==manifest['input_snapshot_hash'],'communication_manifest_mismatch')
        result={'key':key,'arm':arm,'status':'completed','simulation':True,'counts':client.counts,
            'messages':main['messages'],'input_hash':main['input_hash'],'manifest':manifest,
            'pipeline':pipeline.record,'reply':reply(item['case_id'],item['role']),
            'active_reservations':active,'runtime':str(store.paths.home)}
        write_once(target,result);return result
    finally:close(engine)


async def simulate(root):
    f=verify_freeze(root)
    base.require(not (root/'run.started').exists(),'communication_no_automatic_resume');write_once(root/'run.started',{'simulation':True})
    completed=0
    try:
        for key in f['items']:
            for arm in ('control','candidate'):
                await run_item(root,read(root/'requests'/(key+'.json')),arm);completed+=1
                atomic_json(root/'state.json',{'status':'running','main_completed':completed,'real_calls':0})
        atomic_json(root/'state.json',{'status':'completed','main_completed':completed,'real_calls':0})
    except BaseException:
        atomic_json(root/'state.json',{'status':'stopped','main_completed':completed,'real_calls':0});raise
    return report(root)


def report(root):
    f=verify_freeze(root);labels=read(root/'evaluation-only.json');rows=[];diffs=[];consumers=[];routing_differences=[]
    from scripts.dialogue_context_audit import blocks
    for key in f['items']:
        pair=[];item=read(root/'requests'/(key+'.json'))
        for arm in ('control','candidate'):
            v=read(root/'results'/(key+'-'+arm+'.json'));base.require(v['simulation'] is True,'communication_not_mock')
            expected={'main':1} if item['routing']['path']=='direct' else {'joint':1,'main':1}
            base.require(v['counts']==expected,'communication_call_structure')
            base.require(v['input_hash']==fingerprint(v['messages'])==v['manifest']['input_snapshot_hash'],'communication_input_changed')
            base.require('CR3_HIDDEN_CANARY_DO_NOT_TRANSMIT' not in encoded(v['messages']),'communication_hidden_material')
            base.require(labels['evaluation_only_canary'] not in encoded(v['messages']),'communication_label_leak')
            base.require(v['pipeline']['routing']==item['routing'],'communication_route_changed')
            rows.append({'key':key,'arm':arm,'counts':v['counts'],'input_hash':v['input_hash']});pair.append(v)
            if arm=='candidate':
                actual=blocks(v).get('communication_spec',[])
                base.require(actual==([v['pipeline']['projection']] if v['pipeline']['projection'] else []),'communication_consumer_not_actual')
                base.require('dialogue_context' not in blocks(v),'communication_double_projection')
                consumers.append({'key':key,'route':item['routing']['path'],'actual_block_hash':fingerprint(actual),
                    'spec':v['pipeline']['communication_spec'],'gap_consumer':v['pipeline']['decision_input'],
                    'final_input_hash':v['input_hash'],'understanding_status':v['pipeline']['understanding_status'],
                    'trimmed':v['pipeline']['projection_removed']})
                if item['routing']['path']!=labels['expected_paths'][item['case_id']]:
                    routing_differences.append({'key':key,'expected':labels['expected_paths'][item['case_id']],'actual':item['routing']['path']})
        left,right=map(blocks,pair);changed=sorted(k for k in left.keys()|right.keys() if left.get(k)!=right.get(k))
        base.require(set(changed)<={'dialogue_context','communication_spec'},'communication_unexplained_difference')
        base.require(pair[0]['manifest']['model_snapshot_hash']==pair[1]['manifest']['model_snapshot_hash'],'communication_parameters_changed')
        base.require(pair[0]['pipeline']['character']==pair[1]['pipeline']['character'],'communication_selected_material_changed')
        diffs.append({'key':key,'changed_blocks':changed,'same_messages':pair[0]['messages']==pair[1]['messages'],'selected_materials_equal':True,'parameters_equal':True})
    calls=sum(sum(x['counts'].values()) for x in rows)
    result={'version':VERSION,'simulation':True,'main_requests':len(rows),'mock_generations':calls,'rows':rows,
        'differences':diffs,'routing_differences':routing_differences,'real_calls':0,'real_spec_accuracy':'unverified','persona_distinction':'unverified','naturalness':'unverified'}
    atomic_json(root/'consumer-views.json',consumers)
    atomic_json(root/'report.json',result);return result


def verify(root):
    s=report(root);base.require(s['main_requests']==48,'communication_simulation_incomplete')
    expected=48+2*sum(read(root/'requests'/(k+'.json'))['routing']['path']=='joint' for k in read(root/'freeze.json')['items'])
    base.require(s['mock_generations']==expected,'communication_mock_count')
    calls=0
    for p in (root/'receipts').rglob('*.reserve.json'):
        row=read(p);t=read(p.with_name(p.name.replace('.reserve.','.terminal.')))
        base.require(t['status']=='completed' and t['reservation_hash']==fingerprint(row),'communication_receipt_binding');calls+=1
        base.require(t['retry_attempt']['request_hash']==row['request_hash'],'communication_native_audit_binding')
    base.require(calls==expected,'communication_receipt_count')
    from tests.prompt_baseline_fixture import capture_all
    frozen=read(ROOT/'tests/fixtures/prompt-baseline-r0.json');actual=capture_all()
    equal={k:encoded(v)==encoded(frozen['scenarios'].get(k)) for k,v in actual.items()}
    atomic_json(root/'r0-equivalence.json',{'count':len(equal),'passed':len(equal)==34 and all(equal.values()),'scenarios':equal})
    base.require(len(equal)==34 and all(equal.values()),'communication_r0_difference')
    result={'passed':True,'main_requests':48,'mock_generations':calls,'real_calls':0,'R0_zero_diff':34}
    atomic_json(root/'verification.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'offline-v1');args=parser.parse_args();root=confined(args.root)
    base.require(not (OUT/'closure.json').exists(),'communication_stage_sealed')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp');os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    with offline() as net,secondary() as net2:
        try:
            result=asyncio.run(prepare(root)) if args.action=='prepare' else asyncio.run(simulate(root)) if args.action=='simulate' else verify(root) if args.action=='verify' else report(root)
        finally:
            if root.exists():atomic_json(root/('network-'+args.action+'.json'),{'primary':net,'secondary':net2,'real_calls':0})
    print(encoded({k:v for k,v in result.items() if k not in ('rows','differences')}))


if __name__=='__main__':main()
