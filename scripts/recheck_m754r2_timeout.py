"""One explicit timeout-only recheck after user raised limit to 60s. No replay of successes."""
import asyncio,json,sys,time
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
from persona_runtime.character_retrieval import load_config
from persona_runtime.quotas import QuotaService
from character_evaluation import selection_check
from evaluate_m754r2 import summarize

async def run(evidence):
    old=evidence/'development';rows=json.loads((old/'results.json').read_text(encoding='utf-8'))
    corpus=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/m754r2_development.json').read_text(encoding='utf-8'))
    cases={c['id']:c for c in corpus['cases']};failed=[r for r in rows if r['degrade_reason']=='selector_timeout']
    out=evidence/'timeout-60s-recheck';out.mkdir(exist_ok=True)
    freeze={'source_results_hash':file_hash(old/'results.json'),'case_ids':[r['id'] for r in failed],'timeout':60,'maximum_calls':18,'reason':'User raised timeout ceiling; preserve 8s failures'}
    assert len(failed)<=18
    if (out/'freeze.json').exists():assert json.loads((out/'freeze.json').read_text())==freeze
    else:atomic_json(out/'freeze.json',freeze)
    paths=RuntimePaths(old/'runtime');store=Store(paths);cfg=ConfigStore(paths);cfg.update({'character_selector':{'timeout':60}})
    engine=ChatEngine(store,cfg);rt=engine.cognitive.characters;rt.notify=lambda *_:[];store.character_committed=rt.notify
    home=RuntimePaths.from_environment();alg=load_config();index=CharacterSemanticIndex(store,CharacterEmbeddingProvider(home,home.home/'models/character-embedding'/alg['model_fingerprint']));rt.index_factory=lambda _:index
    result_path=out/'results.json';rechecks=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else []
    completed={r['id'] for r in rechecks};consecutive=0
    for r in reversed(rechecks):
        if r.get('attempt') and r['attempt']['status']!='completed':consecutive+=1
        else:break
    if consecutive>=5:raise RuntimeError('Five failures already reached')
    with QuotaService(store,cfg).acceptance():
        try:
            for prior in failed:
                if prior['id'] in completed:continue
                case=cases[prior['id']];tid=prior['turn_id']
                with store.connection() as db:
                    if db.execute('SELECT count(*) FROM character_selection_attempts WHERE turn_id=?',(tid,)).fetchone()[0]!=1:raise RuntimeError('Unreported repair exists; reconcile without replay')
                with patch.object(engine,'_schedule'):await engine.retry(tid)
                with store.connection() as db:turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                v=json.loads(turn['character_snapshot']);index.build(v['persona_id'],v['id']);assert index.provider.diagnose()['status']=='ready';start=time.monotonic()
                value=await engine.selector.prepare(tid);ids=value['lore_ids']+value['example_ids']
                ok=len(value['text'])<=3000 and value['budgets']['dynamic']<=1600 and value['budgets']['examples']<=600
                check=selection_check(case,ids,degraded=value['effective_mode']!='hybrid_llm',contract_ok=ok)
                attempt=engine.selector.list(v['persona_id'],tid)[0]
                recheck=prior|{'selected':ids,'degrade_reason':value['degrade_reason'],'effective_mode':value['effective_mode'],'selector_returned':value.get('selector_returned'),'attempt':attempt,'elapsed_ms':round((time.monotonic()-start)*1000,3),'recheck_of':prior['attempt']['id'],'timeout':60,**check}
                rechecks.append(recheck);atomic_json(result_path,rechecks)
                with store.connection(write=True) as db:db.execute("UPDATE turns SET status='interrupted',error_code='acceptance_selector_only_no_primary' WHERE id=?",(tid,))
                engine.selector.release(tid);consecutive=consecutive+1 if attempt['status']!='completed' else 0
                print(json.dumps({'case':case['id'],'passed':check['passed'],'reason':value['degrade_reason'],'rechecks':len(rechecks)}),flush=True)
                if consecutive>=5:raise RuntimeError('Five consecutive failures')
        finally:rt.close()
    replacements={r['id']:r for r in rechecks};combined=[replacements.get(r['id'],r) for r in rows]
    report=summarize(combined)|{'initial_selector_calls':63,'repair_selector_calls':len(rechecks),'primary_calls':0,'original_failures_preserved':True,'mixed_timeout_evidence':True}
    atomic_json(out/'combined-results.json',combined);atomic_json(out/'summary.json',report);print(json.dumps(report),flush=True)

if __name__=='__main__':asyncio.run(run(Path(sys.argv[1])))
