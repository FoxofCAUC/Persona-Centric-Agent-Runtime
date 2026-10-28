"""Loopback HTTP during a 1,000-turn seal; artificial model/platform only."""
import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.chat import ChatEngine
from persona_runtime.models import generic_persona,identifier
from persona_runtime.server import create_app
from tests.m71_fixtures import seed_turn
from tests.test_m73 import Model


async def check(output):
    with tempfile.TemporaryDirectory(prefix='m73-http-',dir=RuntimePaths.from_environment().home/'tmp') as d:
        paths=RuntimePaths(Path(d));store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
        cfg.update({'bot_qq':'12345','owner_qq':'67890','model':{'name':'fake','base_url':'http://127.0.0.1:1/v1'},'organization':{'daily_enabled':False}})
        service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        engine=ChatEngine(store,cfg);sid=engine.session()['session_id']
        with store.connection(write=True) as db:
            for n in range(1000):seed_turn(db,pid,sid,'long-'+str(n),['人工内容'+str(n),'补充成员'+str(n)])
        model=Model();app=create_app(paths,model);timings={};intervals={}
        async with TestServer(app) as server,ClientSession(headers={'Authorization':'Bearer '+cfg.token()}) as client:
            async def call(label,method,path,data=None):
                start=time.perf_counter()
                headers={'Authorization':'Bearer '+cfg.token('bridge')} if path=='/v2/ingress' else {}
                async with client.request(method,server.make_url(path),json=data,headers=headers) as r:
                    result=await r.json();assert r.status==200,(label,r.status,result)
                end=time.perf_counter();timings.setdefault(label,[]).append((end-start)*1000);intervals.setdefault(label,[]).append((start,end));return result
            payload={'channel':'qq','bot':'12345','sender':'67890','persona_id':pid,'request_id':'delivery-seed','platform_message_id':'offline-seed','text':'离线平台种子'}
            first=await call('seed','POST','/v2/ingress',payload);await app['engine'].wait()
            turn=app['engine'].get(first['turn_id']);batch=turn['reply_batch']['id']
            stream=await call('parts','GET','/v2/replies/'+batch+'/parts');part=stream['parts'][0]
            row=await call('status','POST','/v1/manage',{'action':'session.show','args':{'persona_id':pid,'session_id':sid}})
            seal=asyncio.create_task(call('seal','POST','/v1/manage',{'action':'session.seal','args':{'persona_id':pid,'session_id':sid,'expected_revision':row['revision'],'operation_id':'load-seal'}}))
            async def health():
                for _ in range(10):await call('health','GET','/v1/health');await asyncio.sleep(.01)
            async def delivery():
                base='/v2/replies/'+batch+'/parts/'+part['id']
                claim=await call('claim','POST',base+'/claim',{'expected_revision':stream['revision'],'operation_id':'load-claim'})
                await call('result','POST',base+'/result',{'expected_revision':claim['part_revision'],'operation_id':'load-result','claim_token':claim['claim_token'],'state':'sent','platform_message_id':'simulated-platform-id'})
            accepted=[]
            async def ingress():
                for n in range(3):
                    r=await call('ingress','POST','/v2/ingress',payload|{'request_id':'load-'+str(n),'platform_message_id':'load-'+str(n),'text':'人工并发输入'+str(n)});accepted.append(r)
            await asyncio.gather(health(),delivery(),ingress(),seal);await app['engine'].wait()
            assert seal.result()['state']=='sealed'
            with store.connection() as db:
                package=json.loads(db.execute('SELECT manifest_json FROM session_packages WHERE session_id=?',(sid,)).fetchone()[0]);assert len(package['turns'])==1000
                for receipt in accepted:assert db.execute('SELECT count(*) FROM ingress_receipts WHERE id=?',(receipt['receipt_id'],)).fetchone()[0]==1
            a,b=intervals['seal'][0]
            overlaps={k:any(start<b and end>a for start,end in intervals[k]) for k in ('health','claim','ingress')}
            assert all(overlaps.values()),overlaps
            report={'passed':True,'artificial_only':True,'turns':1000,'members':2000,'http_max_ms':{k:max(v) for k,v in timings.items()},'overlap_with_seal':overlaps,'accepted_inputs':len(accepted),'lost_inputs':0,'paid_calls':0,'real_qq_sends':0}
            atomic_json(output,report);print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Use private D-drive evidence')
    asyncio.run(check(a.output))
