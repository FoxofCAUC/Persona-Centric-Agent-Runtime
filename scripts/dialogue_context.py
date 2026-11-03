"""CR2 artificial-only offline prepare/simulate/report/verify; no real execute action."""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.chat import ChatEngine,ModelCompletion
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.ingress import IngressService
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.character_policy import choose
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.provider_compat import build_request
from persona_runtime.dialogue_voice import freeze as freeze_voice
from persona_runtime.dialogue_context import (SharedEvidence,route,ROUTING_RULES,SYSTEM,UNDERSTANDING_VERSION,require)
from persona_runtime.dialogue_pipeline import (DialoguePipeline,VERSION,FROZEN,HOME,OUT,collect)
from persona_runtime.private_personas import safe
from scripts.dialogue_refactor import offline,close,NoDispatch
from scripts.example_calibration_r2 import IsolatedEmbeddingProvider,offline as offline_guard
from scripts.dialogue_diagnosis_runtime import public_models,write_once,configure
from tests.m71_fixtures import seed_turn
from tests.dialogue_context_fixture import materials,evaluation
from tests.dialogue_context_responses import selection,joint,reply

def read(path): return json.loads(Path(path).read_text('utf-8'))
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def confined(root):
    root=Path(root).absolute()
    return safe(root,HOME/'tmp' if root.is_relative_to(HOME/'tmp') else OUT)


def sources():
    names=set(p.relative_to(ROOT).as_posix() for p in (ROOT/'persona_runtime').rglob('*') if p.suffix in ('.py','.json'))
    names.update(['scripts/dialogue_context.py','scripts/dialogue_refactor.py','scripts/example_calibration_r2.py',
        'scripts/dialogue_diagnosis_runtime.py','tests/dialogue_context_fixture.py','tests/dialogue_context_responses.py','tests/m71_fixtures.py'])
    return {n:sha(ROOT/n) for n in sorted(names)}


def clone(source,target):
    target=confined(target);require(not target.exists(),'cr2_clone_exists')
    dest=Store(RuntimePaths(target));dest.paths.initialize()
    with closing(sqlite3.connect(source.path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dest.path)) as dst: src.backup(dst)
    dest.initialize();return dest


async def admit(store,cfg,pid,case,role):
    engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
    try:
        service=RuntimeService(store);service.use_persona(pid);sid=service.create_session()['id']
        with store.connection(write=True) as db:
            for n,pair in enumerate(case['history']): seed_turn(db,pid,sid,f'cr2-{case["id"]}-{role}-h{n}',[pair[0]],reply=pair[1])
        ingress=IngressService(engine);old=ingress.current_policy(pid,'desktop')
        ingress.update_policy(pid,'desktop','natural_batch',old['id'],identifier(),reply_mode='natural_structured',
            context_mode='working_set',cross_session=False,character_retrieval_mode='hybrid_llm',
            disposition_mode='baseline',situation_observation_mode='disabled',expression_policy_version=3)
        with patch.object(engine,'_schedule'):
            for n,text in enumerate(case['members']):
                payload={'channel':'desktop','persona_id':pid,'session_id':sid,'text':text,'request_id':identifier()}
                if case['reference'] and n==0: payload['reference']={'kind':'event','id':f'cr2-{case["id"]}-{role}-h0-e1'}
                receipt=ingress.accept(payload)
            batch=ingress.get(receipt['batch_id'])
            tid=ingress.action(batch['id'],'seal',batch['revision'],identifier(),pid)['turn_id']
        with store.connection() as db: turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
        return {'version':VERSION,'private':False,'persona_id':pid,'turn_id':tid,'execution_revision':turn['execution_revision'],
                'snapshots':{k:fingerprint(turn[k]) for k in FROZEN},'config_hash':sha(cfg.file)}
    finally: close(engine)


async def prepare(root):
    root=confined(root);require(not root.exists(),'cr2_prepare_exists');root.mkdir(parents=True)
    data=materials();models=public_models()
    write_once(root/'materials.json',data);write_once(root/'evaluation-only.json',evaluation())
    write_once(root/'router-rules.json',ROUTING_RULES)
    write_once(root/'understanding-prompt.json',{'version':UNDERSTANDING_VERSION,'text':SYSTEM,'hash':fingerprint(SYSTEM)})
    policy=choose('hybrid_llm')
    provider=IsolatedEmbeddingProvider(RuntimePaths(HOME),HOME/'models/character-embedding'/policy['config']['model_fingerprint'],root/'embedding-cache')
    diagnostic=provider.diagnose();write_once(root/'embedding.json',diagnostic)
    require(diagnostic['status']=='ready','cr2_local_model_unavailable')
    seed=Store(RuntimePaths(root/'seed'));seed.initialize();configure(seed.paths,models)
    service=RuntimeService(seed);chars=CharacterService(seed);people={}
    for role,row in data['roles'].items():
        spec=replace(generic_persona(),name='岑',expression_style=row['style'],initial_background='人工角色，普通朋友，无共同事件。')
        pid=service.create_persona(spec)['id'];service.use_persona(pid)
        character=empty_character();character['identity']['species']='人类'
        character['examples']=[{'id':f'e{n+1}','scenario':'日常交流','keywords':['过程','取舍','细节','时间'],
            'text':text,'enabled':True} for n,text in enumerate(row['examples'])]
        character['lorebook']=[{'id':'hidden','title':'不可见人工稿','category':'private','keywords':['更正'],
            'text':'CR2_HIDDEN_CANARY_DO_NOT_TRANSMIT','priority':0,'enabled':True,'visibility':'creator_only','dependencies':[]}]
        v=chars.update(pid,character,chars.current(pid)['id'],identifier());people[role]={'persona_id':pid,'character_version':v['id']}
    index=CharacterSemanticIndex(seed,provider)
    for person in people.values(): index.build(person['persona_id'],person['character_version'])
    items=[]
    for case in data['cases']:
        for role,person in people.items():
            key=case['id']+'-'+role;store=clone(seed,root/'admitted'/key);cfg=configure(store.paths,models,case)
            # The experimental recipe is frozen before normal admission; it never rewrites native snapshots.
            write_once(store.paths.home/'execution-contract.json',{'version':VERSION,'paths':['legacy','automatic'],
                'models':models,'routing_rules_hash':fingerprint(ROUTING_RULES),'automatic_aux_max_tokens':2048})
            attachment=await admit(store,cfg,person['persona_id'],case,role)
            engine=ChatEngine(store,cfg,NoDispatch())
            try:
                engine.cognitive.characters.index_factory=lambda _:index
                with store.connection() as db: turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
                from persona_runtime.behavior import resolve_behavior
                scenarios=[s['act'] for s in resolve_behavior('\n\n'.join(case['members']))['signals']]
                pool=engine.cognitive.characters.local_pool(json.loads(turn['character_snapshot']),policy,case['members'],scenarios)
                attachment['pool_hash']=fingerprint(pool)
                write_once(store.paths.home/'artificial-cr2.json',attachment)
                gathered=collect(engine,attachment['turn_id'],attachment)
                item={'key':key,'case_id':case['id'],'role':role,'admitted':str(store.paths.home),
                    'attachment':attachment,'pool':pool,'shared_preview':gathered['shared'].document(),
                    'budget_preview':gathered['budget']}
                write_once(root/'requests'/(key+'.json'),item);items.append(key)
            finally:close(engine)
        print(encoded({'prepared_scene':case['id'],'real_calls':0}),flush=True)
    write_once(root/'freeze.json',{'version':VERSION,'sources':sources(),'items':items,
        'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json')
                 if not any(x in p.parts for x in ('seed','admitted','embedding-cache'))}})
    return {'items':len(items),'main_positions':64,'real_calls':0}


def verify_freeze(root):
    freeze=read(root/'freeze.json');require(freeze['sources']==sources(),'cr2_execution_source_changed')
    for name,value in freeze['files'].items():require(sha(root/name)==value,'cr2_frozen_file_changed')
    return freeze


class Ledger:
    def __init__(self,root):self.root=Path(root);self.rows=[];self.broken=False;self.seen_attempt_ids=set()
    def reserve(self,slot,model,messages):
        require(not self.broken,'cr2_ledger_broken')
        require(not any(r['status']=='reserved' for r in self.rows),'cr2_unsettled_receipt')
        old=[r for r in self.rows if r['slot']==slot]
        require(len(old)<4 and not any(r['status']=='completed' for r in old),'cr2_success_never_replay')
        request=build_request(model,messages)[0];digest=fingerprint(request)
        require(not old or all(r['request_hash']==digest for r in old),'cr2_retry_request_changed')
        row={'ordinal':len(self.rows)+1,'slot':slot,'status':'reserved','request_hash':digest,
             'input_hash':fingerprint(messages),'model':{k:v for k,v in model.items() if k!='encrypted_key'},'messages':messages,'simulation':True}
        try: write_once(self.root/f'{row["ordinal"]:04d}.reserve.json',row)
        except BaseException:self.broken=True;raise
        self.rows.append(row);return row
    def audit(self,state):
        for attempt in state.get('generation_retry',{}).get('attempts',[]):
            if attempt['status']=='running' or attempt['id'] in self.seen_attempt_ids:continue
            row=next((r for r in self.rows if r['request_hash']==attempt['request_hash'] and r['status']=='reserved'),None)
            if row is None:continue
            status=attempt['status'];terminal={'status':status,'retry_attempt':attempt,'reservation_hash':fingerprint(row),'simulation':True}
            try:write_once(self.root/f'{row["ordinal"]:04d}.terminal.json',terminal)
            except BaseException:self.broken=True;raise
            row['status']=status
            self.seen_attempt_ids.add(attempt['id'])
            if status=='failed' and attempt['finished_epoch']-attempt['started_epoch']>600:
                self.broken=True;raise RuntimeErrorBase('cr2_slow_physical_failure')


class MockClient:
    def __init__(self,case_id,key,arm,ledger,fault=None):
        self.case_id,self.key,self.arm,self.ledger=case_id,key,arm,ledger
        self.fault=fault;self.counts={};self.received=[]
    async def complete(self,model,messages):
        if model.get('_character_selection'):kind='selection'
        elif model.get('_situation_observation'):kind='joint'
        else:kind='main'
        self.counts[kind]=self.counts.get(kind,0)+1
        row=self.ledger.reserve(self.key+'/'+self.arm+'/'+kind,model,messages)
        self.received.append(deepcopy(row))
        if self.fault:self.fault(kind,self.counts[kind],messages)
        if kind=='selection':answer=encoded(selection(self.case_id))
        elif kind=='joint':
            payload=json.loads(messages[1]['content']);e=payload['evidence']|{'digest':payload['evidence_hash']}
            answer=encoded(joint(self.case_id,e))
        else:answer=reply(self.case_id)
        return ModelCompletion(answer,{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20},
            {'response_model':'mock-not-real','system_fingerprint':'mock','finish_reason':'stop'})


async def run_item(root,item,arm):
    key=item['key'];result_path=root/'results'/(key+'-'+arm+'.json')
    require(not result_path.exists(),'cr2_success_never_replay')
    rowdir=root/'receipts'/(key+'-'+arm);require(not rowdir.exists(),'cr2_existing_receipt_no_resume')
    rowdir.mkdir(parents=True);ledger=Ledger(rowdir)
    source=Store(RuntimePaths(Path(item['admitted'])))
    store=clone(source,root/'runtime'/(key+'-'+arm))
    ConfigStore(store.paths).file.write_bytes(ConfigStore(source.paths).file.read_bytes())
    attachment=item['attachment'];write_once(store.paths.home/'artificial-cr2.json',attachment)
    client=MockClient(item['case_id'],key,arm,ledger)
    pipeline=DialoguePipeline(attachment,item['pool'],ledger.audit,expected_guard=item['shared_preview']['source_guard']) if arm=='automatic' else None
    engine=ChatEngine(store,ConfigStore(store.paths),client,dialogue_pipeline=pipeline,generation_audit_sink=ledger.audit)
    engine.selector.audit_sink=ledger.audit
    try:
        if arm=='legacy':
            # Freeze identical recalled candidates; normal selector parsing/audit still executes.
            engine.cognitive.characters.local_pool=lambda *_:deepcopy(item['pool'])
        # Observe native main audit after protocol validation, including failed attempts.
        await engine._run(attachment['turn_id'])
        with store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
            for a in db.execute('SELECT usage_json FROM attempts WHERE turn_id=?',(turn['id'],)):
                if a[0]:ledger.audit(json.loads(a[0]))
            contexts=[json.loads(r[0]) for r in db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(turn['id'],))]
            active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
        require(turn['status']=='completed','cr2_request_failed_'+str(turn.get('error_code')))
        require(not active and all(r['status']=='completed' for r in ledger.rows),'cr2_unsettled_receipt')
        main=[r for r in ledger.rows if r['slot'].endswith('/main')]
        require(len(main)==1 and main[0]['input_hash']==contexts[-1]['input_snapshot_hash'],'cr2_main_manifest_mismatch')
        result={'key':key,'arm':arm,'status':'completed','simulation':True,'counts':client.counts,
            'messages':main[0]['messages'],'input_hash':main[0]['input_hash'],'request_hash':main[0]['request_hash'],
            'manifest':contexts[-1],'pipeline':pipeline.record if pipeline else None,'reply':reply(item['case_id']),
            'receipts':len(ledger.rows),'active_reservations':active}
        write_once(result_path,result);return result
    finally:close(engine)


async def simulate(root):
    freeze=verify_freeze(root)
    for key in freeze['items']:
        item=read(root/'requests'/(key+'.json'))
        for arm in ('legacy','automatic'):await run_item(root,item,arm)
        print(encoded({'simulated_pair':key,'real_calls':0}),flush=True)
    return report(root)


def report(root):
    freeze=verify_freeze(root);labels=read(root/'evaluation-only.json');rows=[];routes={};misses=[];over=[]
    for key in freeze['items']:
        for arm in ('legacy','automatic'):
            path=root/'results'/(key+'-'+arm+'.json')
            if not path.exists():rows.append({'key':key,'arm':arm,'status':'not_completed'});continue
            value=read(path);require(value['simulation'] is True,'cr2_simulation_type')
            require(fingerprint(value['messages'])==value['input_hash']==value['manifest']['input_snapshot_hash'],'cr2_input_hash_changed')
            if arm=='automatic':
                p=value['pipeline'];e=SharedEvidence(encoded(p['shared']));r=route(e)
                require(r==p['routing'],'cr2_router_replay_difference');routes[key]=r['path']
                if r['path']!=labels['expected_paths'][key.split('-')[0]]:
                    (misses if r['path']=='direct' else over).append(key)
                require(value['counts']==({'main':1} if r['path']=='direct' else {'joint':1,'main':1}),'cr2_aux_structure')
            else:require(value['counts']=={'selection':1,'main':1},'cr2_legacy_structure')
            rows.append({'key':key,'arm':arm,'status':'completed','counts':value['counts'],'input_hash':value['input_hash']})
    totals={kind:sum(r.get('counts',{}).get(kind,0) for r in rows) for kind in ('selection','joint','main')}
    result={'stage':'CR2','simulation':True,'rows':rows,'completed':sum(r['status']=='completed' for r in rows),
        'generations':totals,'total_generations':sum(totals.values()),'routes':routes,
        'direct_ratio':sum(v=='direct' for v in routes.values())/len(routes) if routes else None,
        'routing_misses':misses,'over_routing':over,'real_calls':0,'real_routing_accuracy':'unverified',
        'real_understanding_accuracy':'unverified','real_expression_effect':'unverified'}
    atomic_json(root/'report.json',result);return result


def verify(root):
    result=report(root);require(result['completed']==64 and result['total_generations']==112,'cr2_simulation_incomplete')
    require(not result['routing_misses'] and not result['over_routing'],'cr2_routing_label_mismatch')
    count=0
    for reserve in (root/'receipts').rglob('*.reserve.json'):
        row=read(reserve);terminal=read(reserve.with_name(reserve.name.replace('.reserve.','.terminal.')))
        require(terminal['status']=='completed' and terminal['reservation_hash']==fingerprint(row),'cr2_receipt_mismatch');count+=1
    require(count==112,'cr2_receipt_count')
    from tests.prompt_baseline_fixture import capture_all
    baseline=read(ROOT/'tests/fixtures/prompt-baseline-r0.json');actual=capture_all()
    values={k:encoded(v)==encoded(baseline['scenarios'].get(k)) for k,v in actual.items()}
    atomic_json(root/'r0-equivalence.json',{'count':len(values),'equal':all(values.values()),'scenarios':values})
    require(len(values)==34 and all(values.values()),'cr2_r0_difference')
    atomic_json(root/'verification.json',{'passed':True,'main_requests':64,'physical_mock_generations':112,'real_calls':0})
    return {'passed':True,'main_requests':64,'mock_generations':112,'R0':34,'real_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'offline-v3');args=parser.parse_args();root=confined(args.root)
    require(not (OUT/'closure.json').exists(),'cr2_stage_sealed_use_readonly_closure_check')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp')
    with offline() as network,offline_guard() as secondary:
        result=asyncio.run(prepare(root)) if args.action=='prepare' else asyncio.run(simulate(root)) if args.action=='simulate' else report(root) if args.action=='report' else verify(root)
    atomic_json(root/('network-'+args.action+'.json'),{'primary':network,'secondary':secondary,'real_calls':0})
    print(encoded(result if args.action!='simulate' else {k:v for k,v in result.items() if k not in ('rows','routes')}))


if __name__=='__main__':main()
