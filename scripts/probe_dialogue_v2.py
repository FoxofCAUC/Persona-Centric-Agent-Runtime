"""Resumable artificial development conversations for the v2 voice rewrite."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine,ChatCompletionsClient
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.dialogue_voice import freeze
from persona_runtime.disposition import DispositionService
from persona_runtime.disposition_compile import parse_output
from persona_runtime.ingress import IngressService
from persona_runtime.instance import InstanceLock
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from scripts.m763_provider_gate import DirectoryGate

CASES=(
 ('d01','今天在电梯里跟自己的外卖说了声谢谢，外卖员已经走了，我人傻了'),
 ('d02','我很难受，原本约好的朋友又临时放我鸽子，饭都点好了'),
 ('d03','好消息，我成功了！第一次做陶杯居然没裂，不过丑得很自信'),
 ('d04','在便利店买了个新品面包，第一口怀疑人生，第二口觉得还能吃'),
 ('d05','今天啥也没干，就把窗帘换了，房间突然像开了新地图'),
 ('d06','我想找人倾诉，群里每次说正事都被表情包刷走，烦死了'),
 ('d07','想听你的真实看法，我是不是该把那盆永远养不好的薄荷送人'),
 ('d08','哈哈我给小区的橘猫起名“总监”，它现在看谁都像在扣绩效'),
 ('d09','请批评这个想法：只要我一直迁就别人，大家就不会讨厌我'),
 ('d10','只输出27乘以14的结果'),
 ('d11','给出完整Python函数unique(items)，保持顺序去重，不修改输入'),
 ('d12','这句“晚霞把楼顶染红了”原样保留，再随便聊一句你的感觉'),
)
AUTHOR='坦率、温热，熟了会轻轻吐槽，有点好奇心，不爱把聊天讲成大道理。会为别人不公平的遭遇生气，听到开心事也会跟着乐。喜欢手作和植物。'


async def run(home,out,baseline_from=None):
    out.mkdir(parents=True,exist_ok=True)
    approved=ConfigStore(RuntimePaths(home)).load()
    gate=DirectoryGate(out/'provider-check.json',approved,out)
    if not gate.path.exists():
        checks=await gate.check_all();atomic_json(gate.path,{'normal':all(x['normal'] for x in checks),'checks':checks,'model_calls':0})
    await gate.ensure()
    state_path=out/'checkpoint.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'fixture_hash':fingerprint(CASES),'slots':{},'closed':False}
    if state['fixture_hash']!=fingerprint(CASES):raise ValueError('development_fixture_changed')
    for arm in ('baseline','v2'):
        if arm=='baseline' and baseline_from:
            previous=json.loads((baseline_from/'checkpoint.json').read_text(encoding='utf-8'))
            if previous['fixture_hash']!=state['fixture_hash']:raise ValueError('baseline_fixture_changed')
            state['baseline_reused_from']=str(baseline_from);atomic_json(state_path,state);continue
        paths=RuntimePaths(out/arm);store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
        local=cfg.load();local['model']=deepcopy(approved['model']);local['character_selector']['enabled']=False
        local['organization']['daily_enabled']=False;atomic_json(cfg.file,local)
        runtime=RuntimeService(store)
        key='persona_'+arm
        if key not in state:
            pid=runtime.create_persona(replace(generic_persona(),name='澄音',core_traits=['坦率','爱吐槽','关心朋友'],expression_style=AUTHOR))['id']
            runtime.use_persona(pid);ds=DispositionService(store)
            src=next(s for s in ds.sources(pid)['sources'] if s['kind']=='expression')
            defs=[]
            for category,action in [('support_request','support'),('celebration','acknowledge')]:
                defs.append({'id':category,'label':category,'source_refs':[{'source_id':src['source_id'],'quote':src['text']}],
                             'triggers':[{'field':'category','op':'eq','value':category}],
                             'preferences':{action:2},'priority':5,'enabled':True})
            doc=parse_output(json.dumps({'schema_version':1,'definitions':defs},ensure_ascii=False),[src])
            head=ds.draft_update(pid,doc,0,identifier());ds.confirm(pid,head['draft_id'],None,head['revision'],identifier())
            state[key]=pid;atomic_json(state_path,state)
        pid=state[key];runtime.use_persona(pid)
        client=ChatCompletionsClient();grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((pid,)),home)
        engine=ChatEngine(store,cfg,client,assist_admission=grant);ingress=IngressService(engine);quota=QuotaService(store,cfg)
        try:
            with quota.acceptance():
                failures=0
                for cid,text in CASES:
                    slot=cid+'/'+arm
                    if slot in state['slots']:continue
                    await gate.ensure()
                    local=cfg.load();local['model']=deepcopy(approved['model'])
                    if arm=='v2':local['model']['_dialogue_profile']=freeze(cid,[text])
                    atomic_json(cfg.file,local)
                    policy=ingress.current_policy(pid,'desktop')
                    ingress.update_policy(pid,'desktop','legacy_per_message',policy['id'],identifier(),reply_mode='natural_structured',
                        expression_policy_version=3,character_retrieval_mode='lexical',disposition_mode='assist' if arm=='v2' else 'baseline')
                    sid=runtime.create_session()['id'];rid=fingerprint(['voice-v2-development',slot])
                    state['slots'][slot]={'state':'accepted','request_id':rid};atomic_json(state_path,state)
                    started=time.monotonic();receipt=ingress.accept({'text':text,'request_id':rid,'persona_id':pid,'session_id':sid})
                    await engine.wait();turn=engine.get(receipt['turn_id'])
                    item={'state':turn['status'],'turn_id':turn['id'],'error':turn.get('error_code'),'usage':turn.get('usage'),
                          'elapsed_ms':round((time.monotonic()-started)*1000,3)}
                    state['slots'][slot]=item;atomic_json(state_path,state)
                    atomic_json(out/(cid+'-'+arm+'.json'),{'input':text,'output':turn.get('display_reply') or turn.get('reply'),'result':item})
                    print(json.dumps({'case':slot,'status':turn['status']},ensure_ascii=False),flush=True)
                    failures=0 if turn['status']=='completed' else failures+1
                    if failures>=5 or (turn['status']!='completed' and item['elapsed_ms']>600000):raise ValueError('development_model_tests_stopped')
        finally:
            await engine.wait();await engine.observer.drain();engine.cognitive.shadow.close();engine.cognitive.characters.close();quota.end_acceptance()
    state['closed']=True;atomic_json(state_path,state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--baseline-from',type=Path);a=p.parse_args()
    home=a.home.resolve();out=a.output.resolve()
    if not out.is_relative_to(home/'exports/acceptance'):raise ValueError('controlled_output_required')
    lock=InstanceLock(out/'execution.lock')
    try:asyncio.run(run(home,out,a.baseline_from.resolve() if a.baseline_from else None))
    finally:lock.close()
