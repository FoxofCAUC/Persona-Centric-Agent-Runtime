"""R2.1 frozen comparison. Only the explicit run action dispatches to a provider."""
import argparse
import asyncio
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from unittest.mock import patch
from uuid import uuid5,NAMESPACE_URL
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.example_calibration_r2 import (HOME,OUT as R2,offline,configure as configure_offline,
    read,sha,confined,identity_guard,new_policy,NoDispatch,close,collect,reconcile,
    RuntimePaths,Store,ConfigStore,ChatEngine,identifier,encoded,fingerprint,timestamp,
    FROZEN,ARMS,load_turn_input,references,selector_messages,write_once,StopRun)
from scripts.grok47_r1_retry import RetryReceipts
from scripts.verify_prompt_assembly_r0 import formal_hashes
from scripts.report_expression_selection_r1 import body_without_character
from persona_runtime.chat import ChatCompletionsClient,ModelCompletion,ModelError
from persona_runtime.provider_compat import build_request
from persona_runtime.character_selector_protocol import parse_output
from persona_runtime.reply_batch import validate_reply
from persona_runtime.config import atomic_json

OUT=HOME/'exports/acceptance/M7.6.3-R2.1-frozen-real-comparison'
PARENT=R2/'batch-001'
VERSION='r2-real-v1'
LIMIT=256


def public(value):
    if isinstance(value,dict):return {k:public(v) for k,v in value.items() if k!='encrypted_key'}
    if isinstance(value,list):return [public(v) for v in value]
    return value


def public_snapshot(field,value):
    return public(json.loads(value or '{}')) if field.endswith('model_snapshot') else value


def sources():
    paths=set(read(PARENT/'freeze.json')['execution_sources'])
    paths.update(('scripts/r2_frozen_comparison.py','scripts/r2_frozen_report.py'))
    return {name:sha(ROOT/name) for name in sorted(paths)}


def protected(*,full=False):
    start=read(OUT/'start.json')
    if sha(R2/'closure.json')!=start['old_closure_sha256']:raise StopRun('R2_closure_changed')
    if full:
        for name,digest in start['old_evidence_hashes'].items():
            if sha(R2/name)!=digest:raise StopRun('R2_evidence_changed:'+name)
    for name,digest in read(PARENT/'freeze.json')['execution_sources'].items():
        if sha(ROOT/name)!=digest:raise StopRun('R2_execution_dependency_changed:'+name)
    if formal_hashes()!=start['formal_before']:raise StopRun('formal_files_changed')


def readonly_clone(source_home,paths):
    confined(paths.home,OUT)
    paths.initialize();target=Store(paths)
    source=Store(RuntimePaths(source_home)).path
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target.path)) as dst:src.backup(dst)
    target.initialize();return target


def configure(paths,approved,case,real):
    cfg=configure_offline(paths,approved,case)
    if real:
        value=cfg.load()
        for key in ('model','character_selector'):
            if not approved[key].get('encrypted_key'):raise StopRun('configured_credential_missing')
            value[key]['encrypted_key']=approved[key]['encrypted_key']
        atomic_json(cfg.file,value)
    return cfg


async def prepare(root,real):
    confined(root,OUT);protected(full=True)
    if root.exists():raise StopRun('preparation_exists_never_overwrite')
    old=read(PARENT/'prepared.json');order=old['future_order'];data=read(PARENT/'fixture.json')
    approved=ConfigStore(RuntimePaths(HOME)).load()
    for key in ('model','character_selector'):
        if public(approved[key])!=old['provider'][key]:raise StopRun('frozen_model_parameters_changed')
    root.mkdir();cases={c['id']:c for c in data['cases']};items=[]
    for name in ('fixture.json','labels.json','rubric.json'):shutil.copyfile(PARENT/name,root/name)
    for key in order:
        original=next(i for i in old['items'] if i['key']==key);case=cases[original['case']]
        # Explicit allow-list: never carry R2's scripted answers into real execution.
        item={k:deepcopy(original[k]) for k in ('key','pair','case','role','arm','persona_id','turn_id','candidate_hash','selector_messages','pool_guard')}
        poolfile=root/'candidates'/(item['pair']+'.json');poolfile.parent.mkdir(exist_ok=True)
        if not poolfile.exists():shutil.copyfile(Path(original['candidate_file']),poolfile)
        if sha(poolfile)!=sha(Path(original['candidate_file'])):raise StopRun('candidate_copy_changed')
        item['candidate_file']=str(poolfile)
        paths=RuntimePaths(root/'runtime'/key)
        store=readonly_clone(Path(original['runtime']).parent/'authority',paths)
        cfg=configure(paths,approved,case,real);engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
        try:
            identity_guard(store,cfg,item['persona_id'],root,[p['persona_id'] for p in old['people'].values()])
            with store.connection() as db:sid=db.execute('SELECT id FROM sessions WHERE persona_id=? ORDER BY rowid DESC',(item['persona_id'],)).fetchone()[0]
            ids=iter(uuid5(NAMESPACE_URL,'pcar-r2/'+item['pair']+'/'+str(n)) for n in range(300))
            with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                ingress=new_policy(engine,item['persona_id'],ARMS[item['arm']]);receipts=[]
                for n,text in enumerate(case['members']):
                    payload={'persona_id':item['persona_id'],'session_id':sid,'text':text,'request_id':'r2-'+item['pair']+'-'+str(n)}
                    if n==0 and case['reference']:payload['reference']={'kind':'turn','id':'r2-reference'}
                    receipts.append(ingress.accept(payload))
                b=ingress.get(receipts[0]['batch_id']);tid=ingress.action(b['id'],'seal',b['revision'],identifier(),item['persona_id'])['turn_id']
            with store.connection() as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());members=[m['text'] for m in load_turn_input(db,turn)];refs,_=references(db,turn)
            prior=read(PARENT/'accepted'/(key+'.json'))
            for field in FROZEN:
                if public_snapshot(field,turn[field])!=public_snapshot(field,prior[field]):raise StopRun('public_snapshot_changed:'+field)
            if tid!=original['turn_id']:raise StopRun('turn_identity_changed')
            if selector_messages(read(poolfile),members,refs,4,approved['character_selector']['max_input_chars'],prompt_version=ARMS[item['arm']])!=item['selector_messages']:raise StopRun('selector_payload_changed')
            item.update(runtime=str(paths.home),snapshot_hashes={f:fingerprint(turn[f]) for f in FROZEN},
                        public_snapshot_hashes={f:fingerprint(public_snapshot(f,turn[f])) for f in FROZEN})
            write_once(root/'accepted-public'/(key+'.json'),{f:public_snapshot(f,turn[f]) for f in FROZEN})
            items.append(item)
        finally:await close(engine)
    if len(items)!=32 or [i['key'] for i in items]!=order:raise StopRun('frozen_order_changed')
    result={'version':VERSION,'real':real,'items':items,'people':old['people'],'generation_limit':LIMIT,
            'provider':{k:public(approved[k]) for k in ('model','character_selector')},'created_at':timestamp()}
    write_once(root/'prepared.json',result)
    write_once(root/'freeze.json',{'files':{p.relative_to(root).as_posix():sha(p) for p in [root/n for n in ('fixture.json','labels.json','rubric.json','prepared.json')]+list((root/'candidates').glob('*.json'))+list((root/'accepted-public').glob('*.json'))},
        'execution_sources':sources(),'parent_closure_hash':sha(R2/'closure.json')})
    print(json.dumps({'prepared':32,'real_package':real,'real_calls':0}),flush=True)
    return result


def verify_freeze(root):
    seal=read(root/'freeze.json')
    for path,digest in seal['files'].items():
        if sha(root/path)!=digest:raise StopRun('frozen_file_changed:'+path)
    if seal['execution_sources']!=sources():raise StopRun('executed_sources_changed')
    return read(root/'prepared.json')


def check_item(root,item,engine):
    protected();prepared=verify_freeze(root)
    if (OUT/'blocked.json').exists():raise StopRun('batch_blocked')
    identity_guard(engine.store,engine.config,item['persona_id'],root,[p['persona_id'] for p in prepared['people'].values()])
    with engine.store.connection() as db:
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
    if {f:fingerprint(turn[f]) for f in FROZEN}!=item['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')


class Ledger(RetryReceipts):
    def __init__(self,root,limit=LIMIT):super().__init__(root,limit=limit)
    def check(self):
        rows=super().check()
        if any(r['status']=='failed' and r.get('seconds',0)>600 for r in rows):raise StopRun('long_failed_physical_call')
        if any(r['status']=='failed' and r['attempt_index']==3 for r in rows):raise StopRun('position_retries_exhausted')
        return rows


class FakeClient:
    async def complete(self,model,messages):
        if model.get('_character_selection'):
            from tests.example_calibration_responses import response_plan
            answer,_=response_plan(self.item['case'],self.item['arm'],self.pool)
            output=encoded(answer)
        else:
            output=encoded({'schema_version':3,'behavior_intent':'respond','messages':[{'content':'离线验证回复，不是质量样本。','content_kind':'chat','cancel_if_new_input':False}]})
        return ModelCompletion(output,{'total_tokens':0},{'response_model':'grok-4.7','system_fingerprint':'sim_backend','finish_reason':'stop'})


def main_manifest(engine,item):
    with engine.store.connection() as db:
        row=db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid DESC LIMIT 1",(item['turn_id'],)).fetchone()
    if not row:raise StopRun('main_manifest_missing')
    result=json.loads(row[0]);material=result.get('prompt_assembly',{}).get('material_selection',{})
    if material.get('applied_prompt')!=ARMS[item['arm']] or material.get('effective_mode')!='hybrid_llm':raise StopRun('selector_fallback_not_valid')
    return result


def compare_common(a,ma,b,mb):
    pa,pb=body_without_character(a['messages'],ma),body_without_character(b['messages'],mb)
    return pa[:2]==pb[:2] and {k:v for k,v in a.items() if k!='messages'}=={k:v for k,v in b.items() if k!='messages'}


class Capture:
    def __init__(self,root,item,engine,ledger,client,real):
        self.root,self.item,self.engine,self.ledger,self.client,self.real=root,item,engine,ledger,client,real
    async def complete(self,model,messages):
        check_item(self.root,self.item,self.engine)
        kind='selector' if model.get('_character_selection') else 'main'
        if kind=='selector' and messages!=self.item['selector_messages']:raise StopRun('selector_input_changed')
        payload=build_request(model,messages)[0]
        if kind=='main':
            manifest=main_manifest(self.engine,self.item)
            if manifest['input_snapshot_hash']!=fingerprint(messages):raise StopRun('final_input_hash_mismatch')
            other=self.item['pair']+'-'+('experiment' if self.item['arm']=='control' else 'control')
            previous=next((r for r in self.ledger.rows() if r['item']==other and r['kind']=='main' and r['status']=='completed'),None)
            if previous:
                other_value=read(self.root/'results'/(other+'.json'))
                other_manifest=next(m for m in other_value['manifests'] if m['task_type']=='dialogue')
                if not compare_common(payload,manifest,previous['request'],other_manifest):raise StopRun('non_example_request_difference')
        row=self.ledger.reserve_attempt(self.item['key']+'/'+kind,{'item':self.item['key'],'kind':kind,'real':self.real,
            'request':payload,'input_hash':fingerprint(messages)})
        atomic_json(self.root/'progress.json',{'phase':'calling','item':self.item['key'],'kind':kind,'attempt_index':row['attempt_index'],'calls_reserved':row['ordinal'],'limit':LIMIT,'at':timestamp()})
        started=time.monotonic();value=None
        try:
            value=await self.client.complete(model,messages)
            finish=getattr(value,'diagnostics',{}).get('finish_reason')
            if finish=='length':raise ModelError('provider_output_truncated')
            if finish=='content_filter':raise ModelError('provider_content_filtered')
            if kind=='selector':parse_output(value[0],read(Path(self.item['candidate_file'])),4)
            else:validate_reply(value[0],model['_reply_mode'],model['_expression_snapshot'])
        except BaseException as error:
            timed=isinstance(error,asyncio.CancelledError) and error.args==('generation_attempt_timeout',)
            interrupted=isinstance(error,asyncio.CancelledError) and not timed
            code=('selector_timeout' if kind=='selector' else 'provider_timeout') if timed else 'request_interrupted' if interrupted else str(error)
            self.ledger.finish(row,'interrupted' if interrupted else 'failed',error=code,seconds=time.monotonic()-started,
                output=value[0] if value is not None else None,usage=value[1] if value is not None else getattr(error,'usage',None),
                diagnostics=getattr(value if value is not None else error,'diagnostics',None))
            print(json.dumps({'item':self.item['key'],'kind':kind,'attempt':row['attempt_index'],'failed':code,'reserved':row['ordinal']},ensure_ascii=False),flush=True)
            if isinstance(error,Exception) and value is not None:error.usage=value[1];error.diagnostics=getattr(value,'diagnostics',None)
            raise
        self.ledger.finish(row,'completed',output=value[0],usage=value[1],diagnostics=getattr(value,'diagnostics',None),seconds=time.monotonic()-started)
        print(json.dumps({'item':self.item['key'],'kind':kind,'attempt':row['attempt_index'],'completed':True,'reserved':row['ordinal']},ensure_ascii=False),flush=True)
        return value


async def execute(root,real):
    protected(full=True);prepared=verify_freeze(root)
    if prepared['real']!=real:raise StopRun('package_mode_mismatch')
    if real:
        if not read(OUT/'simulation/verification.json')['passed'] or not read(OUT/'offline-gate.json')['passed']:raise StopRun('offline_gate_required')
    if (root/'receipts').exists():raise StopRun('existing_receipts_never_resume')
    ledger=Ledger(root/'receipts');completed=0
    for item in prepared['items']:
        paths=RuntimePaths(Path(item['runtime']));engine=ChatEngine(Store(paths),ConfigStore(paths),NoDispatch());engine.store.character_committed=lambda *_:[]
        pool=read(Path(item['candidate_file']));client=ChatCompletionsClient() if real else FakeClient()
        if not real:client.item=item;client.pool=pool
        capture=Capture(root,item,engine,ledger,client,real);engine.client=engine.selector.client=engine.observer.client=capture
        def frozen_pool(version,policy,members,scenario=()):
            if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
            return deepcopy(pool)
        engine.cognitive.characters.local_pool=frozen_pool
        try:
            check_item(root,item,engine)
            try:await engine._run(item['turn_id'])
            finally:
                value=collect(engine,item);value.pop('simulation',None);value['real']=real
                write_once(root/'results'/(item['key']+'.json'),value)
            check_item(root,item,engine);calls=[r for r in ledger.rows() if r['item']==item['key']]
            reconcile(value,calls)
            if value['turn']['status']!='completed':raise StopRun('turn_not_completed:'+item['key']+':'+str(value['turn'].get('error_code')))
            if any(sum(r['kind']==kind and r['status']=='completed' for r in calls)!=1 for kind in ('selector','main')):raise StopRun('successful_call_count')
            manifest=main_manifest(engine,item)
            main=next(r for r in calls if r['kind']=='main' and r['status']=='completed')
            if manifest['prompt_assembly']['input_snapshot_hash']!=main['input_hash']:raise StopRun('diagnostic_input_hash_mismatch')
            completed+=1
            atomic_json(root/'progress.json',{'phase':'completed_request','completed_requests':completed,'calls_reserved':len(ledger.rows()),'limit':LIMIT,'at':timestamp()})
            if completed==8:
                protected();write_once(root/'smoke.json',{'passed':True,'requests':8,'calls':len(ledger.rows()),'real':real,'replayed':False})
        finally:await close(engine)
    protected(full=True)
    write_once(root/'run-status.json',{'complete':True,'real':real,'requests':completed,'calls':len(ledger.rows()),'limit':LIMIT,'failed_calls':sum(r['status']=='failed' for r in ledger.rows()),'qq':0})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','simulate','run','report','verify'));args=parser.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'execution.lock');action=args.action
    try:
        if action in ('prepare','simulate','run') and (OUT/'blocked.json').exists():raise StopRun('batch_blocked')
        if action=='run':asyncio.run(execute(OUT/'real',True))
        else:
            with offline() as network:
                if action=='prepare':asyncio.run(prepare(OUT/'real',True))
                elif action=='simulate':asyncio.run(prepare(OUT/'simulation',False));asyncio.run(execute(OUT/'simulation',False))
                elif action=='report':
                    from scripts.r2_frozen_report import report
                    report(OUT/'real')
                else:
                    from scripts.r2_frozen_report import verify
                    verify(OUT/'real' if (OUT/'real/receipts').exists() else OUT/'simulation')
            atomic_json(OUT/('network-'+action+'.json'),network)
    except (Exception,StopRun,asyncio.CancelledError) as error:
        if action=='run' and not (OUT/'blocked.json').exists():write_once(OUT/'blocked.json',{'action':action,'reason':str(error),'at':timestamp(),'automatic_resume':False})
        print(json.dumps({'stopped':str(error),'action':action},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
