"""R1 isolated two-arm development run. No server, retries, QQ or paid judge.

prepare freezes new policies BEFORE ingress acceptance, candidates and labels.
simulate uses a separate subtree and fake completions. run consumes a global
64-slot durable ledger. An interrupted/pending call is never replayed.
"""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch
from uuid import UUID, uuid5, NAMESPACE_URL

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from persona_runtime.archive import fingerprint
from persona_runtime.behavior import resolve_behavior
from persona_runtime.character import CharacterService
from persona_runtime.character_embedding import CharacterEmbeddingProvider
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_policy import choose, checked
from persona_runtime.character_selector_protocol import messages as selector_messages, parse_output
from persona_runtime.chat import ChatEngine, ChatCompletionsClient, ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.dialogue_voice import freeze
from persona_runtime.ingress import IngressService, encoded
from persona_runtime.models import PersonaSpec, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import append_version
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input
from persona_runtime.references import prepare as references
from tests.expression_selection_fixture import fixture
from tests.m71_fixtures import seed_turn

HOME=Path('D:/Persona-Centric Agent Runtime')
OUT=HOME/'exports/acceptance/M7.6.3-R1-expression-selection'
ARMS={'control':'character-selector-v2','experiment':'character-selector-v3'}
FROZEN=('profile_snapshot','character_snapshot','context_policy_snapshot','model_snapshot',
        'selector_model_snapshot','observation_model_snapshot','expression_snapshot','reply_mode_snapshot')


class StopRun(BaseException):
    """Integrity/stop rules must bypass the runtime's normal provider fallback."""


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def safe_model(model):return {k:v for k,v in model.items() if k not in ('encrypted_key','_request_deadline')}


def confined(path,root):
    from persona_runtime.private_personas import safe
    value=safe(path)
    if not value.is_relative_to(root.resolve()) or value==root.resolve():raise StopRun('isolation_path_invalid')
    return value


def configure(paths,approved,case=None):
    cfg=ConfigStore(paths);cfg.initialize();value=cfg.load()
    for key in ('model','character_selector'):value[key]=deepcopy(approved[key])
    value['organization']['daily_enabled']=False
    if case:value['model']['_dialogue_profile']=freeze('r1-'+case['id'],case['members'])
    atomic_json(cfg.file,value);return cfg


def new_policy(engine,pid,version):
    """Internal experiment-only policy version. No accepted snapshot edits."""
    ingress=IngressService(engine);current=ingress.current_policy(pid,'desktop')
    current=ingress.update_policy(pid,'desktop','natural_batch',current['id'],identifier(),
        reply_mode='natural_structured',context_mode='working_set',cross_session=False,
        character_retrieval_mode='hybrid_llm',disposition_mode='baseline',situation_observation_mode='disabled')
    with engine.store.connection(write=True) as db:
        row=dict(db.execute('SELECT * FROM interaction_policies WHERE id=?',(current['id'],)).fetchone())
        data=json.loads(row['data_json']);data['context']['character_retrieval']['selector_contract']['prompt']=version
        checked(data['context']['character_retrieval'])
        row.update(id=identifier(),revision=row['revision']+1,created_at=timestamp())
        if data.get('expression'):data['expression']['policy_id']=row['id']
        row['data_json']=encoded(data)
        db.execute('INSERT INTO interaction_policies ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
        db.execute('UPDATE interaction_heads SET version_id=? WHERE persona_id=? AND channel=?',(row['id'],pid,'desktop'))
    return ingress


class NoDispatch:
    async def complete(self,*_):raise StopRun('preparation_dispatch_forbidden')


async def close(engine):
    await engine.wait();await engine.observer.drain()
    engine.cognitive.shadow.close();engine.cognitive.characters.close()


def clone(store,paths):
    paths.initialize();target=Store(paths)
    with store.connection() as db,closing(sqlite3.connect(target.path)) as dest:db.backup(dest)
    target.initialize();return target


def identity_guard(store,cfg,pid,root,people):
    confined(store.paths.home,root)
    if pid not in people:raise StopRun('artificial_identity_required')
    AssistAdmission(store.paths.home,cfg.load()['instance_id'],frozenset(people),HOME).check(store,cfg,pid)
    with store.connection() as db:
        ids={r[0] for r in db.execute('SELECT id FROM personas')}
    if ids!=set(people):raise StopRun('unexpected_persona')


async def prepare(root):
    confined(root,OUT)
    if root.exists():raise StopRun('preparation_exists_never_overwrite')
    root.mkdir(parents=True)
    data=fixture();atomic_json(root/'fixture.json',data)
    approved=ConfigStore(RuntimePaths(HOME)).load()
    for key,tokens,timeout in (('model',8192,180),('character_selector',512,60)):
        m=approved[key]
        if m['base_url'].rstrip('/')!='https://api.deepseek.com' or m['name']!='deepseek-flash' or m['max_tokens']!=tokens or m['timeout']!=timeout:
            raise StopRun('approved_model_contract_changed')
    if not approved['character_selector']['enabled']:raise StopRun('selector_disabled')
    seed_paths=RuntimePaths(root/'seed');seed=Store(seed_paths);seed.initialize();configure(seed_paths,approved)
    service=RuntimeService(seed);characters=CharacterService(seed);people={}
    for role,char in data['characters'].items():
        pid=service.create_persona(PersonaSpec.from_dict(data['spec']))['id'];service.use_persona(pid)
        with seed.connection(write=True) as db:append_version(db,pid,'relationship',{'description':'作者设定：双方是普通朋友，无共同经历记录。'},actor='test_author')
        v=characters.update(pid,char,characters.current(pid)['id'],identifier())
        people[role]={'persona_id':pid,'character_version':v['id']}
    policy=choose('hybrid_llm')
    # Existing model weights are read-only. All index/cache writes use seed_paths.
    provider=CharacterEmbeddingProvider(RuntimePaths(HOME),HOME/'models/character-embedding'/policy['config']['model_fingerprint'])
    diagnostic=provider.diagnose()
    if diagnostic['status']!='ready':raise StopRun('local_embedding_unavailable:'+diagnostic['reason'])
    index=CharacterSemanticIndex(seed,provider)
    for person in people.values():index.build(person['persona_id'],person['character_version'])
    atomic_json(root/'embedding.json',diagnostic)
    items=[]
    for case in [c for c in data['cases'] if c['real']]:
        for role,person in people.items():
            pid=person['persona_id'];pair=case['id']+'-'+role;pair_dir=root/'cases'/pair
            base=clone(seed,RuntimePaths(pair_dir/'authority'));service=RuntimeService(base);service.use_persona(pid);sid=service.create_session()['id']
            if case['reference']:
                with base.connection(write=True) as db:seed_turn(db,pid,sid,'r1-reference', [case['reference']['text']],reply=case['reference']['reply'])
            pool=None;comparison=None
            for arm,version in ARMS.items():
                paths=RuntimePaths(pair_dir/arm);store=clone(base,paths);cfg=configure(paths,approved,case)
                engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
                try:
                    identity_guard(store,cfg,pid,root,[p['persona_id'] for p in people.values()])
                    # Deterministic IDs are confined to offline acceptance setup.
                    ids=iter(uuid5(NAMESPACE_URL,'pcar-r1/'+pair+'/'+str(i)) for i in range(200))
                    with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                        ingress=new_policy(engine,pid,version)
                        receipts=[]
                        for i,text in enumerate(case['members']):
                            payload={'persona_id':pid,'session_id':sid,'text':text,'request_id':'r1-'+pair+'-'+str(i)}
                            if i==0 and case['reference']:payload['reference']={'kind':'turn','id':'r1-reference'}
                            receipts.append(ingress.accept(payload))
                        batch=ingress.get(receipts[0]['batch_id']);tid=ingress.action(batch['id'],'seal',batch['revision'],identifier(),pid)['turn_id']
                    with store.connection() as db:
                        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                        members=[m['text'] for m in load_turn_input(db,turn)];ref,_=references(db,turn)
                    char=json.loads(turn['character_snapshot']);signals=[s['act'] for s in resolve_behavior('\n\n'.join(members))['signals']]
                    if pool is None:
                        engine.cognitive.characters.index_factory=lambda _:index
                        pool=engine.cognitive.characters.local_pool(char,policy,members,signals)
                        atomic_json(pair_dir/'candidates.json',pool)
                    selector=selector_messages(pool,members,ref,4,approved['character_selector']['max_input_chars'],prompt_version=version)
                    control={'character':char,'profile':turn['profile_snapshot'],'members':members,'references':ref,
                             'model':safe_model(json.loads(turn['model_snapshot'])),'selector':safe_model(json.loads(turn['selector_model_snapshot'])),
                             'expression':turn['expression_snapshot'],'selector_input':selector[1]}
                    if comparison is not None and comparison!=control:raise StopRun('paired_sources_differ')
                    comparison=control
                    item={'key':pair+'-'+arm,'pair':pair,'case':case['id'],'role':role,'arm':arm,'persona_id':pid,'turn_id':tid,
                          'runtime':str(paths.home),'snapshot_hashes':{k:fingerprint(turn[k]) for k in FROZEN},
                          'candidate_file':str(pair_dir/'candidates.json'),'candidate_hash':fingerprint(pool),
                          'selector_messages':selector,'pool_guard':fingerprint([char,members,signals]),
                          'candidate_available_examples':sorted(set(case['allowed_examples'])&set(pool['payload']['roots']['example']))}
                    items.append(item)
                finally:await close(engine)
            print(json.dumps({'prepared':pair,'cloud_calls':0}),flush=True)
    frozen={'fixture_hash':sha(root/'fixture.json'),'items':items,'people':people,'generation_limit':64,
            'provider':{k:safe_model(approved[k]) for k in ('model','character_selector')},'created_at':timestamp()}
    atomic_json(root/'prepared.json',frozen)
    atomic_json(root/'freeze.json',{'prepared_hash':sha(root/'prepared.json'),'fixture_hash':sha(root/'fixture.json'),
                                  'candidate_hashes':{i['candidate_file']:sha(Path(i['candidate_file'])) for i in items}})
    return frozen


class Ledger:
    def __init__(self,path):
        self.path=path;self.data=read(path) if path.exists() else {'limit':64,'calls':[],'stop':None}
        self.validate()
    def save(self):
        try:atomic_json(self.path,self.data)
        except (OSError,ValueError,TypeError) as exc:
            # A bookkeeping error must never become an ordinary provider
            # failure with lexical fallback and a subsequent main dispatch.
            self.data['stop']='ledger_write_failed:'+type(exc).__name__
            try:atomic_json(self.path.parent/'stop.json',{'reason':self.data['stop'],'at':timestamp()})
            except (OSError,ValueError,TypeError):pass
            raise StopRun(self.data['stop']) from exc
    def validate(self):
        rows=self.data['calls'];keys=[r['key'] for r in rows]
        if self.data['limit']!=64 or len(rows)>64 or len(keys)!=len(set(keys)) or [r['ordinal'] for r in rows]!=list(range(1,len(rows)+1)):
            raise StopRun('ledger_integrity')
    def check(self):
        self.validate()
        if (self.path.parent/'stop.json').exists():raise StopRun('experiment_stopped_no_automatic_resume')
        if self.data['stop']:raise StopRun(self.data['stop'])
        if any(r['status']=='reserved' for r in self.data['calls']):raise StopRun('unresolved_call_never_replay')
        failures=0
        for row in reversed(self.data['calls']):
            if row['status']=='failed':failures+=1
            else:break
        if failures>=5 or any(r['status']=='failed' and r.get('seconds',0)>600 for r in self.data['calls']):
            self.data['stop']='failure_stop_rule';self.save();raise StopRun(self.data['stop'])
    def reserve(self,key,payload):
        self.check()
        if len(self.data['calls'])>=64 or any(r['key']==key for r in self.data['calls']):raise StopRun('call_limit_or_duplicate')
        row={'ordinal':len(self.data['calls'])+1,'key':key,'status':'reserved','started_at':timestamp(),**payload}
        self.data['calls'].append(row);self.save();return row
    def finish(self,row,status,**values):row.update(status=status,**values);self.save()


def verify(root,frozen):
    seal=read(root/'freeze.json')
    if sha(root/'prepared.json')!=seal['prepared_hash'] or sha(root/'fixture.json')!=seal['fixture_hash']:raise StopRun('frozen_package_changed')
    for path,value in seal['candidate_hashes'].items():
        confined(Path(path),root)
        if sha(Path(path))!=value:raise StopRun('candidate_sources_changed')


def reconcile(root,frozen,ledger):
    """Never silently resume a ledger/artifact gap or regenerate a paid result."""
    for item in frozen['items']:
        store=Store(RuntimePaths(Path(item['runtime'])))
        with store.connection() as db:
            for kind,table in (('selector','character_selection_attempts'),('main','attempts')):
                count=db.execute('SELECT count(*) FROM '+table+' WHERE turn_id=?',(item['turn_id'],)).fetchone()[0]
                recorded=sum(r['item']==item['key'] and r['kind']==kind for r in ledger.data['calls'])
                if count!=recorded:raise StopRun('ledger_runtime_count_mismatch')
        result=root/'results'/(item['key']+'.json')
        if result.exists():
            entry=read(result);calls=[r['ordinal'] for r in ledger.data['calls'] if r['item']==item['key']]
            if not calls or entry['calls']!=calls:raise StopRun('result_ledger_mismatch')
        elif any(r['item']==item['key'] for r in ledger.data['calls']):raise StopRun('unreconciled_item_never_replay')


def execution_sources():
    paths=list((ROOT/'persona_runtime').rglob('*.py'))+[Path(__file__),ROOT/'tests/expression_selection_fixture.py']
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}


class CaptureClient:
    def __init__(self,engine,item,root,frozen,ledger,real):
        self.engine,self.item,self.root,self.frozen,self.ledger,self.real=engine,item,root,frozen,ledger,real
        self.client=ChatCompletionsClient();self.last_selector=None
    def check(self):
        verify(self.root,self.frozen);i=self.item
        identity_guard(self.engine.store,self.engine.config,i['persona_id'],self.root,[p['persona_id'] for p in self.frozen['people'].values()])
        with self.engine.store.connection() as db:
            t=dict(db.execute('SELECT * FROM turns WHERE id=?',(i['turn_id'],)).fetchone())
            if {k:fingerprint(t[k]) for k in FROZEN}!=i['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')
            if self.last_selector:
                r=db.execute('SELECT status,error_code FROM character_selection_attempts WHERE turn_id=?',(i['turn_id'],)).fetchone()
                if r is None or r['status']=='running':raise StopRun('selector_ledger_not_terminal')
                if r['status']!='completed':self.ledger.finish(self.last_selector,'failed',validation_error=r['error_code'])
        self.ledger.check()
    async def complete(self,model,msg):
        self.check();kind='selector' if model.get('_json_output') else 'main';i=self.item
        if kind=='selector' and msg!=i['selector_messages']:raise StopRun('selector_input_changed')
        payload,_=build_request(model,msg)
        row=self.ledger.reserve(i['key']+'/'+kind,{'kind':kind,'item':i['key'],'real':self.real,
            'request':payload,'input_hash':fingerprint(msg),'model':safe_model(model)})
        started=time.monotonic()
        try:
            if self.real:result=await self.client.complete(model,msg)
            elif kind=='selector':
                available=i['candidate_available_examples'];answer=available[:1] if i['arm']=='experiment' else []
                result=(encoded({'schema_version':1,'lore_ids':[],'example_ids':answer}),{'total_tokens':0})
            else:
                from persona_runtime.reply_protocol import response_format
                version=response_format(model['_reply_mode'],model['_expression_snapshot'])['json_schema']['schema']['properties']['schema_version']['enum'][0]
                result=(encoded({'schema_version':version,'behavior_intent':'respond',
                    'messages':[{'content':'人工模拟回复。','content_kind':'chat','cancel_if_new_input':False}]}),{'total_tokens':0})
            self.ledger.finish(row,'returned',seconds=time.monotonic()-started,output=result[0],usage=result[1],diagnostics=getattr(result,'diagnostics',None))
            if kind=='selector':self.last_selector=row
            return result
        except (Exception,asyncio.CancelledError) as exc:
            self.ledger.finish(row,'failed',seconds=time.monotonic()-started,error=type(exc).__name__+':'+str(exc),usage=getattr(exc,'usage',None),diagnostics=getattr(exc,'diagnostics',None))
            if kind=='selector':self.last_selector=row
            raise


async def run(root,real,limit_cases=None):
    confined(root,OUT);frozen=read(root/'prepared.json');verify(root,frozen)
    ledger=Ledger(root/'ledger.json');ledger.check()
    reconcile(root,frozen,ledger)
    if real:
        if root!=OUT/'experiment':raise StopRun('real_experiment_root_required')
        gate=read(OUT/'offline/gate.json');simulation=read(OUT/'simulation/run-status.json')
        if not gate['passed'] or not simulation['complete'] or simulation['failed_calls']:raise StopRun('offline_gate_incomplete')
        source_file=root/'execution-sources.json';source=execution_sources()
        if source_file.exists():
            if read(source_file)!=source:raise StopRun('execution_source_changed')
        else:atomic_json(source_file,source)
    results=root/'results';results.mkdir(exist_ok=True)
    cases=list(dict.fromkeys(i['case'] for i in frozen['items']))
    for item in frozen['items']:
        if limit_cases and item['case'] not in cases[:limit_cases]:continue
        destination=results/(item['key']+'.json')
        if destination.exists():continue
        if any(r['item']==item['key'] for r in ledger.data['calls']):raise StopRun('unreconciled_item_never_replay')
        paths=RuntimePaths(Path(item['runtime']));store=Store(paths);cfg=ConfigStore(paths)
        engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
        client=CaptureClient(engine,item,root,frozen,ledger,real);engine.client=client;engine.selector.client=client;engine.observer.client=client
        pool=read(Path(item['candidate_file']))
        def frozen_pool(version,policy,members,scenario=()):
            if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
            return deepcopy(pool)
        engine.cognitive.characters.local_pool=frozen_pool
        try:
            client.check()
            await engine._run(item['turn_id'])
            client.check()
            turn=engine.get(item['turn_id'])
            with store.connection() as db:
                manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(item['turn_id'],))]
                attempts=[dict(r) for r in db.execute('SELECT id,status,error_code,usage_json FROM attempts WHERE turn_id=?',(item['turn_id'],))]
                selected=[dict(r) for r in db.execute('SELECT id,status,selected_json,error_code,usage_json FROM character_selection_attempts WHERE turn_id=?',(item['turn_id'],))]
            main=next((m for m in manifests if m['task_type']=='dialogue'),None)
            calls=[r for r in ledger.data['calls'] if r['item']==item['key']]
            for row in calls:
                if row['kind']=='main' and turn['status']!='completed':ledger.finish(row,'failed',validation_error=turn.get('error_code'))
                elif row['status']=='returned':ledger.finish(row,'completed')
            if main:
                actual=next(r['input_hash'] for r in calls if r['kind']=='main')
                if main['input_snapshot_hash']!=actual or main.get('prompt_assembly',{}).get('input_snapshot_hash')!=actual:raise StopRun('main_manifest_hash_mismatch')
            atomic_json(destination,{'item':item['key'],'turn':turn,'manifests':manifests,'attempts':attempts,'selection_attempts':selected,
                                    'calls':[r['ordinal'] for r in calls],'real':real})
            print(json.dumps({'item':item['key'],'status':turn['status'],'calls':len(ledger.data['calls']),'selector':selected[0]['status'] if selected else 'not_called'},ensure_ascii=False),flush=True)
            ledger.check()
        finally:await close(engine)
    atomic_json(root/'run-status.json',{'real':real,'items':len(list(results.glob('*.json'))),'calls':len(ledger.data['calls']),
        'complete':len(list(results.glob('*.json')))==32,'failed_calls':sum(r['status']=='failed' for r in ledger.data['calls']), 'stop':ledger.data['stop']})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','simulate','run'));parser.add_argument('--smoke',action='store_true');args=parser.parse_args()
    root=OUT/('simulation' if args.action=='simulate' else 'experiment')
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:
        if args.action=='prepare':asyncio.run(prepare(root))
        elif args.action=='simulate':
            if not (root/'prepared.json').exists():asyncio.run(prepare(root))
            asyncio.run(run(root,False,2 if args.smoke else None))
        else:asyncio.run(run(root,True,2 if args.smoke else None))
    except StopRun as exc:
        stopped={'action':args.action,'reason':str(exc),'at':timestamp()}
        if root.exists() and not (root/'stop.json').exists():atomic_json(root/'stop.json',stopped)
        atomic_json(OUT/'last-stop.json',stopped);print(json.dumps({'stopped':str(exc)}));sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
