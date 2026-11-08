"""Finite artificial M8.D chain through the normal managed Workbench service.

Only simulation may advance an injected quota clock. Real runs always keep
6/hour and 24/day and wait for the original ledger's window to expire.
"""
import argparse
import asyncio
from contextlib import ExitStack,closing
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

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

VERSION='m8d-acceptance-v1'
TEMP=DEFAULT_HOME/'tmp/pcar-m8d-acceptance'
ROOT=host.ROOT
read,write_new,require,sha=host.read,host.write_new,host.require,host.sha
FACTS=['我的寄存柜编号是南榆16。','我的读书会固定在星期三下午。','我逛旧书店的单次预算是三百元。','我的背包是白色帆布包。','我的笔记本放在松木抽屉里。','我喝乌龙茶时不加糖。','我常去的画廊叫西馆。','我养的那盆植物叫青豆。','我的闹钟设在08:42。','我的耳机放在蓝色盒子里。','我最近在学木刻。']
CORRECTION='更正一下，我的寄存柜编号是星港47，不是南榆16，以这次为准。另外背包上添了一个铜色铃铛。读书会是不是周五？我只是询问，没有更改原定时间。'
QUESTION='我的寄存柜编号现在是多少？读书会固定在什么时候？我的背包后来添了什么？'


def code_hashes():
    return host.code_hashes()|{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'plugins/m8d_acceptance').glob('*.py')}|{'tests/m71_fixtures.py':sha(ROOT/'tests/m71_fixtures.py')}


def verify(out):
    out=Path(out).resolve();owner=read(out/'owner.json');plan=read(out/'plan.json');home=Path(owner['home']).resolve()
    require(home.parent==TEMP.resolve(),'m8d_scope')
    require(read(home/'ownership.json')=={'version':VERSION,'out':str(out),'artificial_only':True},'m8d_owner')
    expected=owner['code_hashes']
    if (out/'continuation.json').exists():
        c=read(out/'continuation.json');require(c['original_owner_hash']==sha(out/'owner.json') and all(sha(out/p)==h for p,h in c['preserved'].items()),'prior_evidence_changed');expected=c['code_hashes']
    require(code_hashes()==expected and fingerprint(plan)==owner['plan_hash'],'m8d_freeze')
    require(all(Path(p).is_file() and sha(p)==h for p,h in owner['protected'].items()),'formal_changed')
    cfg=ConfigStore(RuntimePaths(home)).load()
    require(not cfg['bot_qq'] and not cfg['owner_qq'],'qq_forbidden')
    require(fingerprint(cfg['model'])==owner['model_hash'] and cfg['organization']['hourly_limit']==6 and cfg['organization']['daily_limit']==24,'configuration_changed')
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
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    chat=ChatEngine(store,cfg);sid=chat.session()['session_id']
    from tests.m71_fixtures import seed_turn
    with store.connection(write=True) as db:
        for n,text in enumerate(FACTS):seed_turn(db,pid,sid,'m8d-artificial-'+str(n),[text],reply='收到。')
    protected={str(p):sha(p) for folder in ('config','private','data/database') for p in (DEFAULT_HOME/folder).rglob('*') if p.is_file() and not p.name.endswith(('-shm','.lock'))}
    plan={'version':VERSION,'simulated':simulated,'positions':{'I1':4,'I2':2,'I3':2,'I4':2},'main_positions':['I2-chat','I4-recall'],'initialized_sources':11,'artificial_not_model_generated':True,'native_safe_retries':3,'hourly':6,'daily':24,'main_physical_max':8,'no_qq':True,'no_private_sources':True,'computer_use':False,'real_cross_day':False}
    write_new(home/'ownership.json',{'version':VERSION,'out':str(out),'artificial_only':True});write_new(out/'plan.json',plan)
    write_new(out/'owner.json',{'home':str(home),'persona_id':pid,'session_id':sid,'code_hashes':code_hashes(),'plan_hash':fingerprint(plan),'protected':protected,'model_hash':fingerprint(value['model'])})
    return {'prepared':True,'simulated':simulated}


class RecordedClient:
    def __init__(self,out):self.out=Path(out);self.inner=ChatCompletionsClient()
    async def complete(self,model,messages):
        owner,plan=verify(self.out);organization=model.get('_json_output',False);paths=RuntimePaths(Path(owner['home']));task=None
        if organization:
            with Store(paths).connection() as db:
                rows=db.execute("SELECT * FROM organization_tasks WHERE status='running'").fetchall();require(len(rows)==1,'execution_authority')
                task=dict(rows[0]);p=json.loads(task['payload_json']);a=p['automatic_memory']
                if a['purpose']=='repair':
                    fragment=db.execute('SELECT data_json FROM memory_fragments WHERE id=?',(a['previous_fragment'],)).fetchone()
                    reason=json.loads(fragment[0]).get('invalidation_reason');key='I3' if reason=='fact_revised' else 'I4'
                else:key='I1' if all(b['turn_id'].startswith('m8d-artificial-') for b in p['bindings']) else 'I2'
                logical=task['id'];stage=a['stage']
        else:key=read(self.out/'main-position.json')['id'];logical=key;stage='main'
        require(key in plan['positions'] or key in plan['main_positions'],'unplanned_position')
        calls=[read(p) for p in (self.out/'calls').glob('*.json')]
        same=[c for c in calls if c['logical_id']==logical];require(len(same)<4 and all(c['status']=='failed_safe' for c in same),'success_or_unknown_replay')
        orgcalls=[c for c in calls if c['organization']];require(len(orgcalls)<24 if organization else sum(not c['organization'] for c in calls)<8,'physical_budget')
        require(len({c['logical_id'] for c in calls if c['position']==key}|{logical})<=plan['positions'].get(key,1),'logical_budget')
        path=self.out/'calls'/(identifier()+'.json');row={'position':key,'logical_id':logical,'stage':stage,'organization':organization,'status':'dispatching','at':timestamp(),'messages_hash':fingerprint(messages)}
        if plan['simulated']:write_new(path,row)
        else:
            from .budget import record_dispatch
            record_dispatch(self.out.parent,path,row)
        try:
            if plan['simulated']:result=self.simulated(messages,organization)
            else:result=await self.inner.complete(model,messages)
            row.update(status='completed',usage=result[1],reply_hash=fingerprint(result[0]));atomic_json(path,row);return result
        except BaseException as error:
            diag=getattr(error,'diagnostics',{}) or {};safe_retry=diag.get('request_headers_sent') is False or diag.get('http_status')==429
            row.update(status='failed_safe' if safe_retry else 'unknown',error=type(error).__name__,usage=getattr(error,'usage',None));atomic_json(path,row);raise

    @staticmethod
    def simulated(messages,organization):
        if not organization:return '寄存柜是星港47，读书会是星期三下午，背包添了铜色铃铛。',{'total_tokens':1,'mock':True}
        p=json.loads(messages[-1]['content']);events={e['id']:e for e in p['events']};summary=[];candidates=[]
        if 'proposals' not in p:
            for n,e in enumerate(p['events']):
                if e['role']!='user':continue
                # A simulation oracle exercises control flow, never counts as
                # independent real factual validation.
                text=e['text'];slot='柜号' if '寄存柜' in text else 'fact-'+e['id'];out='我的寄存柜编号是星港47。' if '更正一下' in text else text
                source=[{'source_id':e['id'],'quote':text}]
                if not any(o['text']==out for o in p['outdated_claims']):summary.append({'id':'s'+str(n),'text':out,'sources':source})
                if p['purpose']!='repair':candidates.append({'id':'c'+str(n),'text':out,'subject':'用户','attribute':slot,'category':'user_fact','sources':source})
            return json.dumps({'format_version':3,'summary':summary,'candidates':candidates},ensure_ascii=False),{'total_tokens':2,'mock':True}
        decisions=[]
        for kind in ('summary','candidates'):
            for i in p['proposals'][kind]:
                target=next((m for m in p['existing_memories'] if m.get('attribute')==i.get('attribute') and m['text']!=i['text']),None)
                d={'id':i['id'],'action':'summary' if kind=='summary' else 'revise' if target else 'publish','reason':'simulation','subject':i.get('subject','用户'),'attribute':i.get('attribute','summary'),'target_memory_id':target['id'] if target and kind=='candidates' else None,'effective_at':None,'valid_until':None,'sources':[{'source_id':s['source_id'],'quote':events[s['source_id']]['text'][s['start']:s['end']]} for s in i['sources']]}
                decisions.append(d)
        return json.dumps({'format_version':3,'decisions':decisions},ensure_ascii=False),{'total_tokens':3,'mock':True}


def snapshot(paths):
    with Store(paths).connection() as db:return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('turns','attempts','organization_attempts','organization_receipts','memories','memory_batches','memory_decisions')}


def worker(out,opening):
    out=Path(out);owner,plan=verify(out);paths=RuntimePaths(Path(owner['home']));folder=out/'openings'/str(opening);folder.mkdir(parents=True,exist_ok=False)
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start();state={}
    from persona_runtime.server import create_app
    async def start():
        app=create_app(paths,RecordedClient(out));state['app']=app;org=app['organization'];commit=org.automatic.commit
        def fault(db,batch,payload,proposals,decisions):
            if payload['automatic_memory']['stage']=='verify' and not (out/'receipt-crash.json').exists():
                write_new(out/'receipt-crash.json',{'at':timestamp(),'after_durable_review_receipt':True,'pid':os.getpid(),'exit_code':73});os._exit(73)
            return commit(db,batch,payload,proposals,decisions)
        org.automatic.commit=fault
        if plan['simulated']:org.managed.clock=lambda:time.time()+(read(out/'simulation-clock.json')['offset'] if (out/'simulation-clock.json').exists() else 0)
        runner=web.AppRunner(app,access_log=None);state['runner']=runner;await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1];ConfigStore(paths).update({'service_port':port});return port
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
            window.after(100,control);write_new(folder/'ready.json',{'pid':os.getpid(),'port':port,'at':timestamp(),'workbench_constructed':True,'withdrawn':True,'snapshot':snapshot(paths)})
            window.mainloop();window.hub.pool.shutdown(wait=True,cancel_futures=True)
        finally:
            if state.get('runner'):asyncio.run_coroutine_threadsafe(state['runner'].cleanup(),loop).result(120)
            loop.call_soon_threadsafe(loop.stop);thread.join(15);loop.close()
    write_new(folder/'closed.json',{'at':timestamp(),'tk_destroyed':bool(window and window.closed),'thread_stopped':not thread.is_alive(),'snapshot':snapshot(paths)})


def seal(client,pid,sid,op):
    s=client.manage('session.show',session_id=sid,persona_id=pid)
    if s['state']!='sealed':client.manage('session.seal',session_id=sid,persona_id=pid,expected_revision=s['revision'],operation_id=op)


def chat(client,out,pid,key,text):
    atomic_json(out/'main-position.json',{'id':key});write_new(out/'intents'/(key+'.json'),{'text':text,'at':timestamp()})
    r=client.submit_input({'channel':'desktop','persona_id':pid,'request_id':'m8d-'+key,'text':text});b=client.call('GET','/v2/batches/'+r['batch_id'])
    if b['state']=='collecting':client.call('POST','/v2/batches/'+b['id']+'/seal',{'persona_id':pid,'expected_revision':b['revision'],'operation_id':'m8d-seal-'+key})
    def done():
        batch=client.call('GET','/v2/batches/'+b['id'])
        if not batch.get('turn_id'):return None
        t=client.call('GET','/v1/requests/'+batch['turn_id']);return t if t['status'] not in ('queued','running') else None
    t=host.wait_until(done,1200);require(t['status']=='completed','main_failed');write_new(out/'results'/(key+'.json'),t);return t


def wait_coverage(out,client,predicate,timeout=7500):
    owner,plan=verify(out)
    def check():
        c=client.manage('organization.coverage');atomic_json(out/'progress.json',{'at':timestamp(),'coverage':c,'quota':client.manage('quota.current')})
        if predicate(c):return c
        tasks=client.manage('organization.list')
        bad=[t for t in tasks if t['status'] not in ('completed','queued','running','waiting_quota')];require(not bad,'blocked_'+str([(t['status'],t.get('error_code')) for t in bad]))
        if plan['simulated'] and any(t['status']=='waiting_quota' for t in tasks):
            old=read(out/'simulation-clock.json')['offset'] if (out/'simulation-clock.json').exists() else 0;atomic_json(out/'simulation-clock.json',{'offset':old+3601,'offline_only':True})
        time.sleep(1);return None
    return host.wait_until(check,timeout)


def continuation(out):
    """Freeze an observer correction; never edits task data or replays a call."""
    out=Path(out).resolve();owner=read(out/'owner.json');home=Path(owner['home']).resolve()
    require(home.parent==TEMP.resolve() and read(out/'openings/2/closed.json')['thread_stopped'],'continuation_scope')
    require(not (out/'continuation.json').exists(),'continuation_exists')
    require(all(Path(p).is_file() and sha(p)==h for p,h in owner['protected'].items()),'formal_changed')
    with Store(RuntimePaths(home)).connection() as db:
        require(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]==4 and not db.execute("SELECT 1 FROM organization_tasks WHERE status!='completed'").fetchone(),'continuation_not_I1_boundary')
    files=[out/'owner.json',out/'plan.json',out/'source.zip',out/'restart-boundary.json']+list((out/'calls').glob('*.json'))
    write_new(out/'continuation.json',{'at':timestamp(),'reason':'I1 published all eleven facts; independent review omitted duplicate summaries. Permit planned NO_ACTION repair positions without overriding review or regenerating. Original observer stopped before I2.','original_owner_hash':sha(out/'owner.json'),'preserved':{p.relative_to(out).as_posix():sha(p) for p in files},'code_hashes':code_hashes(),'new_paid_positions':0,'completed_calls_replayed':False})
    import zipfile
    with zipfile.ZipFile(out/'continuation-source.zip','x',zipfile.ZIP_DEFLATED) as z:
        for name in code_hashes():z.write(ROOT/name,name)
    return execute(out,resume=True)


def execute(out,resume=False):
    out=Path(out).resolve();owner,plan=verify(out);paths=RuntimePaths(Path(owner['home']));pid=owner['persona_id'];process=None;opening=1
    if not plan['simulated']:
        require(any(read(p).get('passed') and read(p).get('simulated') and read(p)['code_hashes']==code_hashes() for p in out.parent.glob('simulation-*/summary.json')),'same_source_simulation_required')
    with closing(InstanceLock(out/'supervisor.lock')):
        if not resume:write_new(out/'started.json',{'at':timestamp()})
        try:
            if resume:opening=3
            process,_=host.spawn_worker(out,opening,module='plugins.m8d_acceptance');client=LocalClient(ConfigStore(paths))
            if not resume:
                seal(client,pid,owner['session_id'],'m8d-I1-seal');p=client.manage('organization.policy.current')
                client.manage('organization.policy.update',persona_id=pid,data=dict(p['data'],memory_automation=True),expected_version=p['id'],expected_selected=pid,operation_id='m8d-once-enable')
                host.wait_until(lambda:True if process.poll() is not None else None,1200);require(process.returncode==73,'review_crash_missing')
                before=snapshot(paths);require(before['organization_attempts']==before['organization_receipts']==2 and before['memories']==0,'review_receipt_boundary')
                write_new(out/'restart-boundary.json',before);opening=2;process,_=host.spawn_worker(out,opening,module='plugins.m8d_acceptance')
            c=wait_coverage(out,client,lambda c:c['processed_turns']==11)
            memories=client.manage('memory.search');require(any('南榆16' in m['text'] for m in memories),'positive_fact_missing')
            ConfigStore(paths).update({'organization':{'daily_enabled':True}})
            client.manage('organization.daily-run-now');time.sleep(2)
            ConfigStore(paths).update({'organization':{'daily_enabled':False}})
            require(snapshot(paths)['organization_attempts']==4,'I1_duplicate');write_new(out/'results/I1.json',c)
            t=chat(client,out,pid,'I2-chat',CORRECTION);seal(client,pid,t['session_id'],'m8d-I2-seal')
            c=wait_coverage(out,client,lambda c:c['processed_turns']>=12)
            memories=client.manage('memory.search');require(any('星港47' in m['text'] and m['revision']>1 for m in memories) and not any('南榆16' in m['text'] for m in memories),'correction_missing')
            require(any('星期三' in m['text'] or '周三' in m['text'] for m in memories),'independent_fact_lost');write_new(out/'results/I2.json',c)
            def repaired(c):
                with Store(paths).connection() as db:return db.execute("SELECT count(*) FROM memory_fragments WHERE state='invalid'").fetchone()[0]==0 and any(b['purpose']=='repair' and b['state']=='completed' for b in c['batches'])
            with Store(paths).connection() as db:need_repair=bool(db.execute("SELECT 1 FROM memory_fragments WHERE state='invalid'").fetchone())
            if need_repair:c=wait_coverage(out,client,repaired)
            write_new(out/'results/I3.json',c if need_repair else {'state':'NO_ACTION','reason':'no_invalid_summary_dependency','coverage':c})
            withdrawal=next((m for m in client.manage('memory.search') if '三百' in m['text'] or '300' in m['text']),None);require(withdrawal,'withdrawal_positive_missing')
            write_new(out/'intents/I4.json',{'action':'memory.retract','memory_id':withdrawal['id']});client.manage('memory.retract',memory_id=withdrawal['id'])
            with Store(paths).connection() as db:
                need_withdrawal_repair=bool(db.execute("SELECT 1 FROM memory_fragments WHERE state='invalid'").fetchone());before_repairs=db.execute("SELECT count(*) FROM memory_batches WHERE purpose='repair' AND state='completed'").fetchone()[0]
            if need_withdrawal_repair:c=wait_coverage(out,client,lambda c:repaired(c) and sum(b['purpose']=='repair' and b['state']=='completed' for b in c['batches'])>before_repairs)
            require(not any(m['id']==withdrawal['id'] for m in client.manage('memory.search')),'withdrawn_fact_returned')
            write_new(out/'results/I4.json',c if need_withdrawal_repair else {'state':'NO_ACTION_REPAIR','withdrawal_verified':True,'reason':'no_invalid_summary_dependency','coverage':client.manage('organization.coverage')})
            r=chat(client,out,pid,'I4-recall',QUESTION)
            with Store(paths).connection() as db:
                event=db.execute('SELECT text FROM events WHERE id=?',(r['reply_event_id'],)).fetchone()[0]
                require('星港47' in event and '南榆16' not in event,'recall_answer_wrong')
                uses=[json.loads(x[0]) for x in db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(r['id'],))]
                require(any(m.get('revision',0)>1 for u in uses for m in u.get('memories',[])),'recall_manifest_missing')
                require(r['session_id']!=t['session_id'],'recall_not_clean_session')
            # Stop further sources at the ordinary versioned policy boundary.
            p=client.manage('organization.policy.current');client.manage('organization.policy.update',persona_id=pid,data=dict(p['data'],memory_automation=False),expected_version=p['id'],expected_selected=pid,operation_id='m8d-acceptance-finished')
            calls=[read(p) for p in (out/'calls').glob('*.json')];require(len([c for c in calls if c['organization']])<=24,'daily_budget')
            write_new(out/'summary.json',{'passed':True,'simulated':plan['simulated'],'code_hashes':code_hashes(),'positions':['I1','I2','I3','I4'],'physical_org':sum(c['organization'] for c in calls),'physical_main':sum(not c['organization'] for c in calls),'reported_tokens':sum((c.get('usage') or {}).get('total_tokens',0) for c in calls),'unknown_usage':sum(not c.get('usage') for c in calls),'saved_review_recovered_without_replay':True,'clean_session_recall':True,'computer_use':False,'controlled_clock_only_in_simulation':True})
        finally:
            if process and process.poll() is None:
                folder=out/'openings'/str(opening);write_new(folder/'close.request',{'at':timestamp()});require(process.wait(timeout=150)==0,'worker_close');require(read(folder/'closed.json')['tk_destroyed'],'window_not_closed')
    return read(out/'summary.json')|{'code_hashes':'recorded'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run','continue','worker'));p.add_argument('--out',type=Path,required=True);p.add_argument('--simulated',action='store_true');p.add_argument('--opening',type=int,default=1);a=p.parse_args()
    if a.action=='worker':return worker(a.out,a.opening)
    print(json.dumps(prepare(a.out,a.simulated) if a.action=='prepare' else continuation(a.out) if a.action=='continue' else execute(a.out),ensure_ascii=False))
