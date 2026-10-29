"""Local-only 100/50 character performance. Separate from cloud acceptance."""
import argparse,json,sys,time,statistics,subprocess,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.character import CharacterService,empty_character,validate_character
from persona_runtime.character_embedding import CharacterEmbeddingProvider
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_retrieval import load_config
from persona_runtime.character_policy import choose
from persona_runtime.character_runtime import CharacterRuntime
from persona_runtime.models import generic_persona,identifier
from probe_m751 import peak_memory

def percentile(values,p):return sorted(values)[min(len(values)-1,int((len(values)-1)*p+.999999))]
def main(output,child=False):
    output.mkdir(parents=True,exist_ok=True);alg=load_config();home=RuntimePaths.from_environment();provider=CharacterEmbeddingProvider(home,home.home/'models/character-embedding'/alg['model_fingerprint'])
    if child:
        start=time.perf_counter();status=provider.diagnose();assert status['status']=='ready'
        print(json.dumps({'cold_ms':(time.perf_counter()-start)*1000,'peak_working_set_bytes':peak_memory()}));return
    target=output/'performance.json'
    if target.exists():raise RuntimeError('Existing performance evidence; do not rerun without reason')
    cold=[]
    for _ in range(3):
        response=subprocess.run([sys.executable,'-B',__file__,str(output),'--child'],capture_output=True,text=True,check=True,encoding='utf-8',env=os.environ.copy(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        cold.append(json.loads(response.stdout))
    paths=RuntimePaths(output/'runtime');store=Store(paths);store.initialize();svc=RuntimeService(store);pid=svc.create_persona(generic_persona())['id'];svc.use_persona(pid);chars=CharacterService(store)
    data=empty_character()
    for i in range(100):data['lorebook'].append({'id':'l'+str(i),'title':'人工场地'+str(i),'category':'place','keywords':['场地'+str(i)],'text':'第'+str(i)+'个人工场地摆放蓝色阅读椅，预算三百元，开放时间是周末。','priority':i%5,'enabled':True,'visibility':'conversation','dependencies':[]})
    for i in range(50):data['examples'].append({'id':'e'+str(i),'scenario':'人工情境'+str(i),'keywords':['情境'+str(i)],'text':'听到你分享这件小事，我也替你高兴。','enabled':True})
    validate_character(data);assert len(json.dumps(data,ensure_ascii=False).encode('utf-8'))<128*1024
    version=chars.update(pid,data,chars.current(pid)['id'],identifier());index=CharacterSemanticIndex(store,provider);rt=CharacterRuntime(store,index_factory=lambda _:index)
    start=time.perf_counter();index.build(pid,version['id']);build=(time.perf_counter()-start)*1000
    queries=[];verify=[];peak=0
    for n in range(100):
        members=['想找安静阅读的地方','我关心第'+str(n)+'个场地的椅子和预算']
        start=time.perf_counter();value=rt.local_pool(version,choose('hybrid_llm'),members);queries.append((time.perf_counter()-start)*1000)
        start=time.perf_counter()
        with store.connection() as conn:assert chars._version(conn,pid,version['id'])==version
        verify.append((time.perf_counter()-start)*1000);peak=max(peak,peak_memory())
    rt.close();result={'queries':100,'cold_samples':cold,'cold_p50_ms':statistics.median(r['cold_ms'] for r in cold),'cold_p95_ms':max(r['cold_ms'] for r in cold),'index_build_ms':build,'query_p50_ms':statistics.median(queries),'query_p95_ms':percentile(queries,.95),'verification_p50_ms':statistics.median(verify),'verification_p95_ms':percentile(verify,.95),'peak_working_set_bytes':peak,'index_bytes':sum(p.stat().st_size for p in (paths.home/'cache/character-semantic/v1').glob('*.sqlite3')),'passed':percentile(queries,.95)<=500,'cloud_calls':0}
    target.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--child',action='store_true');a=p.parse_args();main(a.output,a.child)
