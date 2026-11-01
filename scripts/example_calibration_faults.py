"""Twelve R2 fault groups, exclusively in artificial isolated stores."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from scripts.example_calibration_r2 import (configure,collect,reconcile,close,new_policy,RuntimePaths,Store,
    RuntimeService,CharacterService,PersonaSpec,identifier,ChatEngine,NoDispatch,write_once,read,
    encoded,fingerprint,build_request,RetryReceipts,StopRun,ModelCompletion,seed_turn,OUT,confined)
from tests.example_calibration_fixture import SPEC,character,fixture
from persona_runtime.character_selector_protocol import candidates,parse_output
from persona_runtime.character_semantic_index import sources
from persona_runtime.character_retrieval import load_config
from persona_runtime.character_runtime import validate_snapshot
from persona_runtime.chat import ModelError
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.reply_batch import validate_reply

GROUPS={
 'F01':['duplicate'],'F02':['cross_kind'],'F03':['outside'],'F04':['hidden_disabled'],
 'F05':['dependency'],'F06':['source_withdrawn'],'F07':['reference_revoked'],'F08':['budget'],
 'F09':['final_recheck'],'F10':['transient'],'F11':['exhausted'],'F12':['cancel','incomplete_receipt']}

async def no_wait(seconds):pass


async def exercise(root,mutation):
    if root.exists():raise StopRun('fault_evidence_exists')
    root.mkdir(parents=True)
    ledger=RetryReceipts(root/'receipts',limit=8)
    if mutation=='incomplete_receipt':
        ledger.reserve_attempt('interrupted-process',{'request':{'messages':[]}})
        blocked=False
        try:ledger.reserve_attempt('next',{'request':{'messages':[]}})
        except StopRun:blocked=True
        assert blocked and len(ledger.rows())==1
        result={'mutation':mutation,'passed':True,'provider_dispatches':0,'expected_open_receipt':True,'new_dispatch_blocked':True}
        write_once(root/'result.json',result);return result
    paths=RuntimePaths(root/'runtime');store=Store(paths);store.initialize();store.character_committed=lambda *_:[]
    approved={'model':{'base_url':'https://api.uniapi.io/v1','name':'grok-4.7','timeout':180,'max_tokens':8192,'max_input_chars':24000,'hourly_limit':0},
      'character_selector':{'enabled':True,'base_url':'https://api.uniapi.io/v1','name':'grok-4.7','timeout':60,'max_tokens':512,'max_input_chars':48000,'hourly_limit':0,'daily_limit':0}}
    case=deepcopy(fixture()['cases'][22] if mutation=='reference_revoked' else fixture()['cases'][0])
    cfg=configure(paths,approved,case);service=RuntimeService(store)
    pid=service.create_persona(PersonaSpec.from_dict(SPEC))['id'];service.use_persona(pid);sid=service.create_session()['id']
    data=character('detail')
    if mutation=='hidden_disabled':
        data['lorebook'][0]['visibility']='creator_only';data['lorebook'][1]['dependencies']=[];data['examples'][0]['enabled']=False
    if mutation=='dependency':data['lorebook'][0]['enabled']=False
    if mutation=='budget':
        for e in data['examples']:e['text']='一段用于离线预算验证的材料。'*25
    chars=CharacterService(store);version=chars.update(pid,data,chars.current(pid)['id'],identifier())
    engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
    try:
        if case['reference']:
            with store.connection(write=True) as db:seed_turn(db,pid,sid,'r2-fault-ref',[case['reference']['text']],reply=case['reference']['reply'])
        with patch.object(engine,'_schedule'):
            ingress=new_policy(engine,pid,'character-selector-v3');receipts=[]
            for n,text in enumerate(case['members']):
                p={'persona_id':pid,'session_id':sid,'text':text,'request_id':identifier()}
                if n==0 and case['reference']:p['reference']={'kind':'turn','id':'r2-fault-ref'}
                receipts.append(ingress.accept(p))
            b=ingress.get(receipts[0]['batch_id']);tid=ingress.action(b['id'],'seal',b['revision'],identifier(),pid)['turn_id']
        semantic={kind:[r|{'score':.8} for r in sources(version) if r['kind']==kind] for kind in ('lore','example')}
        pool=candidates(version,case['members'],semantic,load_config())
        if mutation=='hidden_disabled':
            assert 'station' not in pool['payload']['roots']['lore'] and 'progress' not in pool['payload']['roots']['example']
        if mutation=='dependency':assert 'inventory' not in pool['payload']['roots']['lore']
        chosen=pool['payload']['roots']['example'][:2 if mutation=='budget' else 1]
        answer={'schema_version':1,'lore_ids':[],'example_ids':chosen}
        if mutation=='duplicate':answer['example_ids']=chosen[:1]*2
        if mutation=='cross_kind':answer['example_ids']=['station']
        if mutation=='outside':answer['example_ids']=['not_in_candidates']
        unavailable=False;calls=[]
        async def complete(model,messages):
            nonlocal unavailable
            kind='selector' if model.get('_character_selection') else 'main'
            row=ledger.reserve_attempt('fault/'+kind,{'item':'fault','kind':kind,'simulation':True,
                'request':build_request(model,messages)[0],'input_hash':fingerprint(messages)})
            calls.append(kind);text=None
            try:
                if mutation=='cancel':raise asyncio.CancelledError('artificial_user_cancel')
                if mutation=='exhausted' or (mutation=='transient' and row['attempt_index']==0):
                    raise ModelError('provider_timeout' if kind=='selector' else 'provider_empty_response')
                if kind=='selector':
                    if mutation=='source_withdrawn':unavailable=True
                    if mutation=='reference_revoked':
                        with store.connection(write=True) as db:db.execute('INSERT INTO source_exclusions VALUES (?,?,?,?,?)',('r2-fault-ref-e1',pid,'00000000-0000-4000-8000-000000000001','R2 artificial withdrawal','2026-11-01'))
                    text=encoded(answer);parse_output(text,pool,4)
                else:
                    text=encoded({'schema_version':3,'behavior_intent':'respond','messages':[{'content':'离线故障验证回复。','content_kind':'chat','cancel_if_new_input':False}]})
                    validate_reply(text,model['_reply_mode'],model['_expression_snapshot'])
            except BaseException as error:
                ledger.finish(row,'interrupted' if isinstance(error,asyncio.CancelledError) else 'failed',error=str(error),output=text,usage=None)
                raise
            result=ModelCompletion(text,{'total_tokens':0},{'finish_reason':'stop','system_fingerprint':'sim_fault'})
            ledger.finish(row,'completed',output=text,usage=result[1],diagnostics=result.diagnostics)
            return result
        def source_check(conn,turn):
            if unavailable:raise RuntimeErrorBase('character_snapshot_invalid')
            return validate_snapshot(conn,turn)
        original_prepare=engine.cognitive.prepare
        def prepare(*a,**k):
            result=original_prepare(*a,**k)
            if mutation=='final_recheck':result['character_prepared']['_guard']='artificial_stale_guard'
            return result
        engine.client.complete=complete;engine.selector.client=engine.observer.client=engine.client
        with patch.object(engine.cognitive.characters,'local_pool',return_value=pool),patch.object(engine.cognitive,'prepare',side_effect=prepare),\
             patch('persona_runtime.character_selector.validate_snapshot',side_effect=source_check),patch('persona_runtime.generation_retry.retry_delay',no_wait):
            try:await engine._run(tid)
            except asyncio.CancelledError:
                if mutation!='cancel':raise
        value=collect(engine,{'key':'fault','turn_id':tid});rows=ledger.rows();reconcile(value,rows)
        if mutation in ('duplicate','cross_kind','outside','exhausted'):
            assert calls==['selector']*4 and value['turn']['error_code']=='provider_retries_exhausted'
        elif mutation in ('source_withdrawn','reference_revoked'):
            assert calls==['selector'] and value['turn']['status']!='completed'
        elif mutation=='cancel':
            assert calls==['selector'] and rows[0]['status']=='interrupted'
        else:
            assert value['turn']['status']=='completed',value['turn'].get('error_code')
            assert calls==(['selector','selector','main','main'] if mutation=='transient' else ['selector','main'])
            manifest=next(m for m in value['manifests'] if m['task_type']=='dialogue')
            assert manifest['input_snapshot_hash']==next(r for r in rows if r['kind']=='main')['input_hash']
            material=manifest['prompt_assembly']['material_selection']
            if mutation=='budget':assert len(material['returned_example_ids'])>len(material['included_example_ids'])
            if mutation=='final_recheck':assert material['fallback_reason'] and material['applied_prompt'] is None
            before=len(calls);await engine._run(tid);assert len(calls)==before,'success_replayed'
        result={'mutation':mutation,'passed':True,'provider_dispatches':len(calls),'simulation':True,'real_calls':0,
            'calls':calls,'turn_status':value['turn']['status'],'error_code':value['turn'].get('error_code'),
            'runtime_audit_matches_receipts':True,'source_withdrawal_method':'inject source reader refusal; accepted snapshot untouched' if mutation=='source_withdrawn' else None}
        write_once(root/'runtime-result.json',value);write_once(root/'pool.json',pool);write_once(root/'result.json',result)
        return result
    finally:await close(engine)


async def run_faults(root):
    confined(root,OUT)
    if root.exists():raise StopRun('fault_run_exists')
    root.mkdir();results=[]
    for group,mutations in GROUPS.items():
        rows=[]
        for mutation in mutations:rows.append(await exercise(root/group/mutation,mutation))
        results.append({'group':group,'passed':all(r['passed'] for r in rows),'variants':rows})
    result={'groups':12,'subcases':sum(len(r['variants']) for r in results),'passed':all(r['passed'] for r in results),
        'results':results,'real_calls':0,'qq':0}
    write_once(root/'summary.json',result);return result
