"""Prepare public artificial recall .3 materials and finite local probes; no API calls."""
import argparse,hashlib,json,time
from pathlib import Path
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.service import RuntimeService
from persona_runtime.models import PersonaSpec,identifier
from persona_runtime.archive import scope
from persona_runtime.recall_support import RecallSupport

SPEC={'format_version':1,'name':'溪禾','introduction':'公开人工连续体验人物。',
    'initial_background':'喜欢做简单饭菜、散步和独立游戏。','core_traits':['坦率','好奇'],
    'expression_style':'自然清楚地聊天，按需要展开。','values_and_boundaries':['尊重明确更正和拒绝']}
MEMORIES=[
 ('tea','我喜欢乌龙茶，不喜欢把饮料做得太甜。'),
 ('mushroom','空气炸锅炸篮很小，杏鲍菇宁愿分批烤。'),
 ('python','我的Python项目各自有虚拟环境，安装依赖和激活是两回事。'),
 ('travel','我喜欢晚上出门散步，不愿意为了短途出门背一整包东西。'),
 ('game','泰拉瑞亚下洞之前，我会先确认回家的路线和照明。'),
 ('coffee','我嫌手冲麻烦，常用聪明杯泡咖啡。'),
]
CONVERSATION=[
 '今天有点没精神，先陪我随便聊聊。',
 '还记得我平时喜欢喝什么茶吗？',
 '你记得的那个偏好里，我对甜度怎么说的？',
 '换个话题，今晚想用空气炸锅做点东西。',
 '前面说的杏鲍菇，我的小炸篮放不下怎么办？',
 '我现在更喜欢白茶了，乌龙茶偶尔才喝。',
 '先不谈饮料，猫又占着我的键盘。',
 '对了，我说过Python项目之间的环境怎么分吗？',
 '就那件事，激活和安装依赖该怎么区别？',
 '你还记得我现在常喝什么茶吗？',
 '今晚想出去走走，你觉得我会纠结带什么？',
 '帮我查一下南京明天下午到晚上的天气。',
 '按刚查到的预报，薄外套够不够？',
 '先别继续安排，我只是想放松一下。',
 '我们前面提到的小炸篮，是不是适合分批？',
 '那个以前的茶偏好不要再当作现在的事实。',
 '说回泰拉瑞亚，我最怕下洞以后忘了哪件事？',
 '今晚喝咖啡就选我平时嫌麻烦时的那个办法。',
 '把我刚才关于茶的更正再接一下。',
 '这次不用重新查，就按我们已经聊过的资料。',
 '我不想每句话都被你总结成一个规则。',
 '假如休息一晚再打开，你还能接上当前话题吗？',
 '之前的冷资料找不到和没有搜过，要分清楚。',
 '今晚先这样，留着下次接着聊。',
]

def prepare(root):
    root=Path(root).resolve();repo=Path(__file__).resolve().parents[1]
    if root==repo or root.is_relative_to(repo):raise ValueError('preparation_outside_source_required')
    if root.exists() and any(root.iterdir()):raise ValueError('preparation_root_must_be_empty')
    root.mkdir(parents=True,exist_ok=True)
    paths=RuntimePaths(root/'instance');cfg=ConfigStore(paths);cfg.initialize()
    store=Store(paths);store.initialize();service=RuntimeService(store)
    persona=service.create_persona(PersonaSpec.from_dict(SPEC));service.use_persona(persona['id'])
    session=service.create_session();selected=scope(persona['id'],'desktop')
    now=time.time();simulated=[now-200*86400];recall=RecallSupport(store,clock=lambda:simulated[0]);sources={}
    for label,text in MEMORIES:
        event=service.append_event(session['id'],'user',text)
        memory=service.add_memory('preference',text,event['id'])
        sources[label]={'memory_id':memory['id'],'event_id':event['id'],
            'unit_id':recall.register('memory',{'id':memory['id']},selected)}
    # This is fixture-local time, never the OS clock or an actual usage history.
    simulated[0]=now;recall.confirm_time()
    event=service.append_event(session['id'],'user','今天晚饭想做西葫芦。')
    fresh=service.add_memory('preference','今天晚饭准备做西葫芦。',event['id'])
    sources['fresh']={'memory_id':fresh['id'],'event_id':event['id'],
        'unit_id':recall.register('memory',{'id':fresh['id']},selected)}
    probes={
        'cold_old_topic':recall.select(selected,'以前说过喜欢乌龙茶',deep=True,kinds=('memory',)),
        'fresh_topic':recall.select(selected,'西葫芦',kinds=('memory',)),
        'scope_denied':recall.select(scope(identifier(),'desktop'),'乌龙茶',required_ids=[sources['tea']['unit_id']]),
    }
    assert sources['tea']['unit_id'] in [i['id'] for i in probes['cold_old_topic']['selected']]
    assert probes['cold_old_topic']['cold_status']=='found'
    assert sources['fresh']['unit_id'] in [i['id'] for i in probes['fresh_topic']['selected']]
    assert not probes['scope_denied']['selected']
    output={'kind':'public_artificial_preparation','persona_id':persona['id'],'scope':selected,
        'source_map':sources,'probes':probes,'model_calls':0,'system_clock_changed':False,
        'lifecycle_time':'fixture-local 200-day gap, no synthetic consumption events',
        'continuous_dialogue_executed':False,'formal_activation':False,'phase_3_status':'prepared_not_executed'}
    atomic_json(root/'preparation.json',output)
    atomic_json(root/'continuous-materials.json',{'version':'recall-continuity-v1','public_artificial':True,
        'persona':SPEC,'turns':[{'position':i+1,'text':text} for i,text in enumerate(CONVERSATION)],
        'expected_observations':['cross_topic_recall','correction','withdrawal','native_new_and_reuse',
            'restart','scope_isolation','no_double_consumption','ordinary_casual_quality'],
        'call_plan':'Freeze providers, finite main/Jev/native/repair positions before real dispatch in .3; prepare performs zero calls.'})
    readme='''# 召回配套.3连续体验准备

这里是公开人工材料及独立实例。preparation.json中的200天间隔属于测试实例的生命周期时钟，未改系统时间或真实调用账本，未制造回忆事件。continuous-materials.json给出24轮连续体验脚本，尚未执行真实对话。

接入前完成：主上下文选材快照／来源变动复检／已完成反馈的正式接点；原v8与新召回候选对照；用户更正、撤回、跨话题及重启场景；正式升级与回退；有限调用数、修复额度和停止条件。真实文本逐次保留供作者审阅，成功与未知物理调用不重放。

本准备实例不带模型密钥，不能当正式人物库；按所选程序和环境显式定位。原角色、正式配置和旧验收数据保留。当前目标止于.0—.2完成及.3前置准备，表达E0尚未开始。
'''
    (root/'准备说明.md').write_text(readme,encoding='utf-8')
    return {'prepared':True,'root':str(root),'public_artificial_units':len(sources),'continuous_turns':len(CONVERSATION),'model_calls':0,'cold_probe_passed':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True)
    print(json.dumps(prepare(parser.parse_args().root),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
