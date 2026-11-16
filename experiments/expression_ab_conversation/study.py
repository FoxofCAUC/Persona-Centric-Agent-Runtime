"""冻结并执行两条独立历史的48轮链；首次调用不补跑。"""
import argparse
import asyncio
import json
from pathlib import Path
import random
import secrets
import shutil
import sqlite3
import time

from persona_runtime.chat import ChatEngine,ChatCompletionsClient,ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.deepseek_native_response import NativeResponse
from persona_runtime.expression_persona_draft import PROFILE
from persona_runtime.expression_runtime import ExpressionRuntime
from persona_runtime.expression_transport import ExpressionHTTPClient
from persona_runtime.expression_utterance_publication import SpeechPublication
from persona_runtime.instance import InstanceLock
from persona_runtime.models import PersonaSpec,generic_persona,identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.security import protect,unprotect
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from experiments.expression_e3_review.common import read,sha,digest,write_once
from experiments.expression_e3_review.runner import credentials,error_code
from .fixtures import GROUPS

REPO=Path(__file__).resolve().parents[2]
VERSION='expression-independent-conversation-AB-v1'
LIMITS={'primary':96,'expression':48,'expression_notice':2,'notice':4,'jev':24}


def prepare(root,material,*,trial_kind='real'):
    if root.exists() or root.resolve().is_relative_to(REPO):raise ModelError('ab_plan_target_invalid')
    if trial_kind not in ('real','offline_fixture'):raise ModelError('ab_trial_kind_invalid')
    from scripts.pcar_expression_segment_preflight import inspect
    if not inspect(REPO)['passed']:raise ModelError('ab_R2_preflight_required')
    if len(GROUPS)!=8 or any(len(g['turns'])!=6 for g in GROUPS):raise ModelError('ab_fixture_shape_invalid')
    root.mkdir(parents=True)
    personas=read(REPO/'experiments/expression_e3_review/fixtures.json')['personas']
    positions=[]
    for g in GROUPS:
        for chain in ('A','B'):
            for index,user in enumerate(g['turns'],1):
                positions.append({'id':g['id']+'-'+chain+'-'+str(index),'group':g['id'],'chain':chain,'index':index,'user':user,
                    'network':g['network'],'public_task':{'question':user} if g['network'] and index==1 else None})
    frozen={}
    for directory in ('persona_runtime','experiments/expression_ab_conversation','experiments/expression_e3_review'):
        for path in sorted((REPO/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.json'):
                name=path.relative_to(REPO).as_posix();dest=root/'frozen-source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,dest);frozen[name]=sha(path.read_bytes())
    plan={'version':VERSION,'trial_kind':trial_kind,'created_at':timestamp(),'profile_B':PROFILE,'profile_A':'released_original_v8',
          'groups':GROUPS,'personas':personas,'positions':positions,'limits':LIMITS,'max_client_requests':174,'native_http_requests':0,
          'controlled_material':read(material),'controlled_material_sha256':sha(material.read_bytes()),'source_hashes':frozen,
          'independent_actual_histories':True,'B_primary_saved_separately':True,'per_actual_position_sends':1,
          'automatic_retries':0,'paid_selector':0,'paid_splitter':0,'paid_learning':0,
          'presentation':'isolated simulated receipts, no desktop/user-read claim','author_gate_before_E4':True}
    write_once(root/'plan.json',plan);ph=sha((root/'plan.json').read_bytes());write_once(root/'plan-hash.json',{'sha256':ph})
    choices=['A']*4+['B']*4;random.Random(secrets.token_hex(24)).shuffle(choices)
    write_once(root/'review-key.json',{'plan_sha256':ph,'left_chain':{g['id']:v for g,v in zip(GROUPS,choices)}})
    return plan


def load(root,*,check_source=True):
    plan=read(root/'plan.json')
    if plan['version']!=VERSION or sha((root/'plan.json').read_bytes())!=read(root/'plan-hash.json')['sha256']:raise ModelError('ab_frozen_plan_changed')
    if len(plan['positions'])!=96 or plan['max_client_requests']!=174 or plan['limits']!=LIMITS:raise ModelError('ab_call_budget_changed')
    if check_source:
        for name,value in plan['source_hashes'].items():
            if sha((REPO/name).read_bytes())!=value:raise ModelError('ab_frozen_source_changed')
    return plan


def reserve(root,plan,slot,purpose,request):
    if purpose not in LIMITS or slot['id'] not in {p['id'] for p in plan['positions']}:raise ModelError('ab_call_scope_invalid')
    if purpose in ('expression','expression_notice') and slot['chain']!='B':raise ModelError('ab_control_has_no_expression')
    if purpose in ('notice','expression_notice') and (not slot['network'] or slot['index']!=1):raise ModelError('ab_notice_position_not_planned')
    if purpose=='jev' and not slot['network']:raise ModelError('ab_jev_position_not_planned')
    markers=list((root/'positions').glob('*/calls/*.dispatch.json'))
    if len(markers)>=174 or sum(read(p)['purpose']==purpose for p in markers)>=LIMITS[purpose]:raise ModelError('ab_call_budget_used')
    path=root/'positions'/slot['id']/'calls'/(purpose+'.dispatch.json')
    if path.exists():raise ModelError('ab_call_already_dispatched')
    row={'position':slot['id'],'purpose':purpose,'request_hash':digest(request),'started_at':timestamp(),'ordinal':len(markers)+1}
    write_once(path,row);write_once(path.with_name(purpose+'.request.json'),request);return row


class Calls:
    def __init__(self,root,plan,store,creds,overrides=None):
        self.root,self.plan,self.store,self.creds=root,plan,store,creds
        self.active=None;self.overrides=overrides or {};self.errors=0
        self.main=ChatCompletionsClient();self.expression=ExpressionHTTPClient()

    async def perform(self,purpose,model,inputs,*,save_raw=None):
        slot=self.active
        if self.errors>=3:raise ModelError('ab_consecutive_transport_errors_stop',diagnostics={'request_headers_sent':False,'response_headers_received':False})
        request={'model':{k:v for k,v in model.items() if k!='encrypted_key'},'input':inputs}
        row=reserve(self.root,self.plan,slot,purpose,request);folder=self.root/'positions'/slot['id']/'calls';started=time.monotonic()
        try:
            live=model|{'encrypted_key':self.creds['jev' if purpose=='jev' else 'main']}
            callback=save_raw
            if purpose in ('expression','expression_notice'):
                def received(raw):
                    path=folder/(purpose+'.response.raw.json')
                    if path.exists():
                        if path.read_bytes()!=raw:raise ModelError('ab_raw_receipt_conflict')
                    else:
                        with path.open('xb') as f:f.write(raw)
                    save_raw(raw)
                callback=received
            if purpose in self.overrides:value=await self.overrides[purpose](live,inputs,save_raw=callback)
            elif purpose=='jev':
                from persona_runtime.knowledge_web_decision import execute
                value=await execute(live,inputs)
            elif purpose in ('expression','expression_notice'):
                value=await self.expression.complete(live,inputs,save_raw=callback)
            else:value=await self.main.complete(live,inputs)
            if purpose in ('expression','expression_notice'):row['diagnostics']=value['diagnostics']
            else:
                row.update(usage=value[1],diagnostics=getattr(value,'diagnostics',{}))
                write_once(folder/(purpose+'.receipt.json'),{'text':value[0],'usage':value[1],'diagnostics':getattr(value,'diagnostics',{})})
            row['state']='returned';self.errors=0;return value
        except BaseException as error:
            row.update(state='error',error_code=error_code(error),usage=getattr(error,'usage',None),diagnostics=getattr(error,'diagnostics',{}));self.errors+=1;raise
        finally:
            row['elapsed_ms']=round((time.monotonic()-started)*1000,3);write_once(folder/(purpose+'.outcome.json'),row)

    def client(self,kind):
        owner=self
        class Client:
            async def complete(self,model,messages,**kwargs):
                purpose='notice' if kind=='primary' and '_search_notice_profile' in model else kind
                if kind=='expression':
                    with owner.store.connection() as db:
                        notice=db.execute("SELECT 1 FROM expression_speech_runs r JOIN expression_speech_units u ON u.id=r.utterance_id WHERE u.turn_id=? AND r.state='dispatched' AND r.raw_response IS NULL",(owner.active['turn_id'],)).fetchone()
                    if notice:purpose='expression_notice'
                return await owner.perform(purpose,model,messages,**kwargs)
        return Client()

    def native(self):
        owner=self
        class Client:
            async def complete(self,model,messages):
                slot=owner.active;group=next(g for g in owner.plan['groups'] if g['id']==slot['group'])
                if not slot['network'] or slot['index']!=1:raise ModelError('ab_unplanned_controlled_retrieval')
                material=owner.plan['controlled_material'];ok=group['material']=='success'
                url=material['url'] if ok else 'https://example.org/pcar-controlled-unavailable'
                content=material['summary'] if ok else '未完整取得青岚相册2027版的功能说明，当前不能确认是否支持离线导出。'
                call={'index':0,'id':'controlled-'+slot['id'],'type':'deepseek_read_url','kind':'read_link','action':{'type':'read_link','url':url},
                      'status':'completed' if ok else 'failed','page':{'url':url,'title':'Python官方文档冻结材料' if ok else '受控失败状态'}}
                if not ok:call['error']={'code':'controlled_material_unavailable'}
                raw={'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content,
                     'server_tool_calls':[{'index':0,'calls':[call]}]}}]}
                path=owner.root/'positions'/slot['id']/'controlled-native.json';write_once(path,{'kind':'controlled_frozen_material_not_live_search','raw':raw,'real_http_calls':0,'actual_cny':None})
                parser=NativeResponse();parser.consume(raw);result=parser.complete()
                result.diagnostics.update(network_route='controlled_frozen_material',request_headers_sent=False,response_headers_received=False)
                return result
        return Client()


def seed(root,group,chain,plan,service,runtime):
    path=root/'groups'/group['id']/chain/'binding.json'
    if path.exists():return read(path)
    spec=generic_persona().to_dict();spec.update(plan['personas'][group['persona']],initial_background='和对方从这次聊天开始认识。')
    pid=service.create_persona(PersonaSpec.from_dict(spec))['id'];service.use_persona(pid)
    if chain=='B':runtime.configure(pid,{'enabled':True,'protocol':PROFILE},0,identifier())
    binding={'persona_id':pid,'chain':chain,'group':group['id'],'initial_persona_sha256':digest(spec)};write_once(path,binding);return binding


def present(root,slot,publisher,pid,session):
    for unit in publisher.list(pid,session,slot['turn_id']):
        plan=publisher.plan(pid,session,unit['id'])
        if not plan or plan['state'] in ('complete','closed'):continue
        for item in plan['items']:
            if item['state']!='pending':continue
            offered=publisher.offer(pid,session,unit['id'],plan['id'],plan['revision'],item['ordinal'],'R3-headless-simulation')
            if 'body' not in offered:continue
            path=root/'positions'/slot['id']/'simulation'/(unit['phase']+'-'+str(item['ordinal'])+'.json')
            # 已创建的交付记录绝不二次确认或重新交付。
            if path.exists():raise ModelError('ab_simulated_fragment_already_reserved')
            receipt=identifier();write_once(path,{'utterance_id':unit['id'],'fragment_id':offered['id'],'body':offered['body'],'body_hash':offered['body_hash'],'lease_id':offered['lease_id'],'receipt_id':receipt,'presentation':'simulated'})
            publisher.acknowledge(pid,session,unit['id'],offered['id'],offered['lease_id'],'R3-headless-simulation',receipt,offered['body_hash'])


async def execute(root,creds,*,overrides=None):
    plan=load(root)
    if overrides and plan['trial_kind']!='offline_fixture':raise ModelError('ab_offline_override_forbidden')
    # 在创建任何执行器之前拒绝未封口位置，避免清理路径启动遗留任务。
    for spec in plan['positions']:
        folder=root/'positions'/spec['id']
        if (folder/'turn.json').exists() and not (folder/'finished.json').exists():
            raise ModelError('ab_existing_unsettled_position_not_replayed')
    paths=RuntimePaths(root/'instance');cfg=ConfigStore(paths);cfg.initialize()
    cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-flash','max_tokens':8192,'max_input_chars':60000,'timeout':120},
                'organization':{'reuse_chat':True,'daily_enabled':False}})
    store=Store(paths);store.initialize();service=RuntimeService(store);calls=Calls(root,plan,store,creds,overrides)
    runtime=ExpressionRuntime(store,calls.client('expression'));chat=ChatEngine(store,cfg,calls.client('primary'),expression_runtime=runtime)
    org=OrganizationEngine(store,cfg,calls.native(),chat);publisher=SpeechPublication(runtime.outlet)
    placeholder=protect('R3 synthetic routing placeholder; not a provider key')
    org.routing.model=lambda:creds['jev_public']|{'encrypted_key':placeholder,'timeout':120,'max_input_chars':60000}
    org.routing.transport=lambda model,body:calls.perform('jev',model,body)
    async def no_tool(*args,**kwargs):raise ModelError('ab_no_live_tool_requests',diagnostics={'request_headers_sent':False,'response_headers_received':False})
    org.processing.tools.transport=no_tool
    if not (root/'start.json').exists():write_once(root/'start.json',{'credential_file_hashes':creds['files'],'started_at':timestamp()})
    else:
        if read(root/'start.json')['credential_file_hashes']!=creds['files']:raise ModelError('ab_source_credentials_changed')
    stopping=asyncio.Event()
    async def worker():
        while not stopping.is_set():
            with store.connection() as db:pending=db.execute("SELECT 1 FROM organization_tasks WHERE status IN ('queued','waiting_quota') LIMIT 1").fetchone()
            try:worked=await org.run_next() if pending else False
            except sqlite3.OperationalError as error:
                if 'locked' not in str(error):raise
                worked=False
            if not worked:await asyncio.sleep(.05)
    task=asyncio.create_task(worker())
    try:
        for group in plan['groups']:
            for chain in ('A','B'):
                binding=seed(root,group,chain,plan,service,runtime);pid=binding['persona_id'];service.use_persona(pid)
                if group['network'] and not org.processing.enabled({'persona_id':pid,'channel':'desktop'}):
                    reader=chat.knowledge.current_policy(pid);processing=org.processing.policy(pid);web=org.web.policy(pid)
                    org.processing.enable_for_persona(pid,True,reader['revision'],processing['revision'],web['revision'],identifier())
                for spec in (p for p in plan['positions'] if p['group']==group['id'] and p['chain']==chain):
                    if calls.errors>=3:raise ModelError('ab_consecutive_transport_errors_stop')
                    folder=root/'positions'/spec['id'];binding_path=folder/'turn.json';calls.active=dict(spec)
                    if binding_path.exists():
                        slot=read(binding_path);calls.active.update(slot)
                        if not (folder/'finished.json').exists():raise ModelError('ab_existing_unsettled_position_not_replayed')
                        continue
                    turn=await chat.submit('desktop',spec['user'],'R3-'+spec['id'],public_task=spec['public_task'])
                    slot={'id':spec['id'],'turn_id':turn['id'],'session_id':turn['session_id'],'persona_id':pid};write_once(binding_path,slot);calls.active.update(slot)
                    waiting=asyncio.create_task(chat.wait());started=time.monotonic()
                    while not waiting.done():
                        if task.done():raise ModelError('ab_worker_stopped') from task.exception()
                        if time.monotonic()-started>480:raise ModelError('ab_position_wait_timeout')
                        if chain=='B':present(root,calls.active,publisher,pid,turn['session_id'])
                        await asyncio.sleep(.05)
                    await waiting
                    if chain=='B':present(root,calls.active,publisher,pid,turn['session_id'])
                    value=chat.get(turn['id']);write_once(folder/'finished.json',{'state':value['status'],'reply_hash':sha(value['reply'].encode()) if value.get('reply') else None,
                        'elapsed_seconds':round(time.monotonic()-started,3),'error_code':value['error_code']})
                    print(json.dumps({'position':spec['id'],'state':value['status'],'chain':chain,'trial_kind':plan['trial_kind']},ensure_ascii=False),flush=True)
    finally:
        chat.knowledge.close();stopping.set();org.draining=True
        for pending in list(chat.tasks.values()):
            if not pending.done():pending.cancel()
        await chat.wait();await org.drain()
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    verify(root,creds)


def verify(root,creds):
    load(root,check_source=False)
    if (root/'start.json').exists() and read(root/'start.json')['credential_file_hashes']!=creds['files']:
        raise ModelError('ab_source_credentials_changed')
    for name,expected in creds['files'].items():
        if sha(Path(name).read_bytes())!=expected:raise ModelError('ab_source_credentials_changed')
    needles=[v.encode() for k in ('main','jev') for v in (creds[k],unprotect(creds[k])) if v]
    for path in root.rglob('*'):
        if path.is_file() and path.name!='run.lock' and any(n in path.read_bytes() for n in needles):raise ModelError('ab_source_key_persisted')
    return {'source_credentials_unchanged':True,'source_keys_not_persisted':True,'new_model_calls':0}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run','report','verify'));p.add_argument('--root',type=Path,required=True)
    p.add_argument('--material',type=Path);p.add_argument('--credential-home',type=Path);a=p.parse_args();root=a.root.resolve()
    if a.action=='prepare':
        if not a.material:p.error('material required')
        plan=prepare(root,a.material);print(json.dumps({'positions':len(plan['positions']),'max_client_requests':174}));return
    if a.action=='report':
        from .report import report
        print(json.dumps(report(root),ensure_ascii=False));return
    if not a.credential_home:p.error('credential home required')
    creds=credentials(a.credential_home.resolve())
    if a.action=='verify':print(json.dumps(verify(root,creds)));return
    lock=InstanceLock(root/'run.lock')
    try:asyncio.run(execute(root,creds))
    finally:lock.close()


if __name__=='__main__':main()
