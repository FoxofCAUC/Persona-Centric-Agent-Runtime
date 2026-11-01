"""User-authorized supplement; runtime owns retries, collector never retries."""
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
    identifier,fingerprint,encoded,timestamp,load_turn_input,references,selector_messages)
from scripts.retry_expression_selection_r1 import ReceiptLedger,StopRun,write_once
from scripts.grok47_r1_gate import EVIDENCE
from scripts.verify_prompt_assembly_r0 import formal_hashes
from persona_runtime.provider_compat import build_request
from persona_runtime.config import atomic_json
from persona_runtime.chat import ModelError
from persona_runtime.reply_batch import validate_reply
from persona_runtime.character_selector_protocol import parse_output

SUPPLEMENT=EVIDENCE/'retry-three'
PARENT=EVIDENCE/'cohort'
KEYS=('R08-detail-experiment','R08-stance-control','R08-stance-experiment')


class RetryReceipts(ReceiptLedger):
    def check(self):
        rows=self.rows()
        if any(r['status'] in ('reserved','interrupted') for r in rows):raise StopRun('receipt_gap_or_interruption')
        return rows
    def reserve_attempt(self,slot,payload,prior=0):
        rows=self.check();previous=[r for r in rows if r['slot']==slot]
        index=prior+len(previous)
        if len(rows)>=self.limit or index>3:raise StopRun('retry_limit')
        if any(r['status']=='completed' for r in previous):raise StopRun('successful_slot_never_regenerate')
        if previous and previous[0]['request']!=payload['request']:raise StopRun('retry_request_changed')
        row={'ordinal':len(rows)+1,'key':slot+'/'+str(index),'slot':slot,'attempt_index':index,
             'status':'reserved','started_at':timestamp(),**payload}
        try:write_once(self.root/(str(row['ordinal']).zfill(3)+'.reserve.json'),row)
        except (OSError,ValueError,TypeError) as exc:raise StopRun('reservation_write_failed') from exc
        return row


def sources():
    paths=list((ROOT/'persona_runtime').rglob('*.py'))+[Path(__file__),ROOT/'scripts/expression_selection_r1.py',
        ROOT/'scripts/retry_expression_selection_r1.py',ROOT/'scripts/grok47_r1_gate.py']
    return {str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in paths}


def protected():
    auth=read(SUPPLEMENT/'authorization.json')
    if sha(EVIDENCE/'closure.json')!=auth['old_closure_sha256']:raise StopRun('old_closure_changed')
    if formal_hashes()!=read(EVIDENCE/'start.json')['formal_before']:raise StopRun('formal_changed')


async def prepare(root):
    confined(root,SUPPLEMENT)
    if root.exists():raise StopRun('supplement_exists')
    protected()
    old_closure=read(EVIDENCE/'closure.json')
    for path,value in old_closure['evidence_hashes'].items():
        if sha(EVIDENCE/path)!=value:raise StopRun('old_evidence_changed:'+path)
    original=read(PARENT/'prepared.json');items=[];data=read(PARENT/'fixture.json')
    cases={c['id']:c for c in data['cases']};approved=ConfigStore(RuntimePaths(HOME)).load()
    root.mkdir();shutil.copyfile(PARENT/'fixture.json',root/'fixture.json')
    people=[p['persona_id'] for p in original['people'].values()]
    for old in original['items']:
        if old['key'] not in KEYS:continue
        if (PARENT/'results'/(old['key']+'.json')).exists():raise StopRun('successful_item_not_eligible')
        item=deepcopy(old);case=cases[item['case']];paths=RuntimePaths(root/'runtime'/item['key'])
        poolfile=root/'candidates'/(item['pair']+'.json');poolfile.parent.mkdir(exist_ok=True)
        if not poolfile.exists():shutil.copyfile(Path(item['candidate_file']),poolfile)
        item['candidate_file']=str(poolfile)
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
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());members=[m['text'] for m in load_turn_input(db,turn)];ref,_=references(db,turn)
            if {k:fingerprint(turn[k]) for k in FROZEN}!=old['snapshot_hashes'] or tid!=old['turn_id']:raise StopRun('snapshot_changed')
            if selector_messages(read(poolfile),members,ref,4,approved['character_selector']['max_input_chars'],prompt_version=ARMS[item['arm']])!=item['selector_messages']:raise StopRun('selector_input_changed')
            item.update(runtime=str(paths.home),prior_selector_failures=1 if item['key']==KEYS[0] else 0)
            items.append(item)
        finally:await close(engine)
    if len(items)!=3:raise StopRun('remaining_items_mismatch')
    write_once(root/'prepared.json',{'items':items,'people':original['people'],'generation_limit':23,'at':timestamp()})
    write_once(root/'freeze.json',{'prepared_hash':sha(root/'prepared.json'),'fixture_hash':sha(root/'fixture.json'),
        'candidate_hashes':{i['candidate_file']:sha(Path(i['candidate_file'])) for i in items}})
    write_once(root/'execution-sources.json',sources())
    print(json.dumps({'prepared':3,'max_new_calls':23,'cloud_calls':0}),flush=True)


def check(root,item,engine):
    protected()
    if (root/'blocked.json').exists():raise StopRun('supplement_blocked')
    if sources()!=read(root/'execution-sources.json'):raise StopRun('execution_sources_changed')
    from scripts.expression_selection_r1 import verify
    prepared=read(root/'prepared.json');verify(root,prepared)
    identity_guard(engine.store,engine.config,item['persona_id'],root,[p['persona_id'] for p in prepared['people'].values()])
    with engine.store.connection() as db:
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
        if {k:fingerprint(turn[k]) for k in FROZEN}!=item['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')


class Capture:
    def __init__(self,root,item,engine,ledger,real):
        self.root,self.item,self.engine,self.ledger,self.real=root,item,engine,ledger,real
        self.client=ChatCompletionsClient()
    async def complete(self,model,messages):
        check(self.root,self.item,self.engine)
        kind='selector' if model.get('_character_selection') else 'main';slot=self.item['key']+'/'+kind
        if kind=='selector' and messages!=self.item['selector_messages']:raise StopRun('selector_input_changed')
        if model['name']!='grok-4.7' or model['base_url'].rstrip('/')!='https://api.uniapi.io/v1':raise StopRun('provider_changed')
        prior=self.item['prior_selector_failures'] if kind=='selector' else 0
        payload=build_request(model,messages)[0]
        if self.item['key']==KEYS[0] and kind=='selector' and payload!=read(PARENT/'receipts/059.reserve.json')['request']:
            raise StopRun('original_failed_request_changed')
        row=self.ledger.reserve_attempt(slot,{'item':self.item['key'],'kind':kind,'real':self.real,
            'request':payload,'input_hash':fingerprint(messages),'model':safe_model(model)},prior)
        started=time.monotonic();value=None
        try:
            if self.real:value=await self.client.complete(model,messages)
            elif self.item['key']==KEYS[0] and row['attempt_index']==prior:
                raise ModelError('provider_timeout' if kind=='selector' else 'provider_empty_response')
            elif kind=='selector':value=(encoded({'schema_version':1,'lore_ids':[],'example_ids':self.item['candidate_available_examples'][:1]}),{'total_tokens':0})
            else:value=(encoded({'schema_version':3,'behavior_intent':'respond','messages':[{'content':'人工模拟回复。','content_kind':'chat','cancel_if_new_input':False}]}),{'total_tokens':0})
            if kind=='selector':parse_output(value[0],read(Path(self.item['candidate_file'])),4)
            else:validate_reply(value[0],model['_reply_mode'],model['_expression_snapshot'])
        except BaseException as error:
            timeout_cancel=isinstance(error,asyncio.CancelledError) and error.args==('generation_attempt_timeout',)
            code='selector_timeout' if timeout_cancel and kind=='selector' else 'provider_timeout' if timeout_cancel else 'request_interrupted' if isinstance(error,asyncio.CancelledError) else str(error)
            self.ledger.finish(row,'interrupted' if isinstance(error,asyncio.CancelledError) and not timeout_cancel else 'failed',
                error=code,seconds=time.monotonic()-started,output=value[0] if value else None,
                usage=value[1] if value else getattr(error,'usage',None),diagnostics=getattr(value if value is not None else error,'diagnostics',None))
            print(json.dumps({'item':self.item['key'],'kind':kind,'attempt_index':row['attempt_index'],'failed':code}),flush=True)
            if isinstance(error,Exception) and value is not None:error.usage=value[1];error.diagnostics=getattr(value,'diagnostics',None)
            raise
        self.ledger.finish(row,'completed',seconds=time.monotonic()-started,output=value[0],usage=value[1],diagnostics=getattr(value,'diagnostics',None))
        return value


async def run(root,real):
    if real and not read(SUPPLEMENT/'simulation/run-status.json')['complete']:raise StopRun('simulation_required')
    if (root/'receipts').exists():raise StopRun('no_automatic_resume')
    ledger=RetryReceipts(root/'receipts',limit=23);prepared=read(root/'prepared.json')
    for item in prepared['items']:
        engine=ChatEngine(Store(RuntimePaths(Path(item['runtime']))),ConfigStore(RuntimePaths(Path(item['runtime']))),NoDispatch())
        engine.store.character_committed=lambda *_:[];engine.selector.retry_prior_failures=item['prior_selector_failures']
        client=Capture(root,item,engine,ledger,real);engine.client=engine.selector.client=engine.observer.client=client
        pool=read(Path(item['candidate_file']))
        def frozen_pool(version,policy,members,scenario=()):
            if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
            return deepcopy(pool)
        engine.cognitive.characters.local_pool=frozen_pool
        try:
            check(root,item,engine);await engine._run(item['turn_id']);check(root,item,engine)
            turn=engine.get(item['turn_id']);calls=[r for r in ledger.rows() if r['item']==item['key']]
            if turn['status']!='completed':raise StopRun('retry_turn_failed:'+item['key']+':'+str(turn.get('error_code')))
            if any(sum(r['kind']==kind and r['status']=='completed' for r in calls)!=1 for kind in ('selector','main')):raise StopRun('successful_call_count')
            with engine.store.connection() as db:
                manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(item['turn_id'],))]
                selections=[dict(r) for r in db.execute('SELECT id,status,error_code,selected_json,usage_json FROM character_selection_attempts WHERE turn_id=?',(item['turn_id'],))]
                mains=[dict(r) for r in db.execute('SELECT id,status,error_code,usage_json FROM attempts WHERE turn_id=?',(item['turn_id'],))]
            for kind,records in (('selector',selections),('main',mains)):
                if len(records)!=1 or records[0]['status']!='completed':raise StopRun('runtime_record_invalid')
                evidence=json.loads(records[0]['usage_json'])['generation_retry']['attempts'];captured=[r for r in calls if r['kind']==kind]
                if len(evidence)!=len(captured):raise StopRun('runtime_dispatch_count_mismatch')
                if [e['index'] for e in evidence]!=[r['attempt_index'] for r in captured]:raise StopRun('runtime_retry_index_mismatch')
            m=next(m for m in manifests if m['task_type']=='dialogue');actual=next(r['input_hash'] for r in calls if r['kind']=='main')
            if actual!=m['input_snapshot_hash'] or actual!=m['prompt_assembly']['input_snapshot_hash']:raise StopRun('main_hash_mismatch')
            write_once(root/'results'/(item['key']+'.json'),{'item':item['key'],'turn':turn,'manifests':manifests,
                'selection_attempts':selections,'attempts':mains,'calls':[r['ordinal'] for r in calls],'real':real})
            print(json.dumps({'item':item['key'],'completed':True,'physical_calls':len(ledger.rows())}),flush=True)
        finally:await close(engine)
    write_once(root/'run-status.json',{'complete':True,'real':real,'items':3,'calls':len(ledger.rows()),'failed_calls':sum(r['status']=='failed' for r in ledger.rows())})


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('simulate','prepare','run'));args=p.parse_args()
    root=SUPPLEMENT/('simulation' if args.action=='simulate' else 'real')
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:
        if args.action in ('simulate','prepare'):asyncio.run(prepare(root))
        if args.action!='prepare':asyncio.run(run(root,args.action=='run'))
    except (StopRun,Exception) as error:
        if not (root/'blocked.json').exists():write_once(root/'blocked.json',{'status':'blocked','reason':str(error),'at':timestamp()})
        print(json.dumps({'blocked':str(error)},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
