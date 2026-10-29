"""Resumable bounded R2 selector evaluation using isolated authority and real provider.
Only --execute dispatches cloud calls. Never invokes the primary reply model.
"""
import argparse,asyncio,json,sys,time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.chat import ChatEngine
from persona_runtime.character import CharacterService,read_fact
from persona_runtime.character_embedding import CharacterEmbeddingProvider,digest,file_hash
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_retrieval import load_config,rank_candidates
from persona_runtime.character_selector_protocol import SYSTEM,apply_selection,candidates
from persona_runtime.character_context import select_context
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier,generic_persona,RuntimeErrorBase
from persona_runtime.quotas import QuotaService
from character_evaluation import selection_check,ranking_metrics

def save(path,value):atomic_json(path,value)

def summarize(rows):
    groups={k:[r for r in rows if r['group']==k] for k in ('positive','negative','contract')}
    report={k:{'passed':sum(r['passed'] for r in v),'count':len(v)} for k,v in groups.items()}
    report['types']={k:{'passed':sum(r['passed'] for r in groups['positive'] if r['kind']==k),'count':sum(r['kind']==k for r in groups['positive'])} for k in ('lore','example')}
    report['degraded']=sum(r['degraded'] for r in rows);report['hard_violations']=sum(r['hard_violation'] for r in rows)
    report['development_passed']=(len(rows)==64 and report['positive']['passed']>=28 and all(v['passed']>=14 for v in report['types'].values()) and report['negative']['passed']==24 and report['contract']['passed']==8 and not report['hard_violations'])
    if len(rows)==48:
        positive=groups['positive'];negative=groups['negative'];contracts=groups['contract']
        recall=sum(r['local_candidates']['recall5'] for r in positive)/20
        lexical=sum(r['lexical_candidates']['recall5'] for r in positive)/20
        report.update(local_recall5=recall,lexical_recall5=lexical,mrr5=sum(r['local_candidates']['mrr5'] for r in positive)/20,
            independent_passed=(len(positive)==20 and len(negative)==20 and len(contracts)==8 and recall>=.85 and recall-lexical>=.1 and report['positive']['passed']>=17 and sum(not r['passed'] for r in negative)<=1 and not report['hard_violations'] and report['contract']['passed']==8))
    return report

async def run(fixture,evidence,execute,simulate=False,reconcile=False):
    evidence.mkdir(parents=True,exist_ok=True);corpus=json.loads(fixture.read_text(encoding='utf-8'));alg=load_config()
    frozen={'fixture_sha256':file_hash(fixture),'prompt_sha256':digest(SYSTEM),'algorithm':alg,'labels_version':corpus['evaluation_version'],'maximum_selector_calls':len(corpus['cases']),'primary_calls':0,'simulated':simulate}
    freeze=evidence/'freeze.json'
    if freeze.exists():assert json.loads(freeze.read_text())==frozen,'Frozen inputs changed'
    else:save(freeze,frozen)
    if not execute:print('Frozen; no provider calls');return
    paths=RuntimePaths(evidence/'runtime');store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    setup=evidence/'setup.json'
    if setup.exists():info=json.loads(setup.read_text(encoding='utf-8'))
    else:
        formal={'model':{'base_url':'http://127.0.0.1:1','name':'fake','encrypted_key':''}} if simulate else ConfigStore(RuntimePaths.from_environment()).load();value=cfg.load()
        for key in ('base_url','name','encrypted_key'):value['character_selector'][key]=formal['model'][key]
        value['character_selector']['enabled']=True;value['organization']['daily_enabled']=False
        save(cfg.file,value);info={'versions':{},'turns':{}}
    engine=ChatEngine(store,cfg);chars=CharacterService(store);ingress=IngressService(engine)
    if simulate:
        async def fake(*args):return '{"schema_version":1,"lore_ids":[],"example_ids":[]}',{'total_tokens':0}
        engine.client.complete=fake
    runtime=engine.cognitive.characters
    home=RuntimePaths.from_environment();provider=CharacterEmbeddingProvider(home,home.home/'models/character-embedding'/alg['model_fingerprint'])
    index=CharacterSemanticIndex(store,provider);runtime.index_factory=lambda _:index
    runtime.notify=lambda *_:[];store.character_committed=runtime.notify
    # setup is recoverable before first cloud call; don't silently recreate after an uncertain crash.
    if not setup.exists():
        for key,data in corpus['characters'].items():
            pid=engine.service.create_persona(generic_persona())['id'];engine.service.use_persona(pid)
            v=chars.update(pid,data,chars.current(pid)['id'],identifier());info['versions'][key]=v
            p=ingress.current_policy(pid,'desktop');ingress.update_policy(pid,'desktop','legacy_per_message',p['id'],identifier(),character_retrieval_mode='hybrid_llm')
        for case in corpus['cases']:
            version=info['versions'][case['character']];pid=version['persona_id'];engine.service.use_persona(pid)
            # Real batch members; explicitly seal without timers, no primary scheduling.
            p=ingress.current_policy(pid,'desktop');ingress.update_policy(pid,'desktop','natural_batch',p['id'],identifier())
            with patch.object(engine,'_schedule'):
                for n,text in enumerate(case['members']):receipt=ingress.accept({'text':text,'request_id':case['id']+'-'+str(n),'channel':'desktop'})
                # Internal atomic seal used by the same normal capacity/timer worker.
                with store.connection(write=True) as db:
                    batch=dict(db.execute('SELECT * FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone())
                    ingress._seal(db,batch,'manual')
            batch=ingress.get(receipt['batch_id']);info['turns'][case['id']]=batch['turn_id']
            with store.connection(write=True) as db:db.execute("UPDATE turns SET status='interrupted',error_code='acceptance_pending' WHERE id=?",(batch['turn_id'],))
        save(setup,info)
        for case in corpus['cases']:
            if case['contract']=='old_version':
                v=info['versions'][case['character']];engine.service.use_persona(v['persona_id']);data=deepcopy(v['data']);data['appearance']['height_cm']=199
                chars.update(v['persona_id'],data,v['id'],identifier())
    # Model/cache preparation is explicit and local, prior to timed request retrieval.
    for v in info['versions'].values():index.build(v['persona_id'],v['id'])
    results_path=evidence/'results.json';rows=json.loads(results_path.read_text(encoding='utf-8')) if results_path.exists() else []
    completed={r['id'] for r in rows};consecutive=0
    for prior in reversed(rows):
        if prior.get('attempt') and prior['attempt']['status']!='completed':consecutive+=1
        else:break
    if consecutive>=5:raise RuntimeError('Five consecutive failures already recorded; await recovery')
    quota=QuotaService(store,cfg)
    with quota.acceptance():
        try:
            for case in corpus['cases']:
                if case['id'] in completed:continue
                tid=info['turns'][case['id']];v=info['versions'][case['character']]
                with store.connection() as db:
                    count=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0]
                    if count>=len(corpus['cases']):raise RuntimeError('call ceiling reached')
                    recorded=db.execute('SELECT * FROM character_selection_attempts WHERE turn_id=?',(tid,)).fetchone()
                    recorded=dict(recorded) if recorded else None
                    if recorded and (not reconcile or recorded['status']=='running'):raise RuntimeError('Unreported call exists; reconcile, never replay')
                raw=index.query(v['persona_id'],v['id'],case['members'],per_kind_limit=10)
                lexical,_=rank_candidates(v,case['members'],{},alg)
                local=candidates(v,case['members'],raw['by_kind'],alg)
                contract_ok=True;result=None;started=time.monotonic()
                if case['contract']=='foreign_version':
                    foreign=next(x for x in info['versions'].values() if x['persona_id']!=v['persona_id'])
                    with store.connection(write=True) as db:db.execute('UPDATE turns SET character_snapshot=? WHERE id=?',(json.dumps(foreign,ensure_ascii=False),tid))
                with store.connection(write=True) as db:db.execute("UPDATE turns SET status='queued',error_code=NULL WHERE id=?",(tid,))
                try:
                    if recorded:
                        if recorded['status']=='completed':
                            pool=runtime.local_pool(v,{'config':alg},case['members'])
                            assert pool['fingerprint']==recorded['candidate_hash']
                            result=apply_selection(v,case['members'],pool,json.loads(recorded['selected_json']),2)
                        else:
                            result=select_context(v,'\n\n'.join(case['members']),members=case['members'])
                            result.update(effective_mode='lexical',degrade_reason=recorded['error_code'])
                        result['budgets']={name:sum(len(p['text'])+1 for p in result['parts'] if p['kind'] in kinds) for name,kinds in {'anchor':('anchor',),'dynamic':('fact','lore_group'),'examples':('example',)}.items()}
                    else:result=await engine.selector.prepare(tid)
                except RuntimeErrorBase as exc:
                    if case['contract']!='foreign_version' or str(exc)!='character_snapshot_invalid':raise
                if result:
                    ids=result['lore_ids']+result['example_ids'];degraded=result['effective_mode']!='hybrid_llm' and result.get('degrade_reason')!='no_candidates'
                    contract_ok=len(result['text'])<=3000 and result['budgets']['dynamic']<=1600 and result['budgets']['examples']<=600
                    if case['contract'] in ('known_fact','old_version','unknown_fact'):
                        path='appearance.blood_type' if case['contract']=='unknown_fact' else 'appearance.height_cm';fact=read_fact(v,path)
                        contract_ok &= path in result['fact_paths'] and (fact['status']=='unset' if case['contract']=='unknown_fact' else fact['value']==172)
                else:ids=[];degraded=False
                check=selection_check(case,ids,degraded=degraded,contract_ok=contract_ok)
                attempts=engine.selector.list(v['persona_id'],tid);attempt=attempts[0] if attempts else None
                row={'id':case['id'],'character':case['character'],'group':case['group'],'kind':case['kind'],'turn_id':tid,'selected':ids,
                    'raw':ranking_metrics([r['item_id'] for r in raw['by_kind'].get(case['kind'],[])],case['required_ids']),
                    'local_candidates':ranking_metrics([r['item_id'] for r in local['ranked'].get(case['kind'],[])],case['required_ids']),
                    'lexical_candidates':ranking_metrics([r['item_id'] for r in lexical.get(case['kind'],[])],case['required_ids']),
                    'effective_mode':(result or {}).get('effective_mode'),'degrade_reason':(result or {}).get('degrade_reason'),
                    'selector_returned':(result or {}).get('selector_returned'),'reconciled_without_replay':bool(recorded),'elapsed_ms':round((recorded['finished_epoch']-recorded['started_epoch'])*1000,3) if recorded else round((time.monotonic()-started)*1000,3),
                    'attempt':attempt,'budgets':(result or {}).get('budgets'),**check}
                rows.append(row);save(results_path,rows)
                with store.connection(write=True) as db:db.execute("UPDATE turns SET status='interrupted',error_code='acceptance_selector_only_no_primary' WHERE id=?",(tid,))
                engine.selector.release(tid)
                consecutive=consecutive+1 if attempt and attempt['status']!='completed' else 0
                save(evidence/'summary.json',summarize(rows)|{'actual_selection_calls':0 if simulate else count+(1 if attempt and not recorded else 0),'primary_calls':0,'simulated':simulate})
                print(json.dumps({'case':case['id'],'passed':check['passed'],'degraded':row['degrade_reason'],'calls':count+(1 if attempt and not recorded else 0)},ensure_ascii=False),flush=True)
                if consecutive>=5:raise RuntimeError('Five consecutive model failures; saved checkpoint')
        finally:runtime.close()
    report=summarize(rows)
    with store.connection() as db:report['actual_selection_calls']=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0]
    report['primary_calls']=0;report['simulated']=simulate;
    if simulate:report['simulated_selection_attempts']=report['actual_selection_calls'];report['actual_selection_calls']=0
    save(evidence/'summary.json',report);print(json.dumps(report),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--execute',action='store_true');parser.add_argument('--simulate',action='store_true');parser.add_argument('--reconcile-recorded',action='store_true');args=parser.parse_args()
    asyncio.run(run(args.fixture,args.evidence,args.execute or args.simulate,args.simulate,args.reconcile_recorded))
