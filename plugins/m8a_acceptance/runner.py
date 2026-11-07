"""Finite artificial tests through the normal server, Workbench and task worker."""
import argparse
import asyncio
from contextlib import ExitStack,closing
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from plugins.background_acceptance import runner as host
from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatCompletionsClient,ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import InstanceLock
from persona_runtime.models import generic_persona,identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import DEFAULT_HOME,RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION='m8a-acceptance-v1'
TEMP=DEFAULT_HOME/'tmp/pcar-m8a-acceptance'
ROOT=host.ROOT
read,write_new,require,sha=host.read,host.write_new,host.require,host.sha
POSITIONS={'R1':'manual_receipt_restart','R2':'daily','R3':'incremental','R4':'sealed','R5':'foreground_v8'}


def code_hashes():
    return host.code_hashes()|{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'plugins/m8a_acceptance').glob('*.py')}|{name:sha(ROOT/name) for name in ('tests/m71_fixtures.py','scripts/m8a_deploy.py','scripts/verify_m8a_migration.py')}


def verify(out):
    out=Path(out).resolve();owner=read(out/'owner.json');plan=read(out/'plan.json');home=Path(owner['home']).resolve()
    require(home.parent==TEMP.resolve(),'m8a_scope')
    require(read(home/'ownership.json')=={'version':VERSION,'out':str(out),'artificial_only':True},'m8a_owner')
    require(code_hashes()==owner['code_hashes'] and fingerprint(plan)==owner['plan_hash'],'m8a_freeze')
    require(all(Path(p).is_file() and sha(p)==h for p,h in owner['protected'].items()),'formal_changed')
    cfg=ConfigStore(RuntimePaths(home)).load()
    require(not cfg['bot_qq'] and not cfg['owner_qq'],'qq_forbidden')
    require(fingerprint(cfg['model'])==owner['model_hash'],'model_changed')
    return owner,plan


def prepare(out,simulated=False):
    out=Path(out).resolve();safe(out,DEFAULT_HOME/'exports/acceptance');require(not out.exists(),'already_prepared')
    private_directory(DEFAULT_HOME,out);home=TEMP/identifier();private_directory(DEFAULT_HOME,home)
    paths=RuntimePaths(home);store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    original=ConfigStore(RuntimePaths(DEFAULT_HOME)).load();value=cfg.load()
    for model in (original['model'],original['organization']['model']):
        require(model['name']=='deepseek-flash' and model['base_url'].rstrip('/') in ('https://api.deepseek.com','https://api.deepseek.com/v1'),'unapproved_model')
    value['model']=deepcopy(original['model']);value['organization']=deepcopy(original['organization'])
    value['organization'].update(daily_enabled=False,hourly_limit=6,daily_limit=24)
    if simulated:
        value['model']['encrypted_key']='';value['organization']['model']['encrypted_key']=''
    atomic_json(cfg.file,value)
    service=RuntimeService(store);roles={};sessions={};fixtures=[]
    chat=ChatEngine(store,cfg);org=OrganizationEngine(store,cfg,chat.client,chat)
    from tests.m71_fixtures import seed_turn
    for key in ('R1','R2','R3','R4'):
        pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        roles[key]=pid;sid=chat.session()['session_id'];sessions[key]=sid
        if key=='R3':
            org.schedule.set_automatic(True)
            policy=org.managed.current_policy();org.managed.update_policy(pid,dict(policy['data'],enabled=False),policy['id'],pid,'fixture-idle-paused')
        with store.connection(write=True) as db:
            for n in range(20 if key=='R3' else 1):
                key_id=f'm8a-fixture-{key}-{n}'
                text=f'人工测试设定：我给{key}号样品选择的标签是青竹，编号{n+1}。'
                seed_turn(db,pid,sid,key_id,[text],reply='人工历史占位回复，仅用于工程来源测试。')
                fixtures.append({'id':key_id,'role':key,'artificial':True,'not_model_generated':True})
    service.use_persona(roles['R1'])
    protected={str(p):sha(p) for folder in ('config','private','data/database') for p in (DEFAULT_HOME/folder).rglob('*') if p.is_file() and not p.name.endswith(('-shm','.lock'))}
    plan={'version':VERSION,'positions':POSITIONS,'simulated':simulated,'fixtures':fixtures,'restarts':1,'native_safe_retries':3,'no_qq':True,'real_cross_day':False}
    write_new(home/'ownership.json',{'version':VERSION,'out':str(out),'artificial_only':True})
    write_new(out/'plan.json',plan)
    write_new(out/'owner.json',{'home':str(home),'persona_id':roles['R1'],'roles':roles,'sessions':sessions,'code_hashes':code_hashes(),'plan_hash':fingerprint(plan),'protected':protected,'model_hash':fingerprint(value['model'])})
    return {'prepared':True,'simulated':simulated}


class RecordedClient:
    def __init__(self,out,simulated):self.out=Path(out);self.simulated=simulated;self.inner=ChatCompletionsClient()
    async def complete(self,model,messages):
        owner,plan=verify(self.out);organization=model.get('_json_output',False)
        key=read(self.out/'active.json')['id'] if organization else 'R5'
        require(key in POSITIONS and (organization or (self.out/'R5-admitted.json').exists()),'unplanned_position')
        require(not (self.out/'results'/(key+'.json')).exists(),'success_replay')
        calls=list((self.out/'calls'/key).glob('*.json'));require(len(calls)<4,'retry_limit')
        require(all(read(p)['status']=='failed' for p in calls),'unknown_replay')
        path=self.out/'calls'/key/(identifier()+'.json')
        row={'position':key,'kind':'organization' if organization else 'main','status':'dispatching','at':timestamp(),'messages_hash':fingerprint(messages)}
        write_new(path,row)
        async def response():
            if not self.simulated:return await self.inner.complete(model,messages)
            await asyncio.sleep(.1)
            if not organization:return '人工模拟回复：你好，测试进行顺利。',{'total_tokens':1,'mock':True}
            p=json.loads(messages[-1]['content']);event=next(e for e in p['events'] if e['role']=='user')
            typed='"format_version":2' in messages[0]['content'];source={'source_id' if typed else 'event_id':event['id'],'quote':event['text']}
            item={'text':event['text'],'sources':[source]}
            return json.dumps({'format_version':2 if typed else 1,'summary':([] if p['mode']=='candidates' else p['previous_summary']+[item]),'candidates':([] if p['mode']=='summary' else [dict(item,category='user_fact',conflicts=[])])},ensure_ascii=False),{'total_tokens':2,'mock':True}
        pending=asyncio.create_task(response())
        try:
            if key=='R2':
                # Request has started. Hold the response boundary until the normal
                # frontend has accepted R5; this neither edits state nor releases quota.
                for _ in range(600):
                    if (self.out/'R5-accepted.json').exists():break
                    await asyncio.sleep(.1)
                require((self.out/'R5-accepted.json').exists(),'foreground_not_accepted')
            result=await pending;row.update(status='completed',usage=result[1],reply_hash=fingerprint(result[0]));atomic_json(path,row);return result
        except BaseException as error:
            pending.cancel();await asyncio.gather(pending,return_exceptions=True)
            row.update(status='cancelled' if isinstance(error,asyncio.CancelledError) else 'failed',error=type(error).__name__,usage=getattr(error,'usage',None));atomic_json(path,row);raise


def snapshot(paths):
    with Store(paths).connection() as db:
        return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('turns','attempts','organization_attempts','organization_receipts','candidates','memories')}


def worker(out,opening):
    out=Path(out);owner,plan=verify(out);paths=RuntimePaths(Path(owner['home']));folder=out/'openings'/str(opening);folder.mkdir(parents=True,exist_ok=False)
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start();state={}
    from persona_runtime.server import create_app
    async def start():
        app=create_app(paths,RecordedClient(out,plan['simulated']));state['app']=app
        org=app['organization'];validate=org.validate_output
        def fault(db,payload,reply,audit=None):
            if payload['persona_id']==owner['roles']['R1'] and not (out/'receipt-crash.json').exists():
                write_new(out/'receipt-crash.json',{'at':timestamp(),'position':'R1','after_durable_receipt':True,'exit_code':73})
                os._exit(73)
            return validate(db,payload,reply,audit)
        org.validate_output=fault
        runner=web.AppRunner(app,access_log=None);state['runner']=runner;await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
        ConfigStore(paths).update({'service_port':port});return port
    with ExitStack() as stack:
        for name in ('service','desktop'):stack.enter_context(closing(InstanceLock(paths.home/'runtime'/(name+'.lock'))))
        window=None
        try:
            port=asyncio.run_coroutine_threadsafe(start(),loop).result(120)
            from persona_runtime.desktop import Workbench
            from experiments.companion_workbench_trial import OwnedController
            window=Workbench(paths,managed_lifecycle=False,controller=OwnedController(SimpleNamespace(running=True)));window.withdraw();window.persona_id=owner['persona_id']
            def control():
                if (folder/'close.request').exists():window.close_window()
                else:window.after(100,control)
            window.after(100,control)
            write_new(folder/'ready.json',{'pid':os.getpid(),'port':port,'at':timestamp(),'workbench_constructed':True,'withdrawn':True,'snapshot':snapshot(paths)})
            window.mainloop();window.hub.pool.shutdown(wait=True,cancel_futures=True)
        finally:
            if state.get('runner'):asyncio.run_coroutine_threadsafe(state['runner'].cleanup(),loop).result(120)
            loop.call_soon_threadsafe(loop.stop);thread.join(15);loop.close()
    write_new(folder/'closed.json',{'at':timestamp(),'tk_destroyed':bool(window and window.closed),'thread_stopped':not thread.is_alive(),'snapshot':snapshot(paths)})


def wait_task(client,tid):
    def check():
        row=client.manage('organization.show',task_id=tid)
        return row if row['status'] not in ('queued','running','waiting_quota') else None
    result=host.wait_until(check,1200);require(result['status']=='completed','organization_'+result['status']);return result


def execute(out):
    out=Path(out).resolve();owner,plan=verify(out);paths=RuntimePaths(Path(owner['home']))
    if not plan['simulated']:
        proofs=[read(p) for p in out.parent.glob('simulation-*/summary.json')]
        require(any(p['passed'] and p['simulated'] and p['code_hashes']==code_hashes() for p in proofs),'same_source_simulation_required')
    with closing(InstanceLock(out/'supervisor.lock')):
        write_new(out/'started.json',{'at':timestamp()});process=None;opening=1
        try:
            atomic_json(out/'active.json',{'id':'R1'})
            process,_=host.spawn_worker(out,opening,module='plugins.m8a_acceptance');client=LocalClient(ConfigStore(paths))
            p=client.manage('organization.preview',session_id=owner['sessions']['R1'],mode='candidates')
            intent={'preview':p,'request_id':'m8a-R1'};write_new(out/'intents/R1.json',intent)
            t=client.manage('organization.submit',**intent)
            host.wait_until(lambda:True if process.poll() is not None else None,1200)
            require(process.returncode==73 and (out/'receipt-crash.json').exists(),'receipt_crash_missing')
            before=snapshot(paths);require(before['organization_attempts']==before['organization_receipts']==1 and before['candidates']==0,'receipt_boundary')
            opening+=1;process,_=host.spawn_worker(out,opening,module='plugins.m8a_acceptance')
            r=wait_task(client,t['id']);require(len(r['attempts'])==1 and snapshot(paths)['organization_attempts']==1,'receipt_replayed')
            write_new(out/'results/R1.json',r)
            client.manage('persona.use',persona_id=owner['roles']['R2']);atomic_json(out/'active.json',{'id':'R2'})
            ConfigStore(paths).update({'organization':{'daily_enabled':True}})
            write_new(out/'intents/R2.json',{'action':'organization.daily-run-now'})
            client.manage('organization.daily-run-now')
            host.wait_until(lambda:True if list((out/'calls/R2').glob('*.json')) else None,120)
            write_new(out/'R5-admitted.json',{'at':timestamp(),'text':'你好，后台整理时我也来打个招呼。'})
            receipt=client.submit_input({'channel':'desktop','persona_id':owner['roles']['R2'],'request_id':'m8a-R5','text':read(out/'R5-admitted.json')['text']})
            write_new(out/'R5-accepted.json',{'at':timestamp(),'receipt':receipt})
            batch=client.call('GET','/v2/batches/'+receipt['batch_id'])
            if batch['state']=='collecting':client.call('POST','/v2/batches/'+batch['id']+'/seal',{'persona_id':owner['roles']['R2'],'expected_revision':batch['revision'],'operation_id':'m8a-R5-seal'})
            def main_done():
                b=client.call('GET','/v2/batches/'+batch['id'])
                if not b.get('turn_id'):return None
                turn=client.call('GET','/v1/requests/'+b['turn_id']);return turn if turn['status'] not in ('queued','running') else None
            r5=host.wait_until(main_done,1200);require(r5['status']=='completed','foreground_failed');write_new(out/'results/R5.json',r5)
            tasks=client.manage('organization.list');require(len(tasks)==1,'daily_duplicate');write_new(out/'results/R2.json',wait_task(client,tasks[0]['id']))
            ConfigStore(paths).update({'organization':{'daily_enabled':False}})
            client.manage('persona.use',persona_id=owner['roles']['R3']);atomic_json(out/'active.json',{'id':'R3'})
            policy=client.manage('organization.policy.current');write_new(out/'intents/R3.json',{'action':'resume_preconfigured_idle_policy'})
            client.manage('organization.policy.update',persona_id=owner['roles']['R3'],data=dict(policy['data'],enabled=True),expected_version=policy['id'],expected_selected=owner['roles']['R3'],operation_id='m8a-idle-enable')
            task=host.wait_until(lambda:next(iter(client.manage('organization.list')),None),240)
            write_new(out/'results/R3.json',wait_task(client,task['id']))
            client.manage('organization.automatic',enabled=False)
            client.manage('persona.use',persona_id=owner['roles']['R4']);atomic_json(out/'active.json',{'id':'R4'})
            sid=owner['sessions']['R4'];pid=owner['roles']['R4'];s=client.manage('session.show',session_id=sid,persona_id=pid)
            client.manage('session.seal',session_id=sid,persona_id=pid,expected_revision=s['revision'],operation_id='m8a-seal')
            p=client.manage('session.organization.preview',session_id=sid,persona_id=pid,mode='candidates')
            intent={'preview':p,'persona_id':pid,'expected_revision':p['lifecycle_revision'],'operation_id':'m8a-R4'};write_new(out/'intents/R4.json',intent)
            task=client.manage('session.organization.submit',**intent)
            duplicate=client.manage('session.organization.submit',**dict(intent,operation_id='m8a-R4-duplicate'))
            require(duplicate['id']==task['id'],'sealed_duplicate');write_new(out/'results/R4.json',wait_task(client,task['id']))
            calls=[read(p) for p in (out/'calls').glob('*/*.json')]
            require(set(c['position'] for c in calls)==set(POSITIONS),'position_coverage')
            for key in POSITIONS:
                physical=sorted((c for c in calls if c['position']==key),key=lambda c:c['at'])
                require(1<=len(physical)<=4 and physical[-1]['status']=='completed' and all(c['status']=='failed' for c in physical[:-1]),'physical_call_count')
            with Store(paths).connection() as db:
                require(db.execute("SELECT count(*) FROM organization_tasks WHERE status!='completed'").fetchone()[0]==0,'unsettled_tasks')
                require(db.execute('SELECT count(*) FROM organization_reservations').fetchone()[0]==sum(c['kind']=='organization' for c in calls),'reservation_count')
            write_new(out/'summary.json',{'passed':True,'simulated':plan['simulated'],'positions':list(POSITIONS),'physical_calls':len(calls),'reported_tokens':sum((c.get('usage') or {}).get('total_tokens',0) for c in calls),'unknown_usage':sum(not c.get('usage') for c in calls),'receipt_restart_without_replay':True,'computer_use':False,'code_hashes':code_hashes()})
        finally:
            if process and process.poll() is None:
                folder=out/'openings'/str(opening);write_new(folder/'close.request',{'at':timestamp()});require(process.wait(timeout=150)==0,'worker_close')
                require(read(folder/'closed.json')['tk_destroyed'],'window_not_closed')
    return read(out/'summary.json')|{'code_hashes':'recorded'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run','worker'));p.add_argument('--out',type=Path,required=True);p.add_argument('--simulated',action='store_true');p.add_argument('--opening',type=int,default=1);a=p.parse_args()
    if a.action=='worker':return worker(a.out,a.opening)
    result=prepare(a.out,a.simulated) if a.action=='prepare' else execute(a.out)
    print(json.dumps(result,ensure_ascii=False))
