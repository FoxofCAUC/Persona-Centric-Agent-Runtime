"""Frozen artificial context cases; no provider, scoring model, QQ or formal DB."""
import argparse
import asyncio
import hashlib
import json
import statistics
from pathlib import Path
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.memory_policy import exclude_sources
from persona_runtime.models import generic_persona,timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_turn


class NeverModel:
    async def complete(self,*args):raise AssertionError('This evaluator must not call a model')


async def case_run(case,root):
    paths=RuntimePaths(root/case['id']);store=Store(paths);store.initialize()
    cfg=ConfigStore(paths);cfg.initialize();cfg.update({'model':{'name':'artificial','base_url':'http://127.0.0.1:1/v1'}})
    engine=ChatEngine(store,cfg,NeverModel());service=engine.service
    pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    sid=engine.session()['session_id'];ingress=IngressService(engine)
    family=case['family'];members=case['members'];key=case['id']+'-target'
    if family=='budget':members=[s*1000 for s in members]
    with store.connection(write=True) as db:
        seed_turn(db,pid,sid,key,members,reply='人工回复：已记录设定于此次虚构练习。')
        for n in range(52 if family=='explicit' else 0 if family=='recent' else 20):
            seed_turn(db,pid,sid,case['id']+f'-noise-{n}',[f'无关记录 filler_{n}'])
        if family=='withdrawn':exclude_sources(db,pid,[key+'-e2'],'artificial_withdrawal')
    if family=='scope':engine.session(fresh=True)
    variants=[]
    for mode in ('legacy_recent','working_set'):
        policy=ingress.current_policy(pid,'desktop')
        ingress.update_policy(pid,'desktop',policy['mode'],policy['id'],case['id']+mode,context_mode=mode,cross_session=case.get('cross_session',False))
        request={'text':case['query'],'request_id':case['id']+mode,'channel':'desktop'}
        if family=='explicit':request['reference']={'kind':'event','id':key+'-e3'}
        with patch.object(ingress,'schedule_committed'):receipt=ingress.accept(request)
        prepared=engine.cognitive.prepare(receipt['turn_id'])
        with store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(receipt['turn_id'],)).fetchone())
            prepared=engine.cognitive.finalize(db,turn,prepared)
        details=prepared['details'];text=json.dumps(prepared['messages'],ensure_ascii=False)
        present=all(value in text for value in case['needles'])
        expected=(family in ('recent','explicit') or (mode=='working_set' and family in ('old','nonfirst'))
                  or (mode=='working_set' and family=='scope' and case.get('cross_session',False)))
        if family in ('unknown','withdrawn','budget'):expected=False
        groups=details['history_groups'];ids=[g['turn_id'] for g in groups]
        valid=(present==expected and len(ids)==len(set(ids)) and len(ids)<=(5 if mode=='working_set' else 10)
               and details['input_characters']<=24000 and prepared['messages'][-1]['content'].find(case['query'])>=0)
        if family=='nonfirst' and present:
            valid=valid and all(s in text for s in case['members'])
        variants.append({'mode':mode,'passed':valid,'expected_fact_present':expected,'fact_present':present,
            'selected_turn_ids':ids,'characters':details['input_characters'],'removed':details['removed'],
            'retrieval':details['retrieval'],'preparation_ms':prepared['manifest']['preparation_ms'],
            'final_verification_ms':prepared['manifest']['final_verification_ms'],
            'input_snapshot_hash':fingerprint(prepared['messages'])})
        engine.cancel_queued(receipt['turn_id'])
    return {'id':case['id'],'family':family,'passed':all(v['passed'] for v in variants),'variants':variants}


async def run(args):
    fixture=args.fixture.resolve();out=args.output.resolve();repo=Path(__file__).resolve().parents[1]
    if out.is_relative_to(repo) or out.exists():raise ValueError('Use a new output folder outside the repository')
    out.mkdir(parents=True);data=json.loads(fixture.read_text(encoding='utf-8'));results=[]
    for case in data['cases']:results.append(await case_run(case,out/'artificial-runtime'))
    times=[v['preparation_ms'] for r in results for v in r['variants']]
    final=[v['final_verification_ms'] for r in results for v in r['variants']]
    retrieval=[v['retrieval']['retrieval_ms'] for r in results for v in r['variants']]
    overall=[v['preparation_ms']+v['final_verification_ms'] for r in results for v in r['variants']]
    percentile=lambda seq:sorted(seq)[min(len(seq)-1,int(len(seq)*.95))]
    report={'created_at':timestamp(),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'set':data['set'],'case_count':len(results),'passed':sum(r['passed'] for r in results),
        'provider_calls':0,'scoring_model_calls':0,'qq_sends':0,'synthetic_only':True,
        'performance_ms':{name:{'median':statistics.median(seq),'p95':percentile(seq),'max':max(seq)}
            for name,seq in (('retrieval',retrieval),('final_verification',final),('preparation',times),('overall_ready',overall))},'cases':results}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False))
    if report['passed']!=len(results):raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
