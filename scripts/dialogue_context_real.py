"""CR2.1 isolated real diagnosis. Only explicit execute of a real-prepared batch can send HTTP."""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.chat import ChatEngine,ChatCompletionsClient,ModelCompletion
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.provider_compat import build_request,usage_fields
from persona_runtime.generation_retry import safe_diagnostics
from persona_runtime.dialogue_pipeline import DialoguePipeline,collect,current,source_guard,FROZEN
from persona_runtime.dialogue_context import route,require
from persona_runtime.character_embedding import private_directory
from persona_runtime.private_personas import safe,guard_cloud_dispatch
from scripts import dialogue_context as base
from scripts.dialogue_refactor import offline,close,NoDispatch
from scripts.example_calibration_r2 import offline as secondary_offline
from scripts.dialogue_diagnosis_runtime import write_once

HOME=Path('D:/Persona-Centric Agent Runtime')
OUT=HOME/'exports/acceptance/PCAR-dialogue-refactor/CR2.1-real-diagnosis'
PARENT=OUT.parent/'CR2-shared-context/offline-v3'
VERSION='cr2-real-v1'
PROTOCOL=ROOT/'tests/fixtures/dialogue-context-real-protocol-v1.json'
read,sha=base.read,base.sha


def public(model):return {k:deepcopy(v) for k,v in model.items() if k!='encrypted_key'}


def sources():
    values=base.sources()
    values['scripts/dialogue_context_real.py']=sha(Path(__file__))
    values['tests/fixtures/dialogue-context-real-protocol-v1.json']=sha(PROTOCOL)
    return values


def confined(root):
    root=Path(root).absolute();return safe(root,OUT)


def clone(source,target):
    target=safe(Path(target),HOME/'tmp/cr2-real');require(not target.exists(),'cr21_runtime_exists')
    dest=Store(RuntimePaths(target));dest.paths.initialize()
    with closing(sqlite3.connect(source.path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dest.path)) as dst:src.backup(dst)
    dest.initialize();return dest


def position_order(keys):
    scenes=['S01','S11']+[f'S{n:02d}' for n in range(1,17) if n not in (1,11)]
    rows=[]
    for n,scene in enumerate(scenes):
        for r in (1,2):
            for role_index,role in enumerate(('process','candid')):
                arms=('legacy','automatic') if (n+r+role_index)%2==0 else ('automatic','legacy')
                for arm in arms:rows.append({'id':f'{scene}-{role}-{arm}-r{r}','key':scene+'-'+role,
                    'case_id':scene,'role_id':role,'arm':arm,'replicate':r})
    require(set(row['key'] for row in rows)==set(keys),'cr21_position_keys')
    return rows


async def prepare(root,real=False):
    root=confined(root);require(not root.exists(),'cr21_prepare_exists');root.mkdir(parents=True)
    old=read(PARENT/'freeze.json')
    for name,value in old['files'].items():require(sha(PARENT/name)==value,'cr21_parent_evidence_changed')
    data=read(PARENT/'materials.json');batch=uuid.uuid4().hex
    runtime=HOME/'tmp/cr2-real'/batch
    private_directory(HOME,runtime)
    write_once(runtime/'owner.json',{'version':VERSION,'batch':batch,'artifact_root':str(root),'private':False})
    native=ConfigStore(RuntimePaths(HOME)).load()
    # DPAPI ciphertext is isolated; never copied into native snapshots or public evidence.
    if real:
        require(all(native[k].get('encrypted_key') for k in ('model','character_selector')),'cr21_credentials_missing')
        write_once(runtime/'credentials.dpapi.json',{k:native[k] for k in ('model','character_selector')})
    write_once(root/'materials.json',data)
    write_once(root/'evaluation-only.json',read(PARENT/'evaluation-only.json'))
    write_once(root/'protocol.json',read(PROTOCOL))
    items={}
    for key in old['items']:
        source_item=read(PARENT/'requests'/(key+'.json'));case=next(c for c in data['cases'] if c['id']==source_item['case_id'])
        store=clone(Store(RuntimePaths(PARENT/'seed')),runtime/'admitted'/key)
        cfg=ConfigStore(store.paths);cfg.file.parent.mkdir(parents=True,exist_ok=True)
        cfg.file.write_bytes(ConfigStore(RuntimePaths(Path(source_item['admitted']))).file.read_bytes())
        cfg.initialize()
        # Public parameters must match the approved current route; per-case expression seed is retained.
        cfgvalue=cfg.load()
        for section in ('model','character_selector'):
            a=public(cfgvalue[section]);b=public(native[section]);a.pop('_dialogue_profile',None);b.pop('_dialogue_profile',None)
            require(a==b,'cr21_public_model_changed')
        write_once(store.paths.home/'execution-contract.json',{'version':VERSION,'real':real,'batch':batch,
            'protocol_hash':sha(PROTOCOL),'pool_hash':fingerprint(source_item['pool'])})
        attachment=await base.admit(store,cfg,source_item['attachment']['persona_id'],case,source_item['role'])
        attachment['pool_hash']=fingerprint(source_item['pool'])
        write_once(store.paths.home/'artificial-cr2.json',attachment)
        engine=ChatEngine(store,cfg,NoDispatch())
        try:gathered=collect(engine,attachment['turn_id'],attachment)
        finally:close(engine)
        item={'key':key,'case_id':case['id'],'role_id':source_item['role'],'admitted':str(store.paths.home),
            'attachment':attachment,'pool':source_item['pool'],'shared_guard':gathered['source_guard'],
            'routing':route(gathered['shared']),'public_snapshots':{}}
        with store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
        item['public_snapshots']={k:turn[k] for k in FROZEN}
        write_once(root/'requests'/(key+'.json'),item);items[key]=item
        print(encoded({'prepared':key,'real_calls':0}),flush=True)
    positions=position_order(items)
    write_once(root/'prepared.json',{'version':VERSION,'batch':batch,'real':real,'runtime_root':str(runtime),
        'items':items,'positions':positions,'main_positions':128,'normal_generations':224,
        'smoke_scenes':['S01','S11'],'aggregate_call_cap':None,'paid_judge':False})
    write_once(root/'freeze.json',{'sources':sources(),'files':{p.relative_to(root).as_posix():sha(p)
        for p in root.rglob('*.json')}})
    return {'prepared':128,'real':real,'normal_generations':224,'real_calls':0}


def frozen(root):
    root=confined(root);freeze=read(root/'freeze.json')
    require(freeze['sources']==sources(),'cr21_execution_source_changed')
    for name,value in freeze['files'].items():require(sha(root/name)==value,'cr21_frozen_file_changed')
    p=read(root/'prepared.json');runtime=Path(p['runtime_root']).resolve()
    require(runtime.is_relative_to((HOME/'tmp/cr2-real').resolve()),'cr21_runtime_boundary')
    require(read(runtime/'owner.json')=={'version':VERSION,'batch':p['batch'],'artifact_root':str(root),'private':False},'cr21_owner_mismatch')
    return p


class FrozenCandidates:
    """Instance-only dependency; native parse/apply/audit is retained."""
    def __init__(self,original,item):self.original=original;self.item=item
    def __getattr__(self,name):return getattr(self.original,name)
    def local_pool(self,version,policy,members,scenario=()):
        accepted=json.loads(self.item['public_snapshots']['character_snapshot'])
        require(version==accepted and version['persona_id']==self.item['attachment']['persona_id'],'cr21_candidate_persona')
        expected_policy=json.loads(self.item['public_snapshots']['context_policy_snapshot'])['character_retrieval']
        require(policy==expected_policy,'cr21_candidate_policy')
        require(version['id']==self.item['pool']['version_id'],'cr21_candidate_version')
        require(fingerprint(self.item['pool'])==self.item['attachment']['pool_hash'],'cr21_candidate_changed')
        expected=[s['text'] for s in read(PARENT/'requests'/(self.item['key']+'.json'))['shared_preview']['sources'] if s['kind']=='current']
        require(members==expected,'cr21_candidate_input_changed')
        from persona_runtime.behavior import resolve_behavior
        require(list(scenario)==[s['act'] for s in resolve_behavior('\n\n'.join(expected))['signals']],'cr21_candidate_scenario')
        return deepcopy(self.item['pool'])


class Ledger:
    def __init__(self,root,real):self.root=Path(root);self.real=real;self.rows=[];self.seen=set();self.broken=False
    def reserve(self,pos,kind,model,messages):
        require(not self.broken,'cr21_ledger_broken')
        require(not any('terminal' not in row for row in self.rows),'cr21_unsettled_receipt')
        slot=pos+'/'+kind;old=[r for r in self.rows if r['reserve']['slot']==slot]
        require(len(old)<4 and not any(r['terminal']['status']=='completed' for r in old),'cr21_no_success_replay')
        request_hash=fingerprint(build_request(model,messages)[0])
        require(not old or all(r['reserve']['request_hash']==request_hash for r in old),'cr21_retry_changed')
        value={'id':f'{len(self.rows)+1:04d}','ordinal':len(self.rows)+1,'slot':slot,'kind':kind,
            'real':self.real,'simulation':not self.real,'model':public(model),'messages':deepcopy(messages),
            'input_hash':fingerprint(messages),'request_hash':request_hash,'started_epoch':time.time()}
        try:write_once(self.root/(value['id']+'.reserve.json'),value)
        except BaseException:self.broken=True;raise
        row={'reserve':value};self.rows.append(row);return row
    def transport(self,row,value):
        try:write_once(self.root/(row['reserve']['id']+'.transport.json'),value)
        except BaseException:self.broken=True;raise
        row['transport']=value
    def audit(self,state):
        for attempt in state.get('generation_retry',{}).get('attempts',[]):
            if attempt['status']=='running' or attempt['id'] in self.seen:continue
            row=next((r for r in self.rows if 'terminal' not in r and r['reserve']['request_hash']==attempt['request_hash']),None)
            if row is None:continue
            value={'status':attempt['status'],'runtime_attempt':attempt,'reservation_hash':fingerprint(row['reserve']),
                'real':self.real,'simulation':not self.real}
            try:write_once(self.root/(row['reserve']['id']+'.terminal.json'),value)
            except BaseException:self.broken=True;raise
            row['terminal']=value;self.seen.add(attempt['id'])
            if attempt['status']=='failed' and attempt['finished_epoch']-attempt['started_epoch']>600:
                self.broken=True;raise RuntimeErrorBase('cr21_slow_physical_failure')


class Transport:
    def __init__(self,root,prepared,pos,item,engine,ledger,collected,simulation_client=None):
        self.root,self.prepared,self.pos,self.item=root,prepared,pos,item
        self.engine,self.ledger,self.collected=engine,ledger,collected
        self.simulation_client=simulation_client
    async def complete(self,model,messages):
        with self.engine.store.connection() as db:
            turn=current(self.engine,self.item['attachment']['turn_id'],self.item['attachment'],db)
            require(source_guard(self.engine,db,turn,self.collected['archive_evidence'],self.collected['guard_details'])==self.item['shared_guard'],'cr21_sources_changed')
        kind='selection' if model.get('_character_selection') else 'joint' if model.get('_situation_observation') else 'main'
        if self.pos['arm']=='automatic':require(kind!='selection','cr21_hidden_selector')
        if kind=='main' and self.pos['arm']=='legacy':
            with self.engine.store.connection() as db:
                selection=db.execute('SELECT status FROM character_selection_attempts WHERE turn_id=?',(turn['id'],)).fetchone()
            require(selection and selection[0]=='completed','cr21_unexpected_lexical_fallback')
        real=self.prepared['real'];request=fingerprint(build_request(model,messages)[0])
        if real:
            require((self.root/'run.started.json').is_file(),'cr21_execution_not_started')
            owner=read(Path(self.prepared['runtime_root'])/'owner.json')
            require(owner['batch']==self.prepared['batch'] and owner['artifact_root']==str(self.root),'cr21_real_permit')
            vault=read(Path(self.prepared['runtime_root'])/'credentials.dpapi.json')
            section='model' if kind=='main' else 'character_selector';approved=vault[section]
            latest=ConfigStore(RuntimePaths(HOME)).load()[section]
            require(latest==approved,'cr21_approved_configuration_changed')
            expected=json.loads(turn['model_snapshot'] if kind=='main' else turn['selector_model_snapshot'])
            for name in ('base_url','name','max_input_chars'):require(model[name]==expected[name]==approved[name],'cr21_model_permit_mismatch')
            require(model['timeout']==(180 if kind=='main' else 240),'cr21_timeout_changed')
            require(model['max_tokens']==(8192 if kind=='main' else 2048 if kind=='joint' else 512),'cr21_output_budget_changed')
            allowed=deepcopy(expected)
            if kind=='main':allowed.update(_reply_mode=turn['reply_mode_snapshot'],_expression_snapshot=turn['expression_snapshot'])
            elif kind=='selection':allowed.update(_json_output=True,_character_selection=True,timeout=240)
            else:allowed.update(_json_output=True,_disposition_compile=True,_situation_observation=True,timeout=240,max_tokens=2048)
            require(fingerprint(build_request(allowed,messages)[0])==request,'cr21_public_wire_changed')
            actual=deepcopy(model)|{'encrypted_key':approved['encrypted_key']}
            require(fingerprint(build_request(actual,messages)[0])==request,'cr21_auth_changed_request')
            guard_cloud_dispatch(self.engine.store,turn['persona_id'],'main' if kind=='main' else 'selector',actual)
            client=ChatCompletionsClient()
        else:
            require(not model.get('encrypted_key'),'cr21_simulated_credentials')
            actual=model;client=self.simulation_client
            require(client is not None,'cr21_mock_not_configured')
        row=self.ledger.reserve(self.pos['id'],kind,model,messages);started=time.monotonic()
        try:value=await client.complete(actual,messages)
        except BaseException as error:
            code=str(error);code=code if re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code) else type(error).__name__
            self.ledger.transport(row,{'status':'error','error_code':code,'raw':None,'usage':getattr(error,'usage',None),
                'diagnostics':safe_diagnostics(getattr(error,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started,'real':real})
            raise
        self.ledger.transport(row,{'status':'returned','raw':value[0],'usage':value[1],
            'diagnostics':safe_diagnostics(getattr(value,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started,'real':real})
        return value


class Simulated:
    def __init__(self,case_id):self.case_id=case_id
    async def complete(self,model,messages):
        from tests.dialogue_context_responses import selection,joint,reply
        if model.get('_character_selection'):raw=encoded(selection(self.case_id))
        elif model.get('_situation_observation'):
            payload=json.loads(messages[1]['content']);raw=encoded(joint(self.case_id,payload['evidence']|{'digest':payload['evidence_hash']}))
        else:raw=reply(self.case_id)
        return ModelCompletion(raw,{'total_tokens':20},{'response_model':'mock-not-real','system_fingerprint':'mock','finish_reason':'stop'})


async def run_position(root,p,pos,ledger):
    item=p['items'][pos['key']];folder=Path(p['runtime_root'])/'positions'/pos['id']
    store=clone(Store(RuntimePaths(Path(item['admitted']))),folder)
    cfg=ConfigStore(store.paths);cfg.file.parent.mkdir(parents=True,exist_ok=True)
    cfg.file.write_bytes(ConfigStore(RuntimePaths(Path(item['admitted']))).file.read_bytes())
    attachment=item['attachment'];write_once(folder/'artificial-cr2.json',attachment)
    pipeline=DialoguePipeline(attachment,item['pool'],ledger.audit,expected_guard=item['shared_guard']) if pos['arm']=='automatic' else None
    engine=ChatEngine(store,cfg,NoDispatch(),dialogue_pipeline=pipeline,generation_audit_sink=ledger.audit)
    proxy=FrozenCandidates(engine.cognitive.characters,item)
    engine.cognitive.characters=proxy;engine.selector.characters=proxy;engine.selector.audit_sink=ledger.audit
    started=time.time();first=len(ledger.rows)
    try:
        c=collect(engine,attachment['turn_id'],attachment)
        require(c['source_guard']==item['shared_guard'],'cr21_initial_sources_changed')
        engine.client=Transport(root,p,pos,item,engine,ledger,c,Simulated(pos['case_id']) if not p['real'] else None)
        engine.selector.client=engine.client
        await engine._run(attachment['turn_id'])
        turn=engine.get(attachment['turn_id'])
        with store.connection() as db:
            manifests=[json.loads(r[0]) for r in db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(turn['id'],))]
            active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
        calls=ledger.rows[first:];main=[r for r in calls if r['reserve']['kind']=='main' and r.get('terminal',{}).get('status')=='completed']
        completed=turn['status']=='completed' and bool(main) and active==0 and all('terminal' in r for r in calls)
        value={**pos,'real':p['real'],'simulation':not p['real'],'status':'completed' if completed else 'failed',
            'error_code':turn.get('error_code'),'reply':main[-1]['transport']['raw'] if main else None,
            'messages':main[-1]['reserve']['messages'] if main else [],'manifest':manifests[-1] if manifests else None,
            'pipeline':pipeline.record if pipeline else None,'elapsed_seconds':time.time()-started,
            'calls':[r['reserve']['id'] for r in calls],'runtime':str(folder),'native_active':active}
        if main:require(fingerprint(value['messages'])==value['manifest']['input_snapshot_hash'],'cr21_main_manifest_mismatch')
        write_once(root/'results'/(pos['id']+'.json'),value)
        require(completed,'cr21_position_failed_'+str(turn.get('error_code')))
        return value
    finally:close(engine)


async def run(root,real):
    p=frozen(root);require(p['real'] is real,'cr21_execution_mode_mismatch')
    require(not (root/'run.started.json').exists(),'cr21_existing_batch_report_only')
    write_once(root/'run.started.json',{'real':real,'batch':p['batch'],'at':time.time()})
    ledger=Ledger(root/'receipts',real);completed=0
    try:
        for pos in p['positions']:
            require(sources()==read(root/'freeze.json')['sources'],'cr21_execution_source_changed')
            await run_position(root,p,pos,ledger);completed+=1
            atomic_json(root/'state.json',{'status':'running','real':real,'main_completed':completed,'physical_calls':len(ledger.rows)})
            print(encoded({'completed':completed,'of':128,'position':pos['id'],'physical_calls':len(ledger.rows),'real':real}),flush=True)
            if completed==16:
                require(len(ledger.rows)==28 or any(r['terminal']['status']=='failed' for r in ledger.rows),'cr21_smoke_call_structure')
                write_once(root/'smoke.json',{'passed_protocol_and_audit':True,'main_completed':16,'physical_calls':len(ledger.rows),'quality':'retained_not_retry_gate'})
        state={'status':'completed','real':real,'main_completed':completed,'physical_calls':len(ledger.rows)}
    except BaseException as error:
        code=str(error);code=code if re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code) else type(error).__name__
        state={'status':'interrupted' if isinstance(error,asyncio.CancelledError) else 'stopped','real':real,
            'main_completed':completed,'physical_calls':len(ledger.rows),'reason':code}
        atomic_json(root/'state.json',state);raise
    atomic_json(root/'state.json',state);return state


def verify(root):
    from persona_runtime.reply_batch import validate_reply
    p=frozen(root);state=read(root/'state.json');rows={}
    require(state['real'] is p['real'],'cr21_state_mode_mismatch')
    require(state['status']=='completed' and state['main_completed']==128,'cr21_real_validation_incomplete')
    reserves={f.name.split('.')[0] for f in (root/'receipts').glob('*.reserve.json')}
    for suffix in ('transport','terminal'):
        require({f.name.split('.')[0] for f in (root/'receipts').glob('*.'+suffix+'.json')}==reserves,'cr21_orphan_receipt')
    for reserve in (root/'receipts').glob('*.reserve.json'):
        r=read(reserve);terminal=read(reserve.with_name(reserve.name.replace('.reserve.','.terminal.')))
        transport=read(reserve.with_name(reserve.name.replace('.reserve.','.transport.')))
        require(r['real']==p['real']==terminal['real']==transport['real'],'cr21_real_simulation_mixed')
        require(fingerprint(r)==terminal['reservation_hash'],'cr21_receipt_binding')
        require(r['request_hash']==terminal['runtime_attempt']['request_hash'],'cr21_runtime_audit_binding')
        require(r['input_hash']==fingerprint(r['messages']) and r['request_hash']==fingerprint(build_request(r['model'],r['messages'])[0]),'cr21_wire_hash_mismatch')
        rows[r['id']]={'reserve':r,'terminal':terminal,'transport':transport}
    require(sorted(r['reserve']['ordinal'] for r in rows.values())==list(range(1,len(rows)+1)),'cr21_ordinal_gap')
    require(len(rows)==state['physical_calls'],'cr21_physical_count')
    require({f.stem for f in (root/'results').glob('*.json')}=={pos['id'] for pos in p['positions']},'cr21_result_inventory')
    for pos in p['positions']:
        result=read(root/'results'/(pos['id']+'.json'));item=p['items'][pos['key']]
        require(result['status']=='completed' and result['real']==p['real'] and result['simulation']==(not p['real']),'cr21_result_type')
        require(all(result.get(k)==v for k,v in pos.items()),'cr21_result_identity')
        physical=[r for r in rows.values() if r['reserve']['slot'].startswith(pos['id']+'/')]
        require(set(result['calls'])=={r['reserve']['id'] for r in physical} and len(result['calls'])==len(physical),'cr21_result_call_binding')
        successful=[r for r in physical if r['terminal']['status']=='completed']
        expected={'selection','main'} if pos['arm']=='legacy' else {'main'} if item['routing']['path']=='direct' else {'joint','main'}
        require({r['reserve']['kind'] for r in successful}==expected and len(successful)==len(expected),'cr21_logical_call_structure')
        for kind in expected:
            calls=[r for r in physical if r['reserve']['kind']==kind]
            require(1<=len(calls)<=4 and sum(r['terminal']['status']=='completed' for r in calls)==1,'cr21_retry_structure')
            require(len({r['reserve']['request_hash'] for r in calls})==1,'cr21_retry_changed')
        main=next(r for r in successful if r['reserve']['kind']=='main')
        expected_model=json.loads(item['public_snapshots']['model_snapshot'])
        require(result['manifest']['model_snapshot_hash']==fingerprint(public(expected_model)),'cr21_result_model_binding')
        expected_model.update(_reply_mode=item['public_snapshots']['reply_mode_snapshot'],_expression_snapshot=item['public_snapshots']['expression_snapshot'])
        require(fingerprint(build_request(expected_model,result['messages'])[0])==main['reserve']['request_hash'],'cr21_expected_main_wire')
        require(result['messages']==main['reserve']['messages'] and fingerprint(result['messages'])==result['manifest']['input_snapshot_hash'],'cr21_result_input_binding')
        require(result['reply']==main['transport']['raw'],'cr21_result_reply_changed')
        validate_reply(result['reply'],'natural_structured',item['public_snapshots']['expression_snapshot'])
        runtime=Path(result['runtime']).resolve();require(runtime.is_relative_to(Path(p['runtime_root']).resolve()),'cr21_result_runtime_boundary')
        with Store(RuntimePaths(runtime)).connection() as db:
            require(db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]==0,'cr21_native_active')
    return {'passed':True,'real':p['real'],'main_completed':128,'physical_calls':len(rows)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','simulate','execute','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'simulation-v1');parser.add_argument('--real',action='store_true')
    args=parser.parse_args();root=confined(args.root)
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp')
    os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    if args.action=='execute':
        require(args.real,'cr21_execute_requires_real_flag');result=asyncio.run(run(root,True))
    else:
        with offline(),secondary_offline():
            if args.action=='prepare':result=asyncio.run(prepare(root,args.real))
            elif args.action=='simulate':require(not args.real,'cr21_simulate_real_flag');result=asyncio.run(run(root,False))
            elif args.action=='verify':result=verify(root)
            else:
                from scripts.dialogue_context_real_analysis import report
                result=report(root)
    print(encoded(result))


if __name__=='__main__':main()
