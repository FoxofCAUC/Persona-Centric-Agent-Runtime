"""100 local decision-plus-capsule samples on artificial maximum input."""
import argparse,json,platform,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.disposition import DispositionService,accepted_snapshot
from persona_runtime.disposition_compile import parse_output
from persona_runtime.disposition_shadow import ShadowService
from persona_runtime.decision_capsule import AssistAdmission,capsule,VERSION
from persona_runtime.models import generic_persona,identifier
from tests.m71_fixtures import seed_turn


def run(out):
    out=out.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'performance.json').exists():raise ValueError('Refuse to overwrite evidence')
    paths=RuntimePaths(out/'runtime');store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    sid=service.create_session()['id'];ds=DispositionService(store);source=next(s for s in ds.sources(pid)['sources'] if s['kind']=='expression')
    doc=parse_output(json.dumps({'schema_version':1,'definitions':[{'id':f'd{i:02d}','label':'人工支持偏好',
        'source_refs':[{'source_id':source['source_id'],'quote':source['text']}],'triggers':[],
        'preferences':{'support':2},'priority':i,'enabled':True} for i in range(32)]}),[source])
    h=ds.draft_update(pid,doc,0,identifier());ds.confirm(pid,h['draft_id'],None,h['revision'],identifier())
    grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((pid,)),out/'absent-formal')
    engine=SimpleNamespace(store=store,config=cfg,assist_admission=grant)
    members=[('我想找人倾诉'+('人工普通资料'*200))[:1200]]*20
    with store.connection(write=True) as db:
        seed_turn(db,pid,sid,'performance',members)
        snapshot=accepted_snapshot(db,pid,'assist');snapshot['renderer']=VERSION
        db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps({'disposition_mode':'assist','disposition':snapshot}),'performance'))
    with store.connection() as db:turn=dict(db.execute("SELECT * FROM turns WHERE id='performance'").fetchone())
    shadow=ShadowService(store,engine);samples=[];states=[]
    try:
        for _ in range(100):
            start=time.perf_counter();result=shadow.prepare(turn);text=capsule(result)
            samples.append((time.perf_counter()-start)*1000)
            states.append(result['status'])
            if result['status']=='observed' and not text:raise AssertionError('missing capsule')
            if result['status']!='observed' and text:raise AssertionError('late capsule')
    finally:shadow.close()
    ordered=sorted(samples);report={'samples':100,'members':20,'characters':sum(map(len,members)),'definitions':32,
        'p50_ms':round(ordered[49],3),'p95_ms':round(ordered[94],3),'max_ms':round(max(samples),3),
        'statuses':{s:states.count(s) for s in sorted(set(states))},'no_late_capsule':True,
        'pass':ordered[94]<=150 and states.count('observed')>=95,'python':platform.python_version(),'cloud_calls':0}
    atomic_json(out/'performance.json',report);print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);run(p.parse_args().output)
