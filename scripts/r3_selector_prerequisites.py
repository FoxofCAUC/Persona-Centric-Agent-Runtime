"""R3 prepare/simulate/report/verify. All four actions are offline-only."""
import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import ipaddress
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch
from uuid import uuid5,NAMESPACE_URL
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import (HOME,FROZEN,read,sha,confined,new_policy,NoDispatch,
    close,clone,identity_guard,ChatEngine,RuntimePaths,Store,ConfigStore,identifier,fingerprint,encoded,
    timestamp,load_turn_input,references,selector_messages,RuntimeService,CharacterService,PersonaSpec,
    append_version,choose,CharacterSemanticIndex,resolve_behavior,seed_turn,freeze)
from scripts.example_calibration_r2 import offline as legacy_offline,IsolatedEmbeddingProvider,collect,reconcile
from scripts.example_calibration_analysis import pair_difference
from scripts.retry_expression_selection_r1 import write_once,StopRun
from scripts.grok47_r1_retry import RetryReceipts
from scripts.verify_prompt_assembly_r0 import formal_hashes
from persona_runtime.config import atomic_json
from persona_runtime.chat import ModelCompletion
from persona_runtime.provider_compat import build_request
from persona_runtime.character_selector_protocol import parse_output,prompt_for
from persona_runtime.reply_batch import validate_reply
from scripts.r3_prerequisite_analysis import ARMS,validate_labels,material_row,statistics
from tests.r3_prerequisite_fixture import VERSION,fixture
from tests.r3_prerequisite_annotations import labels
from tests.r3_prerequisite_responses import response_plan,reply

OUT=HOME/'exports/acceptance/M7.6.3-R3-selector-prerequisites'
OLD=OUT.parent/'M7.6.3-R2.1-frozen-real-comparison'
ALLOWED_CHANGES={'persona_runtime/character_selector_protocol.py','persona_runtime/prompt_assembly.py',
 'persona_runtime/__init__.py','pyproject.toml','uv.lock','AGENTS.md','README.md','docs/HANDOFF.md',
 'docs/STATUS.md','docs/acceptance/README.md','docs/pcar_project_plan_zh.md','docs/后续开发指导与规划衔接.md'}


@contextmanager
def offline():
    """Also stop DNS and datagram escapes, while allowing asyncio loopback setup."""
    lookup=socket.getaddrinfo
    with legacy_offline() as stats:
        def resolve(host,*args,**kwargs):
            try:local=host is None or host=='localhost' or ipaddress.ip_address(host).is_loopback
            except ValueError:local=False
            if not local:
                stats['external_connection_attempts']+=1;raise StopRun('external_dns_forbidden')
            return lookup(host,*args,**kwargs)
        def datagram(*args,**kwargs):
            stats['external_connection_attempts']+=1;raise StopRun('datagram_forbidden')
        with patch('socket.getaddrinfo',resolve),patch('socket.socket.sendto',datagram):yield stats


def source_hashes():
    paths=set(read(OLD/'real/freeze.json')['execution_sources'])
    for pattern in ('scripts/r3_*.py','tests/r3_*.py','tests/test_r3_*.py'):
        paths.update(p.relative_to(ROOT).as_posix() for p in ROOT.glob(pattern))
    return {name:sha(ROOT/name) for name in sorted(paths)}


def protected():
    start=read(OUT/'start.json')
    if sha(OLD/'closure.json')!=start['old_closure_sha256']:raise StopRun('R2_1_closure_changed')
    for name,digest in start['old_evidence_hashes'].items():
        if sha(OLD/name)!=digest:raise StopRun('R2_1_evidence_changed:'+name)
    prior=read(OLD/'start.json');r2=OUT.parent/'M7.6.3-R2-example-calibration'
    if sha(r2/'closure.json')!=prior['old_closure_sha256']:raise StopRun('R2_closure_changed')
    for name,digest in prior['old_evidence_hashes'].items():
        if sha(r2/name)!=digest:raise StopRun('R2_evidence_changed:'+name)
    for name,digest in start['source_hashes'].items():
        if name not in ALLOWED_CHANGES and sha(ROOT/name)!=digest:raise StopRun('unapproved_source_change:'+name)
    if formal_hashes()!=start['formal_before']:raise StopRun('formal_changed')


def configure(paths,approved,case=None):
    cfg=ConfigStore(paths);cfg.initialize();value=cfg.load()
    for section in ('model','character_selector'):
        for key in value[section]:
            if key!='encrypted_key' and key in approved[section]:value[section][key]=deepcopy(approved[section][key])
        value[section]['encrypted_key']=''
    value['organization']['daily_enabled']=False
    if case:value['model']['_dialogue_profile']=freeze('r3-'+case['id'],case['members'])
    atomic_json(cfg.file,value);return cfg


async def prepare(root):
    confined(root,OUT);protected()
    if root.exists():raise StopRun('preparation_exists_never_overwrite')
    data=fixture();annotation=labels();validate_labels(data,annotation)
    root.mkdir()
    for name,value in (('fixture',data),('labels',annotation),('prompts',{v:prompt_for(v) for v in ARMS.values()})):
        write_once(root/(name+'.json'),value)
    approved=read(OLD/'real/prepared.json')['provider']
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
                with base.connection(write=True) as db:seed_turn(db,pid,sid,'r3-reference',[case['reference']['text']],reply=case['reference']['reply'])
            pool=None;comparison=None
            order=('control','experiment') if pair_index%2==0 else ('experiment','control');pair_index+=1
            for arm in order:
                paths=RuntimePaths(pair_dir/arm);store=clone(base,paths);cfg=configure(paths,approved,case)
                engine=ChatEngine(store,cfg,NoDispatch());engine.store.character_committed=lambda *_:[]
                try:
                    identity_guard(store,cfg,pid,root,[p['persona_id'] for p in people.values()])
                    ids=iter(uuid5(NAMESPACE_URL,'pcar-r3/'+pair+'/'+str(n)) for n in range(300))
                    with patch('persona_runtime.models.uuid4',side_effect=lambda:next(ids)),patch.object(engine,'_schedule'):
                        ingress=new_policy(engine,pid,ARMS[arm]);receipts=[]
                        for n,text in enumerate(case['members']):
                            payload={'persona_id':pid,'session_id':sid,'text':text,'request_id':'r3-'+pair+'-'+str(n)}
                            if n==0 and case['reference']:payload['reference']={'kind':'turn','id':'r3-reference'}
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
    prepared={'version':VERSION,'items':items,'people':people,'future_order':[i['key'] for i in items],
        'real_call_limit':0,'created_at':timestamp(),'provider':{k:approved[k] for k in ('model','character_selector')}}
    write_once(root/'prepared.json',prepared)
    files=[root/n for n in ('fixture.json','labels.json','prompts.json','prepared.json','embedding.json')]+list((root/'accepted').glob('*.json'))+[Path(i['candidate_file']) for i in items]
    write_once(root/'freeze.json',{'files':{p.relative_to(root).as_posix():sha(p) for p in files},'execution_sources':source_hashes()})
    return prepared

def verify_freeze(root):
    confined(root,OUT);seal=read(root/'freeze.json')
    for name,value in seal['files'].items():
        if sha(root/name)!=value:raise StopRun('frozen_file_changed:'+name)
    if seal['execution_sources']!=source_hashes():raise StopRun('execution_sources_changed')
    validate_labels(read(root/'fixture.json'),read(root/'labels.json'))
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
            diagnostics={'system_fingerprint':backend,'response_model':'simulated','finish_reason':'stop'}
            diagnostics['simulated']=True
            result=ModelCompletion(text,{'total_tokens':0},diagnostics)
        except BaseException as error:
            self.ledger.finish(row,'interrupted' if isinstance(error,asyncio.CancelledError) else 'failed',error=str(error));raise
        self.ledger.finish(row,'completed',output=text,usage=result[1],diagnostics=diagnostics)
        return result

async def simulate(root):
    protected();prepared=verify_freeze(root);output=root/'simulation'
    if output.exists():raise StopRun('simulation_exists_never_replay')
    output.mkdir();ledger=RetryReceipts(output/'receipts',limit=384)
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
    write_once(output/'run-status.json',{'complete':True,'requests':48,'simulated_calls':len(ledger.rows()),'real_calls':0,'qq':0,'effect_status':'not_run'})

def report(root):
    prepared=verify_freeze(root);values={p.stem:read(p) for p in (root/'simulation/results').glob('*.json')}
    rows=RetryReceipts(root/'simulation/receipts',limit=384).rows();calls={(r['item'],r['kind']):r for r in rows if r['status']=='completed'}
    if any(not r.get('simulation') or r.get('real') for r in rows+list(values.values())):raise StopRun('non_simulation_evidence')
    annotation=read(root/'labels.json');items={i['key']:i for i in prepared['items']};material=[]
    for key,value in values.items():
        main=next(m for m in value['manifests'] if m['task_type']=='dialogue')
        row=material_row(items[key],read(Path(items[key]['candidate_file'])),main['prompt_assembly']['material_selection'],annotation)
        row.update(final_input_hash=main['input_snapshot_hash'],budget_decisions=main.get('character_retrieval',{}))
        material.append(row)
    differences=[pair_difference(pair,items,values,calls) for pair in dict.fromkeys(i['pair'] for i in prepared['items'])]
    for row in differences:
        row.update(eligible_for_real_effect_analysis=False,quality_inference='not_available_from_simulation')
        row['same_examples_same_request']=bool(row.get('examples_changed') or row.get('main_request_equal'))
        row['explained']=bool(row.get('explained') and row['same_examples_same_request'])
    result={'version':VERSION,'preparation_complete':len(items)==48,'simulation_complete':len(values)==48,
        'simulated_requests':len(values),'simulated_calls':len(rows),'real_calls':0,'qq':0,
        'effect_status':'not_run','naturalness_improvement_proven':False,'real_quality_verdicts':None,
        'simulated_material_statistics':statistics(material),
        'all_request_differences_explained':all(r.get('explained') for r in differences),
        'all_input_hashes_match':all(r.get('final_hashes_match') for r in differences),
        'all_expected_selectors_applied':len(material)==48 and all(r['valid_selector'] for r in material),
        'candidate_limitations':[r['key'] for r in material if r['missing_candidates']],
        'mock_response_limitations':[i['key'] for i in prepared['items'] if i['mock_missing_candidates']],
        'disclaimer':'人工脚本模拟只证明工具与数据流，不能据其选择率或输出判定v3/v4质量。'}
    for name,value in (('material',material),('request-differences',differences),('summary',result)):
        atomic_json(root/'analysis'/(name+'.json'),value)
    return result


def verify(root):
    protected();prepared=verify_freeze(root);report_value=report(root)
    if not all(report_value[k] for k in ('preparation_complete','simulation_complete','all_request_differences_explained','all_input_hashes_match','all_expected_selectors_applied')):raise StopRun('verification_failed')
    rows=RetryReceipts(root/'simulation/receipts',limit=384).check()
    if len(rows)!=96:raise StopRun('normal_call_count')
    for item in prepared['items']:
        reconcile(read(root/'simulation/results'/(item['key']+'.json')),[r for r in rows if r['item']==item['key']])
    from scripts.r3_prerequisite_faults import run_faults
    fault_root=root/'faults'
    faults=read(fault_root/'summary.json') if (fault_root/'summary.json').exists() else asyncio.run(run_faults(fault_root))
    if not faults['passed']:raise StopRun('fault_verification_failed')
    from scripts.r3_prerequisite_compat import capture
    if sha(OUT/'legacy-request-baseline.json')!=read(OUT/'baseline-integrity.json')['sha256']:raise StopRun('legacy_baseline_changed')
    prior=read(OUT/'legacy-request-baseline.json');actual=capture(prepared['provider']['character_selector'])
    comparisons=[{'section':section,'key':key,'equal':encoded(value)==encoded(actual[section].get(key)),
        'expected_hash':fingerprint(value),'actual_hash':fingerprint(actual[section].get(key))}
        for section,values in prior.items() for key,value in values.items()]
    legacy_ok=set(actual)==set(prior) and all(set(actual[s])==set(prior[s]) for s in prior) and all(r['equal'] for r in comparisons)
    r0=read(ROOT/'tests/fixtures/prompt-baseline-r0.json')['scenarios']
    r0_ok=encoded(actual['main_requests'])==encoded(r0)
    atomic_json(root/'legacy-request-differences.json',{'passed':legacy_ok,'comparisons':comparisons})
    atomic_json(root/'r0-differences.json',{'passed':r0_ok,'scenarios':34,'baseline_hash':sha(ROOT/'tests/fixtures/prompt-baseline-r0.json')})
    if not legacy_ok or not r0_ok:raise StopRun('legacy_or_R0_difference')
    protected()
    value={'passed':True,'requests':48,'pairs':24,'simulated_calls':len(rows),'real_calls':0,'qq':0,
        'fault_groups':faults['groups'],'fault_subcases':faults['subcases'],'r0_frozen_scenarios':34,
        'old_prompts_and_requests_unchanged':True,'old_evidence_unchanged':True,'formal_unchanged':True,'effect_status':'not_run'}
    atomic_json(root/'verification.json',value);return value


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','report','verify'));args=p.parse_args()
    root=OUT/'batch-001';confined(root,OUT)
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r3-offline.lock')
    try:
        with offline() as network:
            if args.action=='prepare':asyncio.run(prepare(root));result={'prepared':48,'real_calls':0}
            elif args.action=='simulate':asyncio.run(simulate(root));result={'simulated':48,'real_calls':0}
            elif args.action=='report':result=report(root)
            else:result=verify(root)
        atomic_json(root/('network-'+args.action+'.json'),network)
        print(json.dumps({k:v for k,v in result.items() if k not in ('simulated_material_statistics','candidate_limitations','mock_response_limitations')},ensure_ascii=False),flush=True)
    except (Exception,StopRun) as exc:
        if root.exists():write_once(root/('failure-'+args.action+'-'+identifier()+'.json'),{'action':args.action,'error':str(exc),'real_calls':0,'at':timestamp()})
        raise
    finally:lock.close()


if __name__=='__main__':main()
