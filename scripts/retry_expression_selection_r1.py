"""Explicitly authorized R1 supplement; immutable original evidence, bounded retries.

Never used by production. Each generation slot has at most two dispatches, all
arms share 34 reserved slots. A missing receipt stops, never silently replays.
"""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid5, NAMESPACE_URL

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import (OUT,HOME,ARMS,FROZEN,StopRun,read,sha,
    safe_model,confined,configure,new_policy,NoDispatch,close,clone,identity_guard,
    verify,execution_sources,ChatEngine,ChatCompletionsClient,RuntimePaths,Store,
    ConfigStore,identifier,fingerprint,build_request,encoded,timestamp,load_turn_input,
    references,resolve_behavior,selector_messages)
from persona_runtime.reply_batch import validate_reply
from persona_runtime.reply_protocol import response_format
from scripts.verify_prompt_assembly_r0 import formal_hashes

ORIGINAL=OUT/'experiment'
SUPPLEMENT=OUT/'retry-20261101'
LIMIT=34


def write_once(path,value):
    """Exclusive, flushed receipt. Partial writes remain visible and block resume."""
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())


class ReceiptLedger:
    def __init__(self,root,limit=LIMIT):
        self.root=root;self.limit=limit;self.root.mkdir(parents=True,exist_ok=True)
    def rows(self):
        rows=[]
        try:
            for path in sorted(self.root.glob('*.reserve.json')):
                row=read(path);terminal=self.root/(str(row['ordinal']).zfill(3)+'.result.json')
                if terminal.exists():
                    result=read(terminal)
                    if result['reservation_hash']!=fingerprint(row):raise StopRun('receipt_reservation_mismatch')
                    row={**row,**result}
                rows.append(row)
            if [r['ordinal'] for r in rows]!=list(range(1,len(rows)+1)):raise StopRun('receipt_sequence')
            if len(rows)>self.limit:raise StopRun('supplement_limit')
            if len(list(self.root.glob('*.result.json')))!=sum(r['status']!='reserved' for r in rows):raise StopRun('orphan_receipt')
            if len({r['key'] for r in rows})!=len(rows):raise StopRun('duplicate_receipt')
            return rows
        except (OSError,ValueError,KeyError,TypeError) as exc:raise StopRun('receipt_unreadable:'+type(exc).__name__) from exc
    def check(self):
        rows=self.rows()
        if any(r['status']=='reserved' for r in rows):raise StopRun('receipt_gap_never_replay')
        consecutive=0
        for row in reversed(rows):
            if row['status']=='failed':consecutive+=1
            else:break
        if consecutive>=5 or any(r['status']=='failed' and r.get('seconds',0)>600 for r in rows):raise StopRun('failure_stop_rule')
        return rows
    def reserve(self,slot,payload):
        rows=self.check();previous=[r for r in rows if r['slot']==slot]
        if len(rows)>=self.limit or len(previous)>=2:raise StopRun('retry_limit')
        if any(r['status']=='completed' for r in previous):raise StopRun('successful_slot_never_regenerate')
        if previous and previous[0]['request']!=payload['request']:raise StopRun('retry_request_changed')
        row={'ordinal':len(rows)+1,'key':slot+'/'+str(len(previous)+1),'slot':slot,
             'status':'reserved','started_at':timestamp(),**payload}
        try:write_once(self.root/(str(row['ordinal']).zfill(3)+'.reserve.json'),row)
        except (OSError,ValueError,TypeError) as exc:raise StopRun('reservation_write_failed') from exc
        return row
    def finish(self,row,status,**values):
        try:write_once(self.root/(str(row['ordinal']).zfill(3)+'.result.json'),
                       {'reservation_hash':fingerprint(row),'status':status,**values})
        except (OSError,ValueError,TypeError) as exc:raise StopRun('result_write_failed') from exc


def sources():
    return {**execution_sources(),str(Path(__file__).relative_to(ROOT)):sha(Path(__file__))}


def check_original(seal):
    for path,digest in seal.items():
        if sha(Path(path))!=digest:raise StopRun('original_evidence_changed:'+path)


def lenient_labels(fixture):
    # Wider conversational functions, never permission to transplant sample facts.
    extra={'R01':['play'],'R02':['opinion','choice'],'R03':[],
           'R04':['choice'],'R05':[],'R06':[],'R07':['opinion'],
           'R08':['small_win','opinion']}
    return {'version':'r1-sensitivity-v1','registered_before_supplement_calls':True,
        'retrospective_to_original':True,
        'rule':'除原允许集合外，允许与当前交流意图相容的反应功能；不因情绪正向或话题词相似一律放行。不得搬用样例事件，事实、引用、最终更正、精确格式标准不变。',
        'reasons':{'R01':'可用无害玩笑回应小收获，但不得将闹钟事件当事实。',
            'R02':'可表达对反复修改的立场或对取舍的态度；small_win与当前挫败不适配。',
            'R03':'仍限玩笑；small_win、opinion没有恰当的成功或安排情境。',
            'R04':'允许表达安排取舍，不能把日常意见推成用户经历。',
            'R07':'允许一般取舍立场，但必须服从后续改口。',
            'R08':'允许肯定制作成果或表达留存取舍；不得把用户未说的共同事件补为事实。'},
        'allowed':{c['id']:sorted(set(c['allowed_examples'])|set(extra.get(c['id'],[]))) for c in fixture['cases'] if c['real']}}


async def prepare(root):
    confined(root,SUPPLEMENT)
    if root.exists():raise StopRun('supplement_preparation_exists')
    frozen=read(ORIGINAL/'prepared.json');verify(ORIGINAL,frozen)
    closure=read(OUT/'closure.json')
    previous=read(OUT/'final-source-hashes.json')
    mismatches=[p for p,digest in previous.items() if sha(ROOT/p)!=digest]
    if mismatches:raise StopRun('R1_source_baseline_changed:'+','.join(mismatches))
    root.mkdir(parents=True)
    originals=[OUT/'closure.json',OUT/'final-source-hashes.json',ORIGINAL/'ledger.json',ORIGINAL/'stop.json',ORIGINAL/'stop-reconciliation.json',ORIGINAL/'prepared.json',ORIGINAL/'fixture.json',ORIGINAL/'freeze.json']
    originals+=list((ORIGINAL/'results').glob('*.json'))
    originals+=[Path(p) for p in read(ORIGINAL/'freeze.json')['candidate_hashes']]
    seal={str(p):sha(p) for p in originals}
    fixture=read(ORIGINAL/'fixture.json');write_once(root/'lenient-labels.json',lenient_labels(fixture))
    write_once(root/'authorization.json',{'user_instruction':'放宽口径，重试该阶段任务；同时增加宽松适用性分析',
        'new_calls_limit':LIMIT,'attempts_per_generation_slot':2,'expected_minimum_calls':17,
        'original_recorded_calls':56,'original_unresolved_local_attempts':2,
        'cumulative_conservative_limit':58+LIMIT,'original_64_limit_retained_as_historical':True,
        'stop_rules':['five_consecutive_actual_failures','failed_over_600_seconds','integrity_or_isolation_error'],
        'reuse_successful_main_replies':19,'reuse_successful_selectors':28,
        'no_quality_replacement':True,'registered_at':timestamp()})
    write_once(root/'original-seal.json',seal);write_once(root/'formal-before.json',formal_hashes())
    old_calls=read(ORIGINAL/'ledger.json')['calls'];items=[]
    people=[p['persona_id'] for p in frozen['people'].values()]
    cases={c['id']:c for c in fixture['cases']}
    for original in frozen['items']:
        item=deepcopy(original);key=item['key']
        result_path=ORIGINAL/'results'/(key+'.json')
        if result_path.exists():
            prior=read(result_path)
            if prior['turn']['status']=='completed':continue
            call=next(r for r in old_calls if r['item']==key and r['kind']=='main')
            if call['status']!='failed' or call.get('error')!='ModelError:provider_empty_response':raise StopRun('unexpected_original_failure')
            item.update(operation='replay_main',original_call_ordinal=call['ordinal'])
            items.append(item);continue
        if item['case']!='R08':raise StopRun('unexpected_missing_case')
        # Re-accept only the four incomplete R08 items in a new isolated copy.
        # The authority contains reference/history but no original accepted request.
        base=Store(RuntimePaths(ORIGINAL/'cases'/item['pair']/'authority'))
        paths=RuntimePaths(root/'runtime'/key);store=clone(base,paths)
        approved=ConfigStore(RuntimePaths(Path(original['runtime']))).load()
        cfg=configure(paths,approved,cases[item['case']]);engine=ChatEngine(store,cfg,NoDispatch())
        engine.store.character_committed=lambda *_:[]
        try:
            identity_guard(store,cfg,item['persona_id'],root,people)
            with store.connection() as db:sid=db.execute('SELECT id FROM sessions WHERE persona_id=? ORDER BY rowid DESC',(item['persona_id'],)).fetchone()[0]
            ids=iter(uuid5(NAMESPACE_URL,'pcar-r1/'+item['pair']+'/'+str(i)) for i in range(200))
            case=cases[item['case']]
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
            if hashes!=original['snapshot_hashes'] or tid!=item['turn_id']:raise StopRun('reaccepted_snapshot_changed')
            messages=selector_messages(read(Path(item['candidate_file'])),members,ref,4,approved['character_selector']['max_input_chars'],prompt_version=ARMS[item['arm']])
            if messages!=item['selector_messages']:raise StopRun('reaccepted_selector_input_changed')
            item.update(operation='new_request',runtime=str(paths.home))
            items.append(item)
        finally:await close(engine)
    if len(items)!=13:raise StopRun('unexpected_supplement_slots')
    write_once(root/'prepared.json',{'items':items,'people':frozen['people'],'original_closure_status':closure['status'],
        'original_seal_hash':sha(root/'original-seal.json'),'lenient_labels_hash':sha(root/'lenient-labels.json')})
    print(json.dumps({'prepared':len(items),'cloud_calls':0,'root':str(root)}),flush=True)


class RetryClient:
    def __init__(self,root,item,ledger,guard,real):
        self.root,self.item,self.ledger,self.guard,self.real=root,item,ledger,guard,real
        self.client=ChatCompletionsClient()
    async def complete(self,model,messages):
        kind='selector' if model.get('_json_output') else 'main';slot=self.item['key']+'/'+kind
        payload,_=build_request(model,messages)
        if kind=='selector' and messages!=self.item['selector_messages']:raise StopRun('selector_input_changed')
        for attempt in range(2):
            self.guard();row=self.ledger.reserve(slot,{'kind':kind,'item':self.item['key'],
                'real':self.real,'request':payload,'input_hash':fingerprint(messages),'model':safe_model(model)})
            started=time.monotonic();result=None
            try:
                if self.real:result=await self.client.complete(model,messages)
                elif kind=='selector':result=(encoded({'schema_version':1,'lore_ids':[],'example_ids':self.item['candidate_available_examples'][:1]}),{'total_tokens':0})
                else:
                    version=response_format(model['_reply_mode'],model['_expression_snapshot'])['json_schema']['schema']['properties']['schema_version']['enum'][0]
                    result=(encoded({'schema_version':version,'behavior_intent':'respond','messages':[{'content':'人工模拟回复。','content_kind':'chat','cancel_if_new_input':False}]}),{'total_tokens':0})
                if kind=='main':validate_reply(result[0],model['_reply_mode'],model['_expression_snapshot'])
            except Exception as exc:
                self.ledger.finish(row,'failed',seconds=time.monotonic()-started,error=type(exc).__name__+':'+str(exc),
                    output=result[0] if result else None,usage=result[1] if result else getattr(exc,'usage',None),diagnostics=getattr(result or exc,'diagnostics',None))
                self.ledger.check()
                print(json.dumps({'item':self.item['key'],'kind':kind,'attempt':attempt+1,'failed':str(exc)}),flush=True)
                if attempt==1:raise
                continue
            self.ledger.finish(row,'completed',seconds=time.monotonic()-started,output=result[0],usage=result[1],diagnostics=getattr(result,'diagnostics',None))
            return result


def guard_for(root,item,engine=None):
    prepared=read(root/'prepared.json');seal=read(root/'original-seal.json')
    def guard():
        check_original(seal)
        if sha(root/'original-seal.json')!=prepared['original_seal_hash'] or sha(root/'lenient-labels.json')!=prepared['lenient_labels_hash']:raise StopRun('supplement_freeze_changed')
        if formal_hashes()!=read(root/'formal-before.json'):raise StopRun('protected_files_changed')
        if (root/'execution-sources.json').exists() and sources()!=read(root/'execution-sources.json'):raise StopRun('supplement_source_changed')
        paths=RuntimePaths(Path(item['runtime']));store=engine.store if engine else Store(paths)
        cfg=engine.config if engine else ConfigStore(paths)
        identity_guard(store,cfg,item['persona_id'],root if engine else ORIGINAL,[p['persona_id'] for p in prepared['people'].values()])
        with store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
            if {k:fingerprint(turn[k]) for k in FROZEN}!=item['snapshot_hashes']:raise StopRun('snapshot_changed')
    return guard


async def run(root,real):
    confined(root,SUPPLEMENT)
    if real and root!=SUPPLEMENT/'real':raise StopRun('real_supplement_root_required')
    if real and not read(SUPPLEMENT/'simulation/run-status.json')['complete']:raise StopRun('simulation_required')
    if (root/'stop.json').exists():raise StopRun('supplement_stopped')
    prepared=read(root/'prepared.json');ledger=ReceiptLedger(root/'receipts');ledger.check()
    if not (root/'execution-sources.json').exists():write_once(root/'execution-sources.json',sources())
    for item in prepared['items']:
        destination=root/'results'/(item['key']+'.json')
        if destination.exists():continue
        if any(r['item']==item['key'] for r in ledger.rows()):raise StopRun('unreconciled_supplement_item')
        if item['operation']=='replay_main':
            guard=guard_for(root,item);guard()
            original=read(ORIGINAL/'results'/(item['key']+'.json'))
            call=next(c for c in read(ORIGINAL/'ledger.json')['calls'] if c['ordinal']==item['original_call_ordinal'])
            with Store(RuntimePaths(Path(item['runtime']))).connection() as db:
                snap=json.loads(db.execute('SELECT model_snapshot FROM turns WHERE id=?',(item['turn_id'],)).fetchone()[0])
            model={**call['model'],'encrypted_key':snap['encrypted_key']};messages=call['request']['messages']
            if build_request(model,messages)[0]!=call['request']:raise StopRun('main_replay_not_identical')
            client=RetryClient(root,item,ledger,guard,real);result=deepcopy(original)
            result['provenance']={'kind':'saved_request_supplement','original_result_unchanged':True,'original_call':call['ordinal'],
                'original_failed_turn_id':item['turn_id'],'not_a_new_historical_manifest':True}
            try:
                output=await client.complete(model,messages);batch=validate_reply(output[0],model['_reply_mode'],model['_expression_snapshot'])
                result['turn']={**original['turn'],'status':'completed','error_code':None,'reply':batch.text}
            except Exception as exc:result['supplement_error']=type(exc).__name__+':'+str(exc)
            result['calls']=[r['ordinal'] for r in ledger.rows() if r['item']==item['key']]
            result['real']=real;write_once(destination,result)
        else:
            paths=RuntimePaths(Path(item['runtime']));store=Store(paths);cfg=ConfigStore(paths)
            engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
            client=RetryClient(root,item,ledger,guard_for(root,item,engine),real)
            engine.client=engine.selector.client=engine.observer.client=client
            pool=read(Path(item['candidate_file']))
            def frozen_pool(version,policy,members,scenario=()):
                if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
                return deepcopy(pool)
            engine.cognitive.characters.local_pool=frozen_pool
            try:
                client.guard();await engine._run(item['turn_id']);client.guard();ledger.check()
                turn=engine.get(item['turn_id'])
                with store.connection() as db:
                    manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(item['turn_id'],))]
                    selected=[dict(r) for r in db.execute('SELECT id,status,selected_json,error_code FROM character_selection_attempts WHERE turn_id=?',(item['turn_id'],))]
                if not selected or selected[0]['status']!='completed':raise StopRun('supplement_selector_not_valid')
                main=next(m for m in manifests if m['task_type']=='dialogue')
                calls=[r for r in ledger.rows() if r['item']==item['key']]
                actual=next(r['input_hash'] for r in calls if r['kind']=='main')
                if main['input_snapshot_hash']!=actual or main['prompt_assembly']['input_snapshot_hash']!=actual:raise StopRun('manifest_hash_mismatch')
                result={'item':item['key'],'turn':turn,'manifests':manifests,'selection_attempts':selected,
                    'calls':[r['ordinal'] for r in calls],'real':real,
                    'provenance':{'kind':'new_isolated_request','same_snapshot_hashes':True,'same_candidates':True}}
                write_once(destination,result)
            finally:await close(engine)
        print(json.dumps({'item':item['key'],'status':result['turn']['status'],'supplement_calls':len(ledger.rows())}),flush=True)
        ledger.check()
    values=[read(p) for p in (root/'results').glob('*.json')]
    write_once(root/'run-status.json',{'real':real,'items':len(values),'complete':len(values)==13 and all(r['turn']['status']=='completed' for r in values),
        'calls':len(ledger.rows()),'failed_calls':sum(r['status']=='failed' for r in ledger.rows()),'formal_unchanged':formal_hashes()==read(root/'formal-before.json')})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','simulate','run'));args=parser.parse_args()
    root=SUPPLEMENT/('simulation' if args.action=='simulate' else 'real')
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:
        if args.action in ('prepare','simulate') and not (root/'prepared.json').exists():asyncio.run(prepare(root))
        if args.action!='prepare':asyncio.run(run(root,args.action=='run'))
    except StopRun as exc:
        if root.exists() and not (root/'stop.json').exists():write_once(root/'stop.json',{'reason':str(exc),'at':timestamp()})
        print(json.dumps({'stopped':str(exc)}),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
