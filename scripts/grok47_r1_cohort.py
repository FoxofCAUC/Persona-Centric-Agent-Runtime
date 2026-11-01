"""Fresh Grok-only R1 cohort, guarded by the completed compatibility gate.

Both arms are re-accepted through new isolated policies. Original DeepSeek
successes stay immutable and are never merged into this provider's statistics.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch
from uuid import uuid5,NAMESPACE_URL
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import (HOME,OUT,ARMS,FROZEN,read,sha,safe_model,confined,configure,new_policy,
    NoDispatch,close,clone,identity_guard,ChatEngine,ChatCompletionsClient,RuntimePaths,Store,ConfigStore,
    identifier,fingerprint,build_request,encoded,timestamp,load_turn_input,references,selector_messages)
from scripts.retry_expression_selection_r1 import ReceiptLedger,StopRun,write_once
from scripts.grok47_r1_gate import EVIDENCE,GATE,source_hashes
from scripts.verify_prompt_assembly_r0 import formal_hashes
from persona_runtime.config import atomic_json
from persona_runtime.reply_batch import validate_reply
from persona_runtime.character_selector_protocol import parse_output
from persona_runtime.reply_protocol import response_format


def gate_guard():
    if (EVIDENCE/'blocked.json').exists():raise StopRun('grok_provider_blocked')
    if not read(GATE/'result.json')['protocol_passed'] or not read(GATE/'content-review.json')['passed']:raise StopRun('compatibility_gate_not_passed')
    if formal_hashes()!=read(EVIDENCE/'start.json')['formal_before']:raise StopRun('formal_changed')


def sources():return {**source_hashes(),str(Path(__file__).relative_to(ROOT)).replace('\\','/'):sha(Path(__file__))}


async def prepare(root):
    confined(root,EVIDENCE)
    if root.exists():raise StopRun('cohort_exists')
    gate_guard();root.mkdir()
    frozen=read(OUT/'experiment/prepared.json');data=read(OUT/'experiment/fixture.json')
    approved=ConfigStore(RuntimePaths(HOME)).load();people=[v['persona_id'] for v in frozen['people'].values()]
    shutil.copyfile(OUT/'experiment/fixture.json',root/'fixture.json')
    items=[];cases={c['id']:c for c in data['cases']}
    for old in frozen['items']:
        item=deepcopy(old);key=item['key'];case=cases[item['case']]
        poolfile=root/'candidates'/(item['pair']+'.json');poolfile.parent.mkdir(exist_ok=True)
        if not poolfile.exists():shutil.copyfile(Path(item['candidate_file']),poolfile)
        if sha(poolfile)!=sha(Path(item['candidate_file'])):raise StopRun('candidate_copy_changed')
        item['candidate_file']=str(poolfile)
        paths=RuntimePaths(root/'runtime'/key)
        store=clone(Store(RuntimePaths(OUT/'experiment/cases'/item['pair']/'authority')),paths)
        cfg=configure(paths,approved,case);engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
        try:
            identity_guard(store,cfg,item['persona_id'],root,people)
            with store.connection() as db:sid=db.execute('SELECT id FROM sessions WHERE persona_id=? ORDER BY rowid DESC',(item['persona_id'],)).fetchone()[0]
            ids=iter(uuid5(NAMESPACE_URL,'pcar-r1/'+item['pair']+'/'+str(n)) for n in range(200))
            with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                ingress=new_policy(engine,item['persona_id'],ARMS[item['arm']]);receipts=[]
                for n,text in enumerate(case['members']):
                    payload={'persona_id':item['persona_id'],'session_id':sid,'text':text,'request_id':'r1-'+item['pair']+'-'+str(n)}
                    if n==0 and case['reference']:payload['reference']={'kind':'turn','id':'r1-reference'}
                    receipts.append(ingress.accept(payload))
                batch=ingress.get(receipts[0]['batch_id']);tid=ingress.action(batch['id'],'seal',batch['revision'],identifier(),item['persona_id'])['turn_id']
            with store.connection() as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                members=[m['text'] for m in load_turn_input(db,turn)];ref,_=references(db,turn)
            hashes={k:fingerprint(turn[k]) for k in FROZEN}
            for k in FROZEN:
                # The disabled observer freezes the selector model as well.
                # Its policy is still compared; this does not enable observation.
                if k not in ('model_snapshot','selector_model_snapshot','observation_model_snapshot') and hashes[k]!=old['snapshot_hashes'][k]:raise StopRun('non_provider_snapshot_changed:'+k)
            messages=selector_messages(read(poolfile),members,ref,4,approved['character_selector']['max_input_chars'],prompt_version=ARMS[item['arm']])
            if messages!=old['selector_messages'] or tid!=old['turn_id']:raise StopRun('source_messages_changed')
            item.update(runtime=str(paths.home),snapshot_hashes=hashes,original_snapshot_hashes=old['snapshot_hashes'])
            items.append(item)
        finally:await close(engine)
    result={'items':items,'people':frozen['people'],'fixture_hash':sha(root/'fixture.json'),'generation_limit':64,
        'provider':{k:safe_model(approved[k]) for k in ('model','character_selector')},'at':timestamp()}
    write_once(root/'prepared.json',result)
    write_once(root/'freeze.json',{'prepared_hash':sha(root/'prepared.json'),'fixture_hash':sha(root/'fixture.json'),
        'candidate_hashes':{i['candidate_file']:sha(Path(i['candidate_file'])) for i in items}})
    write_once(root/'execution-sources.json',sources());print(json.dumps({'prepared':len(items),'cloud_calls':0}),flush=True)


def check(root,item,engine):
    gate_guard()
    from scripts.expression_selection_r1 import verify
    prepared=read(root/'prepared.json');verify(root,prepared)
    if sources()!=read(root/'execution-sources.json'):raise StopRun('execution_sources_changed')
    identity_guard(engine.store,engine.config,item['persona_id'],root,[v['persona_id'] for v in prepared['people'].values()])
    with engine.store.connection() as db:
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
        if {k:fingerprint(turn[k]) for k in FROZEN}!=item['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')


class Client:
    def __init__(self,root,item,engine,ledger,real):self.root,self.item,self.engine,self.ledger,self.real=root,item,engine,ledger,real;self.client=ChatCompletionsClient()
    async def complete(self,model,messages):
        check(self.root,self.item,self.engine);kind='selector' if model.get('_character_selection') else 'main'
        if kind=='selector' and messages!=self.item['selector_messages']:raise StopRun('selector_input_changed')
        if model['name']!='grok-4.7' or model['base_url'].rstrip('/')!='https://api.uniapi.io/v1':raise StopRun('provider_changed')
        row=self.ledger.reserve(self.item['key']+'/'+kind,{'item':self.item['key'],'kind':kind,'real':self.real,
            'request':build_request(model,messages)[0],'model':safe_model(model),'input_hash':fingerprint(messages)})
        started=time.monotonic();value=None
        try:
            if self.real:value=await self.client.complete(model,messages)
            elif kind=='selector':value=(encoded({'schema_version':1,'lore_ids':[],'example_ids':self.item['candidate_available_examples'][:1]}),{'total_tokens':0})
            else:value=(encoded({'schema_version':3,'behavior_intent':'respond','messages':[{'content':'人工模拟回复。','content_kind':'chat','cancel_if_new_input':False}]}),{'total_tokens':0})
            if kind=='selector':parse_output(value[0],read(Path(self.item['candidate_file'])),4)
            else:validate_reply(value[0],model['_reply_mode'],model['_expression_snapshot'])
        except Exception as exc:
            self.ledger.finish(row,'failed',error=type(exc).__name__+':'+str(exc),seconds=time.monotonic()-started,
                output=value[0] if value else None,usage=value[1] if value else getattr(exc,'usage',None),diagnostics=getattr(value or exc,'diagnostics',None))
            raise StopRun('cohort_model_or_protocol_failed:'+self.item['key']+'/'+kind+':'+str(exc)) from exc
        self.ledger.finish(row,'completed',output=value[0],usage=value[1],diagnostics=getattr(value,'diagnostics',None),seconds=time.monotonic()-started)
        return value


async def run(root,real):
    gate_guard()
    if real and not read(EVIDENCE/'cohort-simulation/run-status.json')['complete']:raise StopRun('simulation_required')
    if (root/'receipts').exists():raise StopRun('cohort_no_automatic_resume')
    prepared=read(root/'prepared.json');ledger=ReceiptLedger(root/'receipts',limit=64)
    for item in prepared['items']:
        paths=RuntimePaths(Path(item['runtime']));engine=ChatEngine(Store(paths),ConfigStore(paths),NoDispatch());engine.store.character_committed=lambda *_:[]
        client=Client(root,item,engine,ledger,real);engine.client=engine.selector.client=engine.observer.client=client
        pool=read(Path(item['candidate_file']))
        def frozen_pool(version,policy,members,scenario=()):
            if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
            return deepcopy(pool)
        engine.cognitive.characters.local_pool=frozen_pool
        try:
            check(root,item,engine);await engine._run(item['turn_id']);check(root,item,engine)
            rows=ledger.rows();calls=[r for r in rows if r['item']==item['key']];turn=engine.get(item['turn_id'])
            if turn['status']!='completed' or len(calls)!=2 or any(r['status']!='completed' for r in calls):raise StopRun('cohort_turn_failed:'+item['key'])
            with engine.store.connection() as db:
                manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(item['turn_id'],))]
                selected=[dict(r) for r in db.execute('SELECT id,status,selected_json,error_code FROM character_selection_attempts WHERE turn_id=?',(item['turn_id'],))]
                attempts=[dict(r) for r in db.execute('SELECT id,status,error_code FROM attempts WHERE turn_id=?',(item['turn_id'],))]
            if not selected or selected[0]['status']!='completed':raise StopRun('selector_fallback_not_valid_sample')
            manifest=next(m for m in manifests if m['task_type']=='dialogue');actual=next(r['input_hash'] for r in calls if r['kind']=='main')
            if manifest['input_snapshot_hash']!=actual or manifest['prompt_assembly']['input_snapshot_hash']!=actual:raise StopRun('main_hash_mismatch')
            write_once(root/'results'/(item['key']+'.json'),{'item':item['key'],'turn':turn,'manifests':manifests,
                'attempts':attempts,'selection_attempts':selected,'calls':[r['ordinal'] for r in calls],'real':real})
            atomic_json(root/'ledger.json',{'limit':64,'calls':rows,'stop':None})
            print(json.dumps({'item':item['key'],'status':'completed','calls':len(rows)}),flush=True)
        finally:await close(engine)
    write_once(root/'run-status.json',{'real':real,'complete':True,'items':32,'calls':64,'failed_calls':0})


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('simulate','prepare','run'));args=p.parse_args()
    root=EVIDENCE/('cohort-simulation' if args.action=='simulate' else 'cohort')
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:
        if args.action in ('simulate','prepare'):asyncio.run(prepare(root))
        if args.action!='prepare':asyncio.run(run(root,args.action=='run'))
    except (StopRun,Exception) as exc:
        if not (EVIDENCE/'blocked.json').exists():write_once(EVIDENCE/'blocked.json',{'status':'blocked','reason':str(exc),'action':args.action,'continue_r1':False,'at':timestamp()})
        print(json.dumps({'blocked':str(exc)},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
