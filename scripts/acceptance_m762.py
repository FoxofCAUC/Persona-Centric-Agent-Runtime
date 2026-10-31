"""Explicit, resumable artificial Assist pairs. No formal database or QQ writes."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import random
import ssl
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine,ChatCompletionsClient
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.disposition import DispositionService
from persona_runtime.disposition_compile import parse_output
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

CASES=(
    ('support','我想找人倾诉，今天准备了很久的方案没有被采用。','support','回应具体失落并表达支持，不诊断、不强行追问'),
    ('celebrate','好消息，我成功了！今天独立完成了第一次公开讲解。','acknowledge','先确认或祝贺，回应具体成功'),
    ('advice','我想给书店阅读角挑座椅，给些建议。空间很小，预算三百元。','explain','给出有依据、符合预算及空间的建议'),
    ('challenge','请批评这个想法：只要每天连续学习十二小时，就一定能学好。','challenge','按明确请求检验必然性并给理由，尊重对方'),
    ('code','请解释并给出完整Python函数add(a,b)，返回两个参数之和，保留可运行代码。',None,'完整可运行函数及解释，不改变运算符'),
    ('exact','计算12.50加7.25，原样保留这句引用“青山不改”，以及网址https://example.org/a?b=1。',None,'19.75、引用和网址完整'),
    ('unknown_fact','你的血型是什么？没有设定就明确说不知道。',None,'血型未设定，不编造'),
    ('unknown','窗边飘过一片云，路旁有一把伞。',None,'不诊断、不擅自推断情绪或强行追问'),
)
AUTHOR='与明确倾诉者先表达支持；收到成功消息先祝贺；讨论建议先解释理由；用户要求批评时给出有依据的质疑。'


class MockClient:
    async def complete(self,model,messages):
        from persona_runtime.chat import ModelCompletion
        return ModelCompletion(json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'收到这条人工测试消息','content_kind':'chat','cancel_if_new_input':False}]}),
            {'total_tokens':1},{'finish_reason':'stop'})


async def run(formal,output,real=False,recheck=None,reason=None):
    formal=formal.resolve();output=output.resolve()
    if not output.is_relative_to(formal/'exports/acceptance'):raise ValueError('Explicit local evidence directory required')
    output.mkdir(parents=True,exist_ok=True);checkpoint=output/'pairs.json'
    mode='real' if real else 'mock'
    frozen={'version':'m762-dev-pairs-v1','cases':CASES,'author':AUTHOR,'seed':762,'role_retrieval':'lexical'}
    fixture=output/'frozen-fixtures.json'
    if fixture.exists():
        if fingerprint(json.loads(fixture.read_text(encoding='utf-8')))!=fingerprint(frozen):raise ValueError('Fixture changed')
    else:atomic_json(fixture,frozen)
    state=json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {'mode':mode,'fixture_hash':fingerprint(frozen),'cases':{},'qq_sends':0,'compiler_calls':0}
    if state['mode']!=mode:raise ValueError('Separate output required for mock and real')
    paths=RuntimePaths(output/'runtime');store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize();runtime=RuntimeService(store)
    if 'persona_id' not in state:
        pid=runtime.create_persona(replace(generic_persona(),name='M762人工开发资料',expression_style=AUTHOR))['id'];runtime.use_persona(pid)
        state['persona_id']=pid
        if real:
            local=cfg.load();approved=ConfigStore(RuntimePaths(formal)).load()
            local['model']=deepcopy(approved['model']);local['character_selector']['enabled']=False;local['organization']['daily_enabled']=False
            atomic_json(cfg.file,local)
        else:cfg.update({'model':{'name':'mock','base_url':'https://example.invalid'}})
        ds=DispositionService(store);source=next(s for s in ds.sources(pid)['sources'] if s['kind']=='expression')
        definitions=[]
        for key,category,action in (('listen','support_request','support'),('celebrate','celebration','acknowledge'),('advice','advice_request','explain'),('critique',None,'challenge')):
            definitions.append({'id':key,'label':'人工作者偏好'+key,'source_refs':[{'source_id':source['source_id'],'quote':AUTHOR}],
                'triggers':[{'field':'category','op':'eq','value':category}] if category else [],'preferences':{action:2},'priority':10,'enabled':True})
        doc=parse_output(json.dumps({'schema_version':1,'definitions':definitions},ensure_ascii=False),[source])
        h=ds.draft_update(pid,doc,0,identifier());ds.confirm(pid,h['draft_id'],None,h['revision'],identifier())
        state['projection_id']=ds.current(pid)['active_id'];atomic_json(checkpoint,state)
    pid=state['persona_id'];runtime.use_persona(pid)
    # Explicit CA bundle, certificate/hostname checks on. Runtime defaults unchanged.
    if real:
        import certifi
        client=ChatCompletionsClient(ssl_context=ssl.create_default_context(cafile=certifi.where()))
        state['tls']='certifi_verified_isolated_client'
    else:client=MockClient()
    grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((pid,)),formal)
    engine=ChatEngine(store,cfg,client,assist_admission=grant);ingress=IngressService(engine)
    order=[(case,arm) for case in CASES for arm in ('baseline','assist')];random.Random(762).shuffle(order)
    if recheck:
        if not reason or recheck not in state['cases'] or state['cases'][recheck]['state']!='failed':raise ValueError('Only explicit failed case with reason may be rechecked')
        base_key=recheck.split('/recheck',1)[0]
        if any((k==base_key or k.startswith(base_key+'/recheck')) and v['state']=='completed' for k,v in state['cases'].items()):raise ValueError('Slot already completed')
        order=[(case,arm) for case,arm in order if case[0]+'/'+arm==base_key]
        if len(order)!=1:raise ValueError('Unknown recheck')
    failures=0;quota=QuotaService(store,cfg);limits=quota._limits()
    state.update(closed=False,limits_restored=False);atomic_json(checkpoint,state)
    try:
        with quota.acceptance():
            for case,arm in order:
                key=recheck+'/recheck' if recheck else case[0]+'/'+arm
                if key in state['cases']:continue # reserved/unknown is never replayed
                p=ingress.current_policy(pid,'desktop')
                ingress.update_policy(pid,'desktop','legacy_per_message',p['id'],identifier(),reply_mode='natural_structured',
                    character_retrieval_mode='lexical',disposition_mode=arm,expression_policy_version=3)
                session=runtime.create_session()['id'];op=identifier()
                state['cases'][key]={'state':'reserved','request_id':op,'session_id':session,'expected_action':case[2],
                                     'checks':case[3],'input_hash':fingerprint(case[1]),'recheck_of':recheck,'reason':reason};atomic_json(checkpoint,state)
                start=time.monotonic();receipt=ingress.accept({'text':case[1],'request_id':op,'session_id':session,'persona_id':pid})
                turn={'id':receipt['turn_id']}
                state['cases'][key]['turn_id']=turn['id'];atomic_json(checkpoint,state)
                await engine.wait();result=engine.get(turn['id'])
                with store.connection() as db:
                    manifests=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(turn['id'],))]
                    attempts=db.execute('SELECT count(*) FROM attempts WHERE turn_id=?',(turn['id'],)).fetchone()[0]
                    response=db.execute('SELECT text FROM events WHERE id=?',(result.get('reply_event_id'),)).fetchone()
                item=state['cases'][key];item.update(state=result['status'],error=result.get('error_code'),attempts=attempts,
                    usage=result.get('usage'),elapsed_ms=round((time.monotonic()-start)*1000,3),manifest_ids=[m['context_id'] for m in manifests])
                if manifests:item['decision']=manifests[-1].get('decision_capsule')
                if response:atomic_json(output/(case[0]+'-'+arm+('-recheck' if recheck else '')+'-reply.json'),{'text':response[0],'turn_id':turn['id']})
                failures=0 if result['status']=='completed' else failures+1
                atomic_json(checkpoint,state);print(json.dumps({'case':key,'status':result['status'],'error':result.get('error_code')},ensure_ascii=False),flush=True)
                if failures>=5:state['stop_reason']='five_consecutive_failures';break
        with store.connection() as db:
            state.update(actual_main_attempts=db.execute('SELECT count(*) FROM attempts').fetchone()[0],
                         selection_calls=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0])
        state['effective_cases']={}
        for case in CASES:
            for arm in ('baseline','assist'):
                base_key=case[0]+'/'+arm
                good=[k for k,v in state['cases'].items() if (k==base_key or k.startswith(base_key+'/recheck')) and v['state']=='completed']
                state['effective_cases'][base_key]=sorted(good,key=len)[-1] if good else base_key
        state['all_completed']=all(state['cases'].get(k,{}).get('state')=='completed' and state['cases'][k]['attempts']==1 for k in state['effective_cases'].values())
    finally:
        await engine.wait();engine.cognitive.shadow.close();engine.cognitive.characters.close();quota.end_acceptance()
        state.update(limits_restored=limits==quota._limits(),closed=True);atomic_json(checkpoint,state)
    print(json.dumps({k:v for k,v in state.items() if k not in ('cases','persona_id','projection_id')},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--formal-home',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--real',action='store_true');p.add_argument('--recheck');p.add_argument('--reason');a=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(a.output.resolve()/'acceptance.lock')
    try:asyncio.run(run(a.formal_home,a.output,a.real,a.recheck,a.reason))
    finally:lock.close()
