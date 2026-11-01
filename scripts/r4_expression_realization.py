"""Isolated R4-A expression experiment. Only the explicit run action has network access."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid5,NAMESPACE_URL
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import (HOME,FROZEN,read,sha,confined,NoDispatch,close,clone,identity_guard,
    RuntimePaths,Store,ConfigStore,ChatEngine,RuntimeService,CharacterService,PersonaSpec,append_version,
    identifier,timestamp,encoded,fingerprint,seed_turn,IngressService,StopRun)
from scripts.r3_selector_prerequisites import offline
from scripts.example_calibration_r2 import collect,reconcile
from scripts.grok47_r1_retry import RetryReceipts
from scripts.retry_expression_selection_r1 import write_once
from scripts.verify_prompt_assembly_r0 import formal_hashes
from persona_runtime.chat import ChatCompletionsClient,ModelCompletion,ModelError
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_voice import freeze,render
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.generation_retry import audited_complete as original_audited_complete
from tests.r4_expression_fixture import fixture,rubric

OUT=HOME/'exports/acceptance/M7.6.3-R4-A-expression-realization'
OLD=HOME/'exports/acceptance/M7.6.3-R3.1-provider240-continuation'
VERSION='r4-expression-realization-v1'
ARMS={'control':'natural-dialogue-v2','experiment':'natural-dialogue-v3'}
# User removed the cumulative R4 experiment cap after physical call 154.
# None is serialized as null; per-position retries and integrity stops remain.
LIMIT=None


def public(value):
    if isinstance(value,dict):return {k:public(v) for k,v in value.items() if k!='encrypted_key'}
    if isinstance(value,list):return [public(v) for v in value]
    return value


def sources(extras=()):
    paths=list((ROOT/'persona_runtime').rglob('*.py'))
    paths += [ROOT/n for n in ('scripts/r4_expression_realization.py','scripts/expression_selection_r1.py',
        'scripts/r3_selector_prerequisites.py','scripts/example_calibration_r2.py','scripts/grok47_r1_retry.py',
        'scripts/retry_expression_selection_r1.py','scripts/verify_prompt_assembly_r0.py',
        'tests/r4_expression_fixture.py','tests/m71_fixtures.py')]
    paths += [ROOT/name for name in extras]
    return {p.relative_to(ROOT).as_posix():sha(p) for p in sorted(set(paths))}


def protected(full=False):
    start=read(OUT/'start.json')
    if sha(OLD/'closure.json')!=start['old_closure_sha256']:raise StopRun('old_closure_changed')
    if full:
        for name,digest in start['old_evidence_hashes'].items():
            if sha(OLD/name)!=digest:raise StopRun('old_evidence_changed:'+name)
    if formal_hashes()!=start['formal_before']:raise StopRun('formal_changed')


def configure(paths,approved,real,case=None,version=None):
    cfg=ConfigStore(paths);cfg.initialize();value=cfg.load()
    for section in ('model','character_selector'):
        value[section]=deepcopy(approved[section]);value[section]['encrypted_key']=''
    if real:
        if not approved['model'].get('encrypted_key'):raise StopRun('configured_credential_missing')
        value['model']['encrypted_key']=approved['model']['encrypted_key']
    value['organization']['daily_enabled']=False
    if case:value['model']['_dialogue_profile']=freeze('r4-'+case['id'],case['members'],version=version)
    atomic_json(cfg.file,value);return cfg


def admission(engine,pid):
    ingress=IngressService(engine);current=ingress.current_policy(pid,'desktop')
    ingress.update_policy(pid,'desktop','natural_batch',current['id'],identifier(),
        reply_mode='natural_structured',context_mode='working_set',cross_session=False,
        character_retrieval_mode='lexical',disposition_mode='baseline',situation_observation_mode='disabled')
    return ingress


def public_turn(turn):
    return {f:public(json.loads(turn[f] or '{}')) if f.endswith('model_snapshot') else turn[f] for f in FROZEN}


def canonical_snapshot(value):
    value=deepcopy(value);value['model_snapshot']['_dialogue_profile'].pop('version')
    return value


def voice_removed(request,manifest,profile):
    """Remove only the exact known voice rendering, preserving persona facts and all other bytes."""
    result=deepcopy(request);system=result['messages'][0]['content']
    text=render(profile)[0];locations=[]
    for block in manifest['prompt_assembly']['blocks']:
        start=block['start_character'];end=start+block['characters'];segment=system[start:end]
        if block['name']=='persona_expression' and segment.endswith(text):locations.append((end-len(text),end))
        elif block['name']=='expression_realization' and segment==text:locations.append((start,end))
    if len(locations)!=1:raise StopRun('voice_block_not_exact_rendering')
    start,end=locations[0];result['messages'][0]['content']=system[:start]+system[end:]
    return result


async def prepare(root,real,*,data=None,rules=None,versions=None,extras=(),tool_version=VERSION,authorization=None):
    confined(root,OUT);protected(full=True)
    if root.exists():raise StopRun('preparation_exists_never_overwrite')
    approved=ConfigStore(RuntimePaths(HOME)).load();model=approved['model']
    if (model['name'],model['base_url'].rstrip('/'),model['max_tokens'],model['timeout'])!=('grok-4.7','https://e-flowcode.cc/v1',8192,180):raise StopRun('approved_model_contract_changed')
    root.mkdir();data=deepcopy(data if data is not None else fixture());versions=versions or ARMS
    if set(versions)!=set(ARMS):raise StopRun('two_arms_required')
    write_once(root/'fixture.json',data);write_once(root/'rubric.json',rules if rules is not None else rubric())
    seed_paths=RuntimePaths(root/'seed');seed=Store(seed_paths);seed.initialize();configure(seed_paths,approved,False)
    service=RuntimeService(seed);characters=CharacterService(seed);people={}
    for role,char in data['characters'].items():
        pid=service.create_persona(PersonaSpec.from_dict(data['spec']))['id'];service.use_persona(pid)
        with seed.connection(write=True) as db:append_version(db,pid,'relationship',{'description':'作者设定：双方是普通朋友，无共同经历记录。'},actor='test_author')
        version=characters.update(pid,char,characters.current(pid)['id'],identifier())
        people[role]={'persona_id':pid,'character_version':version['id']}
    items=[];comparisons=[];pair_index=0
    for case in data['cases']:
        for role,person in people.items():
            pid=person['persona_id'];pair=case['id']+'-'+role;pair_dir=root/'runtime'/pair
            base=clone(seed,RuntimePaths(pair_dir/'authority'));service=RuntimeService(base);service.use_persona(pid);sid=service.create_session()['id']
            if case['reference']:
                with base.connection(write=True) as db:seed_turn(db,pid,sid,'r4-reference',[case['reference']['text']],reply=case['reference']['reply'])
            for index,history in enumerate(case.get('history',[])):
                with base.connection(write=True) as db:seed_turn(db,pid,sid,'r4-history-'+str(index),[history['text']],reply=history['reply'])
            comparison=None;request_comparison=None
            arms=list(versions) if pair_index%2==0 else list(reversed(versions));pair_index+=1
            for arm in arms:
                paths=RuntimePaths(pair_dir/arm);store=clone(base,paths);cfg=configure(paths,approved,real,case,versions[arm])
                engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
                try:
                    identity_guard(store,cfg,pid,root,[p['persona_id'] for p in people.values()])
                    ids=iter(uuid5(NAMESPACE_URL,'pcar-r4/'+pair+'/'+str(i)) for i in range(300))
                    with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                        ingress=admission(engine,pid);receipts=[]
                        for n,text in enumerate(case['members']):
                            payload={'persona_id':pid,'session_id':sid,'text':text,'request_id':'r4-'+pair+'-'+str(n)}
                            if n==0 and case['reference']:payload['reference']={'kind':'turn','id':'r4-reference'}
                            receipts.append(ingress.accept(payload))
                        batch=ingress.get(receipts[0]['batch_id']);tid=ingress.action(batch['id'],'seal',batch['revision'],identifier(),pid)['turn_id']
                    with store.connection() as db:turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                    accepted=public_turn(turn);normalized=canonical_snapshot(accepted)
                    if comparison is not None and comparison!=normalized:raise StopRun('paired_snapshots_differ')
                    comparison=normalized
                    prepared=engine.cognitive.prepare(tid)
                    model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
                    request=build_request(model,prepared['messages'])[0]
                    canonical=voice_removed(request,prepared['manifest'],model['_dialogue_profile'])
                    if request_comparison is not None and request_comparison!=canonical:raise StopRun('non_voice_request_difference')
                    request_comparison=canonical
                    key=pair+'-'+arm;material=prepared['manifest']['prompt_assembly']['material_selection']
                    item={'key':key,'pair':pair,'case':case['id'],'role':role,'arm':arm,'persona_id':pid,'turn_id':tid,'runtime':str(paths.home),
                        'snapshot_hashes':{f:fingerprint(turn[f]) for f in FROZEN},'configuration_hash':sha(cfg.file),
                        'request_hash':fingerprint(request),'input_hash':fingerprint(prepared['messages']),'voice':model['_dialogue_profile'],
                        'selected_examples':material['included_example_ids'],'no_cloud_selector':True}
                    write_once(root/'accepted-public'/(key+'.json'),accepted)
                    write_once(root/'planned'/(key+'.json'),{'request':request,'manifest':prepared['manifest']})
                    items.append(item)
                finally:await close(engine)
            comparisons.append({'pair':pair,'non_voice_request_identical':True,'snapshots_only_voice_version_differs':True})
    write_once(root/'pair-controls.json',comparisons)
    write_once(root/'prepared.json',{'version':tool_version,'real':real,'items':items,'people':people,'generation_limit':LIMIT,'arms':versions,
        'provider':public(approved['model']),'created_at':timestamp(),'normal_requests':48,'cloud_selector_calls':0})
    files=[root/n for n in ('fixture.json','rubric.json','pair-controls.json','prepared.json')]
    if authorization is not None:
        write_once(root/'authorization.json',authorization);files.append(root/'authorization.json')
    files+=list((root/'planned').glob('*.json'))+list((root/'accepted-public').glob('*.json'))
    write_once(root/'freeze.json',{'files':{p.relative_to(root).as_posix():sha(p) for p in files},'execution_extras':list(extras),'execution_sources':sources(extras)})
    print(json.dumps({'prepared':len(items),'real_package':real,'real_calls':0}),flush=True)


def verify_freeze(root):
    seal=read(root/'freeze.json')
    for name,digest in seal['files'].items():
        if sha(root/name)!=digest:raise StopRun('frozen_file_changed:'+name)
    if seal['execution_sources']!=sources(seal.get('execution_extras',())):raise StopRun('execution_sources_changed')
    return read(root/'prepared.json')


def check_item(root,item,engine):
    protected();prepared=verify_freeze(root)
    if (OUT/'blocked.json').exists():raise StopRun('stage_blocked')
    identity_guard(engine.store,engine.config,item['persona_id'],root,[p['persona_id'] for p in prepared['people'].values()])
    if sha(engine.config.file)!=item['configuration_hash']:raise StopRun('isolated_configuration_changed')
    with engine.store.connection() as db:turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
    if {f:fingerprint(turn[f]) for f in FROZEN}!=item['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')


class Ledger(RetryReceipts):
    def __init__(self,root,limit=LIMIT):
        self.cumulative_limit=limit
        super().__init__(root,limit=float('inf') if limit is None else limit)
    def rows(self):
        # Numeric ordering continues to work after receipt 999. Earlier bounded
        # experiments keep their original collector and frozen dependencies.
        rows=[]
        try:
            paths=sorted(self.root.glob('*.reserve.json'),key=lambda p:int(p.name.split('.')[0]))
            for path in paths:
                row=read(path)
                if path.name!=str(row['ordinal']).zfill(3)+'.reserve.json':raise StopRun('receipt_sequence')
                terminal=self.root/(str(row['ordinal']).zfill(3)+'.result.json')
                if terminal.exists():
                    result=read(terminal)
                    if result['reservation_hash']!=fingerprint(row):raise StopRun('receipt_reservation_mismatch')
                    row={**row,**result}
                rows.append(row)
            if [r['ordinal'] for r in rows]!=list(range(1,len(rows)+1)):raise StopRun('receipt_sequence')
            if self.cumulative_limit is not None and len(rows)>self.cumulative_limit:raise StopRun('supplement_limit')
            if len(list(self.root.glob('*.result.json')))!=sum(r['status']!='reserved' for r in rows):raise StopRun('orphan_receipt')
            if len({r['key'] for r in rows})!=len(rows):raise StopRun('duplicate_receipt')
            return rows
        except (OSError,ValueError,KeyError,TypeError) as exc:raise StopRun('receipt_unreadable:'+type(exc).__name__) from exc
    def check(self):
        rows=super().check()
        if any(r['status']=='failed' and r.get('seconds',0)>600 for r in rows):raise StopRun('long_failed_physical_call')
        if any(r['status']=='failed' and r['attempt_index']==3 for r in rows):raise StopRun('position_retries_exhausted')
        return rows


class FakeClient:
    async def complete(self,model,messages):
        return ModelCompletion(encoded({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'离线数据流验证，不是质量样本。','content_kind':'chat','cancel_if_new_input':False}]}),
            {'total_tokens':0},{'response_model':'grok-4.7','system_fingerprint':'sim_backend','finish_reason':'stop'})


def main_manifest(engine,item):
    with engine.store.connection() as db:row=db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid DESC LIMIT 1",(item['turn_id'],)).fetchone()
    if not row:raise StopRun('main_manifest_missing')
    return json.loads(row[0])


def validate_request(item,payload,manifest):
    if fingerprint(payload)!=item['request_hash']:raise StopRun('planned_request_changed')
    assembly=manifest.get('prompt_assembly',{})
    if manifest['input_snapshot_hash']!=item['input_hash'] or assembly.get('input_snapshot_hash')!=item['input_hash']:raise StopRun('final_input_hash_mismatch')
    if assembly['effective']['expression']!=item['voice']['version']:raise StopRun('voice_renderer_mismatch')
    material=assembly['material_selection']
    if material['effective_mode']!='lexical' or material['selection_attempt_id'] or material['included_example_ids']!=item['selected_examples']:raise StopRun('selected_material_changed')


class Capture:
    def __init__(self,root,item,engine,ledger,client,real):
        self.root,self.item,self.engine,self.ledger,self.client,self.real=root,item,engine,ledger,client,real;self.ready=None
    def preflight(self,model,messages):
        started=time.monotonic();check_item(self.root,self.item,self.engine);self.ledger.check()
        self.ready=fingerprint(build_request(model,messages)[0]);self.preflight_seconds=time.monotonic()-started
    async def complete(self,model,messages):
        payload=build_request(model,messages)[0]
        if self.ready!=fingerprint(payload):raise StopRun('preflight_required')
        self.ready=None
        if model.get('_character_selection'):raise StopRun('cloud_selector_forbidden')
        if sha(self.engine.config.file)!=self.item['configuration_hash']:raise StopRun('configuration_changed_before_dispatch')
        validate_request(self.item,payload,main_manifest(self.engine,self.item))
        row=self.ledger.reserve_attempt(self.root.name+'/'+self.item['key']+'/main',{'item':self.item['key'],'batch':self.root.name,
            'kind':'main','real':self.real,'request':payload,'input_hash':fingerprint(messages),'provider_route':model['base_url'],
            'configured_timeout_seconds':model['timeout'],'preflight_seconds':self.preflight_seconds})
        atomic_json(self.root/'progress.json',{'phase':'calling','item':self.item['key'],'attempt_index':row['attempt_index'],'reserved':row['ordinal'],'limit':LIMIT,'at':timestamp()})
        started=time.monotonic();value=None
        try:
            value=await self.client.complete(model,messages)
            finish=getattr(value,'diagnostics',{}).get('finish_reason')
            if finish=='length':raise ModelError('provider_output_truncated')
            if finish=='content_filter':raise ModelError('provider_content_filtered')
            validate_reply(value[0],model['_reply_mode'],model['_expression_snapshot'])
        except BaseException as error:
            timed=isinstance(error,asyncio.CancelledError) and error.args==('generation_attempt_timeout',)
            interrupted=isinstance(error,asyncio.CancelledError) and not timed
            code='provider_timeout' if timed else 'request_interrupted' if interrupted else str(error)
            self.ledger.finish(row,'interrupted' if interrupted else 'failed',error=code,seconds=time.monotonic()-started,
                output=value[0] if value is not None else None,usage=value[1] if value is not None else getattr(error,'usage',None),
                diagnostics=getattr(value if value is not None else error,'diagnostics',None))
            if isinstance(error,Exception) and value is not None:error.usage=value[1];error.diagnostics=getattr(value,'diagnostics',None)
            print(json.dumps({'item':self.item['key'],'attempt':row['attempt_index'],'failed':code},ensure_ascii=False),flush=True)
            raise
        self.ledger.finish(row,'completed',output=value[0],usage=value[1],diagnostics=getattr(value,'diagnostics',None),seconds=time.monotonic()-started)
        print(json.dumps({'item':self.item['key'],'attempt':row['attempt_index'],'completed':True,'reserved':row['ordinal']}),flush=True)
        return value


async def guarded_complete(client,model,messages,**kwargs):
    prior=kwargs['before_attempt']
    def preflight(state):
        if not isinstance(client,Capture):raise StopRun('isolated_capture_required')
        client.preflight(model,messages);prior(state)
    return await original_audited_complete(client,model,messages,**(kwargs|{'before_attempt':preflight}))


def ledger_path(root,real):return OUT/'real-receipts' if real else root/'receipts'


async def execute(root,real,*,gate_path=None):
    protected(full=True);prepared=verify_freeze(root)
    if prepared['real']!=real:raise StopRun('package_mode_mismatch')
    if real:
        gate=read(gate_path or OUT/'offline-gate.json')
        if not gate['passed']:raise StopRun('offline_gate_required')
        if gate['execution_sources']!=read(root/'freeze.json')['execution_sources']:raise StopRun('offline_gate_source_mismatch')
    if (root/'run-started.json').exists():raise StopRun('existing_run_never_resume')
    ledger=Ledger(ledger_path(root,real));ledger.check()
    write_once(root/'run-started.json',{'real':real,'at':timestamp(),'automatic_resume':False})
    completed=0
    for item in prepared['items']:
        paths=RuntimePaths(Path(item['runtime']));engine=ChatEngine(Store(paths),ConfigStore(paths),NoDispatch());engine.store.character_committed=lambda *_:[]
        capture=Capture(root,item,engine,ledger,ChatCompletionsClient() if real else FakeClient(),real)
        engine.client=capture;engine.selector.client=engine.observer.client=NoDispatch()
        try:
            check_item(root,item,engine)
            try:
                with patch('persona_runtime.generation_retry.audited_complete',guarded_complete):await engine._run(item['turn_id'])
            finally:
                value=collect(engine,item);value.pop('simulation',None);value['real']=real
                write_once(root/'results'/(item['key']+'.json'),value)
            calls=[r for r in ledger.rows() if r['batch']==root.name and r['item']==item['key']];reconcile(value,calls)
            if value['turn']['status']!='completed':raise StopRun('turn_not_completed:'+item['key']+':'+str(value['turn'].get('error_code')))
            if sum(r['status']=='completed' for r in calls)!=1:raise StopRun('successful_call_count')
            main=next(r for r in calls if r['status']=='completed');validate_request(item,main['request'],main_manifest(engine,item))
            check_item(root,item,engine);completed+=1
            atomic_json(root/'progress.json',{'phase':'completed_request','completed_requests':completed,'reserved':len(ledger.rows()),'limit':LIMIT,'at':timestamp()})
            if completed==8:write_once(root/'smoke-gate.json',{'passed':True,'requests':8,'real':real,'replayed':False})
        finally:await close(engine)
    protected(full=True)
    write_once(root/'run-status.json',{'complete':True,'real':real,'requests':completed,'calls':len(ledger.rows()),'failed_calls':sum(r['status']=='failed' for r in ledger.rows()),'qq':0})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','run','report','verify'));args=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'execution.lock');action=args.action
    root=OUT/('simulation-batch-001' if action=='simulate' else 'real-batch-001')
    try:
        if action=='run':asyncio.run(execute(root,True))
        else:
            with offline() as network:
                if action=='prepare':asyncio.run(prepare(root,True))
                elif action=='simulate':asyncio.run(prepare(root,False));asyncio.run(execute(root,False))
                else:
                    from scripts.r4_expression_report import report,verify
                    (report if action=='report' else verify)(root if (root/'run-started.json').exists() else OUT/'simulation-batch-001')
            atomic_json(OUT/('network-'+action+'.json'),network)
    except (Exception,StopRun,asyncio.CancelledError) as error:
        if action=='run' and not (OUT/'blocked.json').exists():write_once(OUT/'blocked.json',{'reason':str(error),'at':timestamp(),'automatic_resume':False})
        print(json.dumps({'stopped':str(error),'action':action},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
