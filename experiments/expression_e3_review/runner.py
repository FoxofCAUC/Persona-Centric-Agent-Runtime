"""在隔离实例执行有限全文对照；供应商请求和只读报告分开。"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import time

from persona_runtime.archive import scope
from persona_runtime.chat import ChatCompletionsClient,ChatEngine,ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.expression_assets import ExpressionAssets
from persona_runtime.expression_publication import ExpressionPublication
from persona_runtime.expression_runtime import ExpressionRuntime
from persona_runtime.expression_transport import ExpressionHTTPClient
from persona_runtime.instance import InstanceLock
from persona_runtime.models import PersonaSpec,generic_persona,identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.security import protect,unprotect
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

from .common import load,prepare,read,reserve,sha,write_once


def credentials(home):
    config=ConfigStore(RuntimePaths(home));main=config.load()['model']
    router=home/'private/knowledge-router/config.json';jev=read(router)
    if not main['encrypted_key'] or not jev['encrypted_key']:raise ModelError('review_credentials_missing')
    return {'main':main['encrypted_key'],'jev':jev['encrypted_key'],
        'jev_public':{k:jev[k] for k in ('base_url','name')},
        'files':{str(p):sha(p.read_bytes()) for p in (config.file,router)}}


def error_code(error):
    value=str(error)
    return value if re.fullmatch(r'[a-zA-Z0-9_]{1,100}',value) else type(error).__name__


class Calls:
    def __init__(self,root,plan,store,creds,overrides=None):
        self.root,self.plan,self.store,self.creds=root,plan,store,creds
        self.active=None;self.overrides=overrides or {};self.consecutive_errors=0
        self.main,self.expression=ChatCompletionsClient(),ExpressionHTTPClient()

    async def perform(self,purpose,model,inputs,*,save_raw=None):
        slot=self.active
        if self.consecutive_errors>=self.plan['consecutive_transport_errors_stop']:
            raise ModelError('review_transport_error_stop',diagnostics={'request_headers_sent':False})
        if purpose=='expression':
            turn_id=read(self.root/'positions'/slot/'turn.json')['turn_id']
            with self.store.connection() as db:
                saved=db.execute('SELECT valid FROM expression_main_receipts WHERE turn_id=?',(turn_id,)).fetchone()
                if not saved or not saved['valid']:raise ModelError('review_main_receipt_required')
        request={'model':{k:v for k,v in model.items() if k!='encrypted_key'},'input':inputs}
        try:record=reserve(self.root,self.plan,slot,purpose,request)
        except ValueError as error:
            raise ModelError(error_code(error),diagnostics={'request_headers_sent':False,'response_headers_received':False}) from None
        folder=self.root/'positions'/slot/'calls';started=time.monotonic()
        try:
            live=model|{'encrypted_key':self.creds['jev' if purpose=='jev' else 'main']}
            if purpose in self.overrides:
                value=await self.overrides[purpose](live,inputs,save_raw=save_raw)
            elif purpose=='jev':
                from persona_runtime.knowledge_web_decision import execute
                value=await execute(live,inputs)
            elif purpose=='tool_search':
                from persona_runtime.knowledge_web_transport import request as tool_request
                value=await tool_request('search',inputs,live)
            elif purpose=='expression':
                def received(raw):
                    target=folder/'expression-first-http-response.json'
                    if target.exists():
                        if target.read_bytes()!=raw:raise ModelError('review_raw_response_conflict')
                    else:
                        with target.open('xb') as file:file.write(raw)
                    save_raw(raw)
                value=await self.expression.complete(live,inputs,save_raw=received)
            else:value=await self.main.complete(live,inputs)
            if purpose=='expression':record['diagnostics']=value['diagnostics']
            else:
                record.update(usage=value[1],diagnostics=getattr(value,'diagnostics',{}))
                write_once(folder/(purpose+'.first-receipt.json'),{'raw_text':value[0],'usage':value[1],
                    'diagnostics':getattr(value,'diagnostics',{}),'native':getattr(value,'native',None)})
            record['state']='returned';self.consecutive_errors=0
            return value
        except BaseException as error:
            record.update(state='error',error_code=error_code(error),diagnostics=getattr(error,'diagnostics',{}),usage=getattr(error,'usage',None))
            self.consecutive_errors+=1
            raise
        finally:
            record['elapsed_ms']=round((time.monotonic()-started)*1000,3)
            write_once(folder/(purpose+'.outcome.json'),record)

    def client(self,purpose):
        owner=self
        class Client:
            async def complete(self,model,messages,**kwargs):
                chosen='notice' if purpose=='primary' and '_search_notice_profile' in model else purpose
                return await owner.perform(chosen,model,messages,**kwargs)
        return Client()


def seed_group(root,group,plan,service,runtime):
    path=root/'groups'/group['id']/'binding.json'
    if path.exists():return read(path)
    spec=generic_persona().to_dict();spec.update(plan['fixtures']['personas'][group['persona']],initial_background='和对方从这次聊天开始认识。')
    pid=service.create_persona(PersonaSpec.from_dict(spec))['id'];service.use_persona(pid)
    runtime.configure(pid,{'enabled':True},0,identifier());assets=ExpressionAssets(runtime.store)
    assets.configure(pid,{'enabled':True},0,identifier());selected=scope(pid,'desktop')
    for item in plan['fixtures']['asset_templates']:
        source=assets.register_source(pid,selected,kind='authored_examples',parts=[{'kind':'text','text':item['situation']+'：'+item['style']}],attribution='E3 authored public synthetic review',operation_id=identifier())
        assets.put(pid,selected,situation=item['situation'],style=item['style'],keywords=item['keywords'],
            source_refs=[{'source_id':source['id'],'revision':1}],operation_id=identifier())
    binding={'persona_id':pid,'role':group['persona']};write_once(path,binding);return binding


def settle_simulation(root,turn,publisher,pid):
    folder=root/'positions'/turn['position'];plan=publisher.plan(pid,turn['session_id'],turn['turn_id'])
    if not plan:return
    args=(pid,turn['session_id'],turn['turn_id']);presenter='E3-expanded-headless-simulation'
    for item in plan['items']:
        journal=folder/'simulation'/(str(item['ordinal'])+'.json')
        if item['state']=='presented':continue
        if journal.exists():offer=read(journal)
        elif item['state']=='pending':
            offer=publisher.offer(*args,plan['id'],plan['revision'],item['ordinal'],presenter)
            if offer['state']=='plan_changed':return settle_simulation(root,turn,publisher,pid)
            if offer['state']!='offered':return
            write_once(journal,offer|{'simulation':True})
        else:raise ModelError('review_simulated_receipt_requires_reconciliation')
        publisher.acknowledge(*args,offer['id'],offer['lease_id'],presenter,'E3-SIM-'+offer['id'],offer['body_hash'])


async def execute(root,*,creds,overrides=None):
    plan=load(root);paths=RuntimePaths(root/'instance');cfg=ConfigStore(paths);cfg.initialize()
    cfg.update({'model':{'base_url':'https://api.deepseek.com','name':plan['model'],'max_tokens':8192,'max_input_chars':60000,'timeout':120},
                'organization':{'reuse_chat':True,'daily_enabled':False}})
    store=Store(paths);store.initialize();service=RuntimeService(store)
    calls=Calls(root,plan,store,creds,overrides);runtime=ExpressionRuntime(store,calls.client('expression'))
    chat=ChatEngine(store,cfg,calls.client('primary'),expression_runtime=runtime)
    org=OrganizationEngine(store,cfg,calls.client('native'),chat);publisher=ExpressionPublication(runtime)
    placeholder=protect('E3 review synthetic routing placeholder; not a provider key')
    model_jev=creds['jev_public']|{'encrypted_key':placeholder,'timeout':120,'max_input_chars':60000}
    org.routing.model=lambda:dict(model_jev)
    org.routing.transport=lambda model,body:calls.perform('jev',model,body)
    async def tool_transport(stage,args,model):
        if stage!='search':raise ModelError('review_unplanned_tool_stage',diagnostics={'request_headers_sent':False})
        return await calls.perform('tool_search',model,args)
    org.processing.tools.transport=tool_transport
    if not (root/'start.json').exists():write_once(root/'start.json',{'started_at':timestamp(),'credential_file_hashes':creds['files'],
        'trial_kind':plan['trial_kind'],'source_keys_persisted':False})
    stop=asyncio.Event()
    async def worker():
        while not stop.is_set():
            # 只处理这份有限计划登记的待办；空闲时不触发后台扫描或恢复写入。
            with store.connection() as db:
                pending=db.execute("SELECT 1 FROM organization_tasks WHERE status IN ('queued','waiting_quota') LIMIT 1").fetchone()
            try:worked=await org.run_next() if pending else False
            except sqlite3.OperationalError as error:
                if 'locked' not in str(error):raise
                worked=False
            if not worked:await asyncio.sleep(.1)
    worker_task=asyncio.create_task(worker())
    try:
        for group in plan['fixtures']['groups']:
            binding=seed_group(root,group,plan,service,runtime);pid=binding['persona_id'];service.use_persona(pid)
            if group['network'] and not org.processing.enabled({'persona_id':pid,'channel':'desktop'}):
                reader=chat.knowledge.current_policy(pid);processing=org.processing.policy(pid);web=org.web.policy(pid)
                org.processing.enable_for_persona(pid,True,reader['revision'],processing['revision'],web['revision'],identifier())
            for slot in (p for p in plan['positions'] if p['group_id']==group['id']):
                calls.active=slot['id'];position=root/'positions'/slot['id'];turn_path=position/'turn.json'
                if calls.consecutive_errors>=plan['consecutive_transport_errors_stop']:break
                if turn_path.exists():
                    turn=read(turn_path);value=chat.get(turn['turn_id'])
                    if value['status'] in ('queued','running'):
                        raise ModelError('review_existing_unsettled_turn_not_replayed')
                else:
                    value=await chat.submit('desktop',slot['user'],'expanded-'+slot['id'],public_task=slot.get('public_task'))
                    turn={'position':slot['id'],'turn_id':value['id'],'session_id':value['session_id'],'persona_id':pid}
                    write_once(turn_path,turn)
                    waiting=asyncio.create_task(chat.wait())
                    done,_=await asyncio.wait({waiting,worker_task},timeout=600,return_when=asyncio.FIRST_COMPLETED)
                    if waiting not in done:
                        waiting.cancel();await asyncio.gather(waiting,return_exceptions=True)
                        if worker_task.done():raise ModelError('review_worker_stopped') from worker_task.exception()
                        raise ModelError('review_turn_wait_timeout')
                    await waiting;value=chat.get(turn['turn_id'])
                try:settle_simulation(root,turn,publisher,pid)
                except ValueError as error:
                    if not (position/'presentation-error.json').exists():write_once(position/'presentation-error.json',{'error_code':error_code(error),'new_model_calls':0})
                value=chat.get(turn['turn_id']);pub=value.get('expression',{}).get('publication')
                print(json.dumps({'position':slot['id'],'turn_status':value['status'],'publication':pub['state'] if pub else None,
                    'producer':pub['producer_kind'] if pub else None,'trial_kind':plan['trial_kind']},ensure_ascii=False),flush=True)
                if not pub or pub['state'] not in ('complete','closed'):break
    finally:
        chat.knowledge.close();stop.set();org.draining=True
        for task in list(chat.tasks.values()):
            if not task.done():task.cancel()
        await chat.wait();await org.drain()
        if not worker_task.done():worker_task.cancel()
        await asyncio.gather(worker_task,return_exceptions=True)
    verify_local(root,creds)


def verify_local(root,creds):
    """收尾可单独重做；跳过内容恒为锁标记的当前互斥文件。"""
    plan=load(root,check_source=False)
    for file,expected in read(root/'start.json')['credential_file_hashes'].items():
        if sha(Path(file).read_bytes())!=expected:raise ModelError('review_source_configuration_changed')
    needles=[unprotect(creds[k]).encode() for k in ('main','jev')]+[creds[k].encode() for k in ('main','jev')]
    if any(any(n and n in path.read_bytes() for n in needles) for path in root.rglob('*') if path.is_file() and path!=root/'runner.lock'):
        raise ModelError('review_source_key_persisted')
    if not (root/'execution-complete.json').exists():write_once(root/'execution-complete.json',{'finished_at':timestamp(),
        'trial_kind':plan['trial_kind'],'source_configuration_unchanged':True,'source_keys_not_persisted':True,
        'real_model_calls_replayed':0,'simulation_only':True,'E4_started':False})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','report','verify'));parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--credential-home',type=Path)
    args=parser.parse_args();root=args.root.resolve()
    if args.action=='prepare':
        plan=prepare(root);print(json.dumps({'positions':len(plan['positions']),'groups':len(plan['fixtures']['groups']),'physical_calls_max':plan['physical_calls_max']}));return
    if args.action=='report':
        from .report import report
        print(json.dumps(report(root),ensure_ascii=False));return
    if not args.credential_home:parser.error('run requires --credential-home')
    if args.action=='verify':
        verify_local(root,credentials(args.credential_home.resolve()));print(json.dumps({'verified':True,'new_model_calls':0}));return
    lock=InstanceLock(root/'runner.lock')
    try:asyncio.run(execute(root,creds=credentials(args.credential_home.resolve())))
    finally:lock.close()


if __name__=='__main__':main()
