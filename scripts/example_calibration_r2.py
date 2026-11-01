"""R2 offline-only prepare/simulate/report/verify. There is no paid execution mode."""
import argparse
import asyncio
from contextlib import contextmanager,ExitStack
from copy import deepcopy
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch
from uuid import uuid5,NAMESPACE_URL

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import (HOME,ARMS,FROZEN,read,sha,confined,new_policy,NoDispatch,
    close,clone,identity_guard,ChatEngine,RuntimePaths,Store,ConfigStore,identifier,fingerprint,encoded,
    timestamp,load_turn_input,references,selector_messages,RuntimeService,CharacterService,PersonaSpec,
    append_version,choose,CharacterEmbeddingProvider,CharacterSemanticIndex,resolve_behavior,seed_turn,freeze)
from scripts.retry_expression_selection_r1 import write_once,StopRun
from scripts.grok47_r1_retry import RetryReceipts
from scripts.verify_prompt_assembly_r0 import formal_hashes
from persona_runtime.config import atomic_json
from persona_runtime.chat import ChatCompletionsClient,ModelCompletion
from persona_runtime.provider_compat import build_request
from persona_runtime.character_selector_protocol import parse_output
from persona_runtime.reply_batch import validate_reply
from tests.example_calibration_fixture import VERSION,fixture,labels,rubric
from tests.example_calibration_responses import response_plan,reply
from scripts.example_calibration_analysis import validate_labels,material_row,statistics,pair_difference

OUT=HOME/'exports/acceptance/M7.6.3-R2-example-calibration'
OLD=HOME/'exports/acceptance/M7.6.3-R1-grok47/retry-three'


@contextmanager
def offline():
    stats={'external_connection_attempts':0,'http_attempts':0,'real_client_attempts':0,'real_calls':0}
    connect=socket.socket.connect;connect_ex=socket.socket.connect_ex
    def network(original,sock,address):
        if isinstance(address,tuple):
            try:allowed=ipaddress.ip_address(address[0]).is_loopback
            except ValueError:allowed=address[0]=='localhost'
            if not allowed:stats['external_connection_attempts']+=1;raise StopRun('external_network_forbidden')
        return original(sock,address)
    def deny_http(*a,**k):stats['http_attempts']+=1;raise StopRun('http_forbidden')
    async def deny_async_http(*a,**k):deny_http()
    async def deny_client(*a,**k):stats['real_client_attempts']+=1;raise StopRun('real_client_forbidden')
    with ExitStack() as stack:
        stack.enter_context(patch('socket.socket.connect',lambda s,a:network(connect,s,a)))
        stack.enter_context(patch('socket.socket.connect_ex',lambda s,a:network(connect_ex,s,a)))
        stack.enter_context(patch('httpx.Client.send',deny_http));stack.enter_context(patch('httpx.AsyncClient.send',deny_async_http))
        stack.enter_context(patch.object(ChatCompletionsClient,'complete',deny_client))
        stack.enter_context(patch.dict('os.environ',{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PYTHONDONTWRITEBYTECODE':'1'}))
        yield stats


def source_hashes():
    paths=list((ROOT/'persona_runtime').rglob('*.py'))+list((ROOT/'scripts').glob('example_calibration*.py'))+[
        ROOT/'tests/example_calibration_fixture.py',ROOT/'tests/example_calibration_responses.py']
    paths += [ROOT/'scripts'/n for n in ('expression_selection_r1.py','retry_expression_selection_r1.py',
        'grok47_r1_retry.py','report_expression_selection_r1.py')]
    return {p.relative_to(ROOT).as_posix():sha(p) for p in paths}


def protected():
    start=read(OUT/'start.json')
    if sha(OLD/'closure.json')!=start['dev8_closure_sha256']:raise StopRun('dev8_closure_changed')
    for path,digest in start['dev8_evidence_hashes'].items():
        if sha(OLD/path)!=digest:raise StopRun('dev8_evidence_changed:'+path)
    for path,digest in start['source_hashes'].items():
        if path.startswith('persona_runtime/') or path=='tests/fixtures/prompt-baseline-r0.json':
            if sha(ROOT/path)!=digest:raise StopRun('production_or_oracle_changed:'+path)
    if formal_hashes()!=start['formal_before']:raise StopRun('formal_files_changed')


def configure(paths,approved,case=None):
    cfg=ConfigStore(paths);cfg.initialize();value=cfg.load()
    # Only already-public scalar configuration is copied. Credentials never are.
    for section in ('model','character_selector'):
        for key in value[section]:
            if key!='encrypted_key' and key in approved[section]:value[section][key]=deepcopy(approved[section][key])
        value[section]['encrypted_key']=''
    value['organization']['daily_enabled']=False
    if case:value['model']['_dialogue_profile']=freeze('r2-'+case['id'],case['members'])
    atomic_json(cfg.file,value);return cfg


class IsolatedEmbeddingProvider(CharacterEmbeddingProvider):
    """Read existing validated weights; direct optional library caches to R2."""
    def __init__(self,paths,directory,cache):
        super().__init__(paths,directory);self.cache=cache;cache.mkdir(parents=True,exist_ok=True)
    @contextmanager
    def _model(self):
        from fastembed import TextEmbedding
        def isolated(*args,**kwargs):
            kwargs['cache_dir']=str(self.cache)
            return TextEmbedding(*args,**kwargs)
        with patch('fastembed.TextEmbedding',isolated):
            with super()._model() as value:yield value


async def prepare(root):
    confined(root,OUT);protected()
    if root.exists():raise StopRun('preparation_exists_never_overwrite')
    data=fixture();annotation=labels();reading=rubric();validate_labels(data,annotation,reading)
    root.mkdir()
    for name,value in (('fixture',data),('labels',annotation),('rubric',reading)):
        write_once(root/(name+'.json'),value)
    approved=ConfigStore(RuntimePaths(HOME)).load()
    for key in ('model','character_selector'):approved[key]={k:v for k,v in approved[key].items() if k!='encrypted_key'}
    if not approved['character_selector']['enabled']:raise StopRun('selector_disabled')
    seed_paths=RuntimePaths(root/'seed');seed=Store(seed_paths);seed.initialize();configure(seed_paths,approved)
    service=RuntimeService(seed);chars=CharacterService(seed);people={}
    for role,char in data['characters'].items():
        pid=service.create_persona(PersonaSpec.from_dict(data['spec']))['id'];service.use_persona(pid)
        with seed.connection(write=True) as db:append_version(db,pid,'relationship',{'description':'作者设定：普通朋友，无共同经历记录。'},actor='test_author')
        v=chars.update(pid,char,chars.current(pid)['id'],identifier());people[role]={'persona_id':pid,'character_version':v['id']}
    policy=choose('hybrid_llm')
    provider=IsolatedEmbeddingProvider(RuntimePaths(HOME),HOME/'models/character-embedding'/policy['config']['model_fingerprint'],root/'embedding-cache')
    diagnostic=provider.diagnose()
    if diagnostic['status']!='ready':raise StopRun('local_embedding_unavailable:'+diagnostic['reason'])
    index=CharacterSemanticIndex(seed,provider)
    for person in people.values():index.build(person['persona_id'],person['character_version'])
    write_once(root/'embedding.json',diagnostic)
    items=[];pair_index=0
    for case in data['cases']:
        for role,person in people.items():
            pid=person['persona_id'];pair=case['id']+'-'+role;pair_dir=root/'cases'/pair
            base=clone(seed,RuntimePaths(pair_dir/'authority'));service=RuntimeService(base);service.use_persona(pid);sid=service.create_session()['id']
            if case['reference']:
                with base.connection(write=True) as db:seed_turn(db,pid,sid,'r2-reference',[case['reference']['text']],reply=case['reference']['reply'])
            pool=None;comparison=None
            order=('control','experiment') if pair_index%2==0 else ('experiment','control');pair_index+=1
            for arm in order:
                paths=RuntimePaths(pair_dir/arm);store=clone(base,paths);cfg=configure(paths,approved,case)
                engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
                try:
                    identity_guard(store,cfg,pid,root,[p['persona_id'] for p in people.values()])
                    ids=iter(uuid5(NAMESPACE_URL,'pcar-r2/'+pair+'/'+str(n)) for n in range(300))
                    with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                        ingress=new_policy(engine,pid,ARMS[arm]);receipts=[]
                        for n,text in enumerate(case['members']):
                            payload={'persona_id':pid,'session_id':sid,'text':text,'request_id':'r2-'+pair+'-'+str(n)}
                            if n==0 and case['reference']:payload['reference']={'kind':'turn','id':'r2-reference'}
                            receipts.append(ingress.accept(payload))
                        b=ingress.get(receipts[0]['batch_id']);tid=ingress.action(b['id'],'seal',b['revision'],identifier(),pid)['turn_id']
                    with store.connection() as db:
                        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                        members=[m['text'] for m in load_turn_input(db,turn)];ref,_=references(db,turn)
                    char=json.loads(turn['character_snapshot']);signals=[s['act'] for s in resolve_behavior('\n\n'.join(members))['signals']]
                    if pool is None:
                        engine.cognitive.characters.index_factory=lambda _:index
                        pool=engine.cognitive.characters.local_pool(char,policy,members,signals)
                        write_once(pair_dir/'candidates.json',pool)
                    payload=selector_messages(pool,members,ref,4,approved['character_selector']['max_input_chars'],prompt_version=ARMS[arm])
                    control={'character':char,'profile':turn['profile_snapshot'],'members':members,'references':ref,
                        'model':turn['model_snapshot'],'selector':turn['selector_model_snapshot'],
                        'expression':turn['expression_snapshot'],'selector_input':payload[1]}
                    if comparison is not None and comparison!=control:raise StopRun('paired_sources_differ')
                    comparison=control
                    answer,missing=response_plan(case['id'],arm,pool)
                    key=pair+'-'+arm
                    write_once(root/'accepted'/ (key+'.json'),{k:turn[k] for k in FROZEN})
                    items.append({'key':key,'pair':pair,'case':case['id'],'role':role,'arm':arm,'persona_id':pid,'turn_id':tid,
                        'runtime':str(paths.home),'snapshot_hashes':{k:fingerprint(turn[k]) for k in FROZEN},
                        'candidate_file':str(pair_dir/'candidates.json'),'candidate_hash':fingerprint(pool),
                        'selector_messages':payload,'pool_guard':fingerprint([char,members,signals]),
                        'mock_answer':answer,'mock_missing_candidates':missing})
                finally:await close(engine)
        print(json.dumps({'prepared_case':case['id'],'requests':len(items),'real_calls':0}),flush=True)
    prepared={'version':VERSION,'items':items,'people':people,'future_order':[i['key'] for i in items if i['case'] in data['future_case_ids']],
        'real_call_limit':0,'created_at':timestamp(),'provider':{k:approved[k] for k in ('model','character_selector')}}
    write_once(root/'prepared.json',prepared)
    files=[root/n for n in ('fixture.json','labels.json','rubric.json','prepared.json','embedding.json')]+list((root/'accepted').glob('*.json'))+[Path(i['candidate_file']) for i in items]
    write_once(root/'freeze.json',{'files':{p.relative_to(root).as_posix():sha(p) for p in files},'execution_sources':source_hashes()})
    return prepared


def verify_freeze(root):
    confined(root,OUT);seal=read(root/'freeze.json')
    for name,value in seal['files'].items():
        if sha(root/name)!=value:raise StopRun('frozen_file_changed:'+name)
    if seal['execution_sources']!=source_hashes():raise StopRun('execution_sources_changed')
    validate_labels(read(root/'fixture.json'),read(root/'labels.json'),read(root/'rubric.json'))
    return read(root/'prepared.json')


def check_item(root,item,engine):
    identity_guard(engine.store,engine.config,item['persona_id'],root,[p['persona_id'] for p in read(root/'prepared.json')['people'].values()])
    with engine.store.connection() as db:
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(item['turn_id'],)).fetchone())
        if {k:fingerprint(turn[k]) for k in FROZEN}!=item['snapshot_hashes']:raise StopRun('accepted_snapshot_changed')


class SimulatedClient:
    def __init__(self,item,pool,ledger):self.item,self.pool,self.ledger=item,pool,ledger
    async def complete(self,model,messages):
        if model.get('encrypted_key'):raise StopRun('credential_in_simulation')
        kind='selector' if model.get('_character_selection') else 'main'
        if kind=='selector' and messages!=self.item['selector_messages']:raise StopRun('selector_input_changed')
        row=self.ledger.reserve_attempt(self.item['key']+'/'+kind,{'item':self.item['key'],'kind':kind,
            'simulation':True,'request':build_request(model,messages)[0],'input_hash':fingerprint(messages)})
        try:
            text=encoded(self.item['mock_answer']) if kind=='selector' else encoded({'schema_version':3,'behavior_intent':'respond',
                'messages':[{'content':reply(self.item['case'],self.item['arm']),'content_kind':'chat','cancel_if_new_input':False}]})
            if kind=='selector':parse_output(text,self.pool,4)
            else:validate_reply(text,model['_reply_mode'],model['_expression_snapshot'])
            backend='sim_backend_a'
            if self.item['case']=='C08' and self.item['arm']=='experiment':backend='sim_backend_b'
            diagnostics={'system_fingerprint':backend,'response_model':'simulated','finish_reason':'stop'}
            if self.item['case']=='C07':diagnostics.pop('system_fingerprint')
            result=ModelCompletion(text,{'total_tokens':0},diagnostics)
        except BaseException as error:
            self.ledger.finish(row,'interrupted' if isinstance(error,asyncio.CancelledError) else 'failed',error=str(error));raise
        self.ledger.finish(row,'completed',output=text,usage=result[1],diagnostics=diagnostics)
        return result


def collect(engine,item):
    with engine.store.connection() as db:
        manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(item['turn_id'],))]
        records={key:[dict(r) for r in db.execute('SELECT id,status,error_code,usage_json FROM '+table+' WHERE turn_id=?',(item['turn_id'],))]
            for key,table in (('selection_attempts','character_selection_attempts'),('attempts','attempts'))}
    return {'item':item['key'],'turn':engine.get(item['turn_id']),'manifests':manifests,**records,'simulation':True}


def reconcile(value,calls):
    for kind,field in (('selector','selection_attempts'),('main','attempts')):
        observed=[r for r in calls if r['kind']==kind];audit=[]
        for record in value[field]:
            usage=json.loads(record['usage_json'] or '{}');audit.extend(usage.get('generation_retry',{}).get('attempts',[]))
        if len(audit)!=len(observed):raise StopRun('runtime_receipt_count_mismatch')
        for a,r in zip(audit,observed):
            if a['index']!=r['attempt_index'] or a['status']!=r['status'] or a['request_hash']!=fingerprint(r['request']):raise StopRun('runtime_receipt_mismatch')
    return True


async def simulate(root):
    protected();prepared=verify_freeze(root);output=root/'simulation'
    if output.exists():raise StopRun('simulation_exists_never_replay')
    output.mkdir();ledger=RetryReceipts(output/'receipts',limit=768)
    for item in prepared['items']:
        paths=RuntimePaths(output/'runtime'/item['key']);store=clone(Store(RuntimePaths(Path(item['runtime']))),paths)
        cfg=configure(paths,prepared['provider'],next(c for c in read(root/'fixture.json')['cases'] if c['id']==item['case']))
        pool=read(Path(item['candidate_file']));client=SimulatedClient(item,pool,ledger)
        engine=ChatEngine(store,cfg,client);engine.store.character_committed=lambda *_:[]
        engine.selector.client=engine.observer.client=client
        def frozen_pool(version,policy,members,scenario=()):
            if fingerprint([version,members,scenario])!=item['pool_guard'] or fingerprint(pool)!=item['candidate_hash']:raise StopRun('candidate_guard')
            return deepcopy(pool)
        engine.cognitive.characters.local_pool=frozen_pool
        try:
            check_item(root,item,engine);await engine._run(item['turn_id']);check_item(root,item,engine)
            value=collect(engine,item);calls=[r for r in ledger.rows() if r['item']==item['key']]
            if value['turn']['status']!='completed' or len(calls)!=2:raise StopRun('normal_simulation_failed:'+item['key']+':'+str(value['turn'].get('error_code')))
            reconcile(value,calls)
            manifest=next(m for m in value['manifests'] if m['task_type']=='dialogue')
            main=next(r for r in calls if r['kind']=='main')
            if manifest['input_snapshot_hash']!=main['input_hash']:raise StopRun('actual_input_hash_mismatch')
            write_once(output/'results'/(item['key']+'.json'),value)
        finally:await close(engine)
        if item['key']==prepared['items'][-1]['key'] or (prepared['items'].index(item)+1)%8==0:
            print(json.dumps({'simulated_requests':prepared['items'].index(item)+1,'physical_simulated_calls':len(ledger.rows()),'real_calls':0}),flush=True)
    protected();verify_freeze(root)
    write_once(output/'run-status.json',{'complete':True,'requests':96,'simulated_calls':len(ledger.rows()),'real_calls':0,'qq':0,'effect_status':'not_run'})


def report(root):
    prepared=verify_freeze(root);values={p.stem:read(p) for p in (root/'simulation/results').glob('*.json')}
    rows=RetryReceipts(root/'simulation/receipts',limit=768).rows();calls={(r['item'],r['kind']):r for r in rows if r['status']=='completed'}
    annotation=read(root/'labels.json');items={i['key']:i for i in prepared['items']};material=[]
    for key,value in values.items():
        main=next(m for m in value['manifests'] if m['task_type']=='dialogue')
        row=material_row(items[key],read(Path(items[key]['candidate_file'])),main['prompt_assembly']['material_selection'],annotation)
        row.update(final_input_hash=main['input_snapshot_hash'],budget_decisions=main.get('character_retrieval',{}))
        material.append(row)
    differences=[pair_difference(pair,items,values,calls) for pair in dict.fromkeys(i['pair'] for i in prepared['items'])]
    result={'version':VERSION,'preparation_complete':len(items)==96,'simulation_complete':len(values)==96,
        'simulated_requests':len(values),'simulated_calls':len(rows),'real_calls':0,'qq':0,
        'effect_status':'not_run','naturalness_improvement_proven':False,'real_quality_verdicts':None,
        'simulated_material_statistics':statistics(material),
        'all_request_differences_explained':all(r.get('explained') for r in differences),
        'all_input_hashes_match':all(r.get('final_hashes_match') for r in differences),
        'candidate_limitations':[r['key'] for r in material if r['primary']['missing_candidates']],
        'mock_response_limitations':[i['key'] for i in prepared['items'] if i['mock_missing_candidates']],
        'disclaimer':'人工脚本模拟只证明工具与数据流，不能据其选择率或输出判定v2/v3质量。'}
    for name,value in (('material',material),('request-differences',differences),('summary',result)):
        atomic_json(root/'analysis'/(name+'.json'),value)
    return result


def verify(root):
    protected();prepared=verify_freeze(root);report_value=report(root)
    if not all(report_value[k] for k in ('preparation_complete','simulation_complete','all_request_differences_explained','all_input_hashes_match')):raise StopRun('verification_failed')
    rows=RetryReceipts(root/'simulation/receipts',limit=768).check()
    for item in prepared['items']:
        reconcile(read(root/'simulation/results'/(item['key']+'.json')),[r for r in rows if r['item']==item['key']])
    from scripts.example_calibration_faults import run_faults
    fault_root=root/'faults'
    faults=read(fault_root/'summary.json') if (fault_root/'summary.json').exists() else asyncio.run(run_faults(fault_root))
    if not faults['passed']:raise StopRun('fault_verification_failed')
    from tests.prompt_baseline_fixture import capture_all
    expected=read(ROOT/'tests/fixtures/prompt-baseline-r0.json');actual=capture_all()
    comparisons=[{'scenario':key,'equal':encoded(expected['scenarios'].get(key))==encoded(value),
        'expected_hash':fingerprint(expected['scenarios'].get(key)),'actual_hash':fingerprint(value)} for key,value in actual.items()]
    r0={'passed':set(actual)==set(expected['scenarios']) and all(r['equal'] for r in comparisons),
        'scenarios':comparisons,'baseline_sha256':sha(ROOT/'tests/fixtures/prompt-baseline-r0.json')}
    atomic_json(root/'r0-differences.json',r0)
    if not r0['passed']:raise StopRun('r0_difference')
    protected()
    value={'passed':True,'requests':96,'pairs':48,'simulated_calls':len(rows),'real_calls':0,'qq':0,
        'fault_groups':faults['groups'],'fault_subcases':faults['subcases'],'r0_frozen_scenarios':len(actual),
        'production_unchanged':True,'dev8_evidence_unchanged':True,'formal_unchanged':True,'effect_status':'not_run'}
    atomic_json(root/'verification.json',value);return value


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','report','verify'))
    p.add_argument('--batch',default='batch-001');args=p.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}',args.batch):p.error('invalid batch name')
    root=OUT/args.batch;confined(root,OUT)
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r2-offline.lock')
    try:
        with offline() as network:
            if args.action=='prepare':asyncio.run(prepare(root));result={'prepared':96,'real_calls':0}
            elif args.action=='simulate':asyncio.run(simulate(root));result={'simulated':96,'real_calls':0}
            elif args.action=='report':result=report(root)
            else:result=verify(root)
        atomic_json(root/('network-'+args.action+'.json'),network)
        print(json.dumps({k:v for k,v in result.items() if k not in ('simulated_material_statistics','candidate_limitations','mock_response_limitations')},ensure_ascii=False),flush=True)
    except (Exception,StopRun) as exc:
        if root.exists():write_once(root/('failure-'+args.action+'-'+identifier()+'.json'),{'action':args.action,'error':str(exc),'real_calls':0,'at':timestamp()})
        raise
    finally:lock.close()


if __name__=='__main__':main()
