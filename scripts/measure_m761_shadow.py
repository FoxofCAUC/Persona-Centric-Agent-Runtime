"""Artificial maximum-scale Shadow timing, without cloud calls or services."""
import argparse,json,platform,sys,time
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.archive import encoded
from persona_runtime.disposition import DispositionService,accepted_snapshot
from persona_runtime.disposition_compile import parse_output
from persona_runtime.disposition_shadow import ShadowService
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_turn

def run(out):
    out=out.resolve()
    if out.drive.upper()!='D:':raise ValueError('Explicit D drive evidence required')
    resultfile=out/'shadow-performance.json'
    if resultfile.exists():raise ValueError('Existing evidence is not overwritten')
    store=Store(RuntimePaths(out/'performance-runtime'));store.initialize();service=RuntimeService(store)
    pid=service.create_persona(generic_persona())['id'];service.use_persona(pid);sid=service.create_session()['id'];ds=DispositionService(store)
    source=next(s for s in ds.sources(pid)['sources'] if s['kind']=='expression')
    doc=parse_output(json.dumps({'schema_version':1,'definitions':[{'id':f'd{i:02d}','label':'人工倾听偏好','source_refs':[{'source_id':source['source_id'],'quote':source['text']}],'triggers':[],'preferences':{'support':2},'priority':i,'enabled':True} for i in range(32)]}),[source])
    h=ds.draft_update(pid,doc,0,identifier());ds.confirm(pid,h['draft_id'],None,h['revision'],identifier())
    text=('人工普通资料'*200)[:1200];members=[text]*20
    assert sum(map(len,members))==24000
    with store.connection(write=True) as db:
        seed_turn(db,pid,sid,'performance',members)
        db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(encoded({'disposition':accepted_snapshot(db,pid,'shadow')}),'performance'))
    with store.connection() as db:turn=dict(db.execute("SELECT * FROM turns WHERE id='performance'").fetchone())
    shadow=ShadowService(store);samples=[];checks=[];states=[]
    try:
        for i in range(100):
            start=time.perf_counter();result=shadow.prepare(turn);samples.append((time.perf_counter()-start)*1000);states.append(result['status'])
            start=time.perf_counter()
            with store.connection(write=True) as db:assert shadow.recheck(db,turn,result)['status']=='observed'
            checks.append((time.perf_counter()-start)*1000)
    finally:shadow.close()
    def stats(v):
        v=sorted(v);return {'p50_ms':round(v[49],3),'p95_ms':round(v[94],3),'max_ms':round(max(v),3)}
    report={'samples':100,'members':20,'characters':24000,'definitions':32,'prepare':stats(samples),'final_recheck':stats(checks),'observed':states.count('observed'),'pass':all(s=='observed' for s in states) and sorted(samples)[94]<=150,'python':platform.python_version(),'platform':platform.platform(),'cloud_calls':0}
    resultfile.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);run(p.parse_args().output)
