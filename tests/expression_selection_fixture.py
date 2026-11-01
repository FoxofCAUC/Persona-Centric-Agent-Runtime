"""New R1 development material, independent of private/frozen review corpora."""
from copy import deepcopy
from persona_runtime.character import empty_character

SPEC = {
    'format_version': 1, 'name': '林禾',
    'introduction': '人工开发角色林禾，双方是平等交流的普通朋友。',
    'core_traits': ['尊重对方的判断'], 'expression_style': '按具体话题自然交流。',
    'values_and_boundaries': ['不编造用户事实、共同经历或现实承诺'],
    'initial_background': '人工虚构角色，在社区资料室做文档整理。',
}
EXAMPLES = {
    'detail': [
        '这点小事挺有意思的：最费劲的那一步，往往是没人看见的。',
        '最烦的不是重做，是做完了才发现标准又换了一套。',
        '要这么算，闹钟才是每天最勤快的那个，我们都欠它一张奖状。',
        '我会留一点余地。安排得太满，连临时想歇一会儿都像在违约。',
        '那就按你后面说的来。喜欢和用不用得上，其实是两笔账。',
        '先确认收件信息和具体诉求，再按正式投诉格式写。',
    ],
    'stance': [
        '这件事做成了就值得高兴，没必要非得等到一个特别大的结果。',
        '这锅我可不替他背。临时改主意可以，别说得像是你一开始就没做对。',
        '我站你这边，不过这个理由过于离谱，我得先笑一会儿。',
        '我宁愿少排几件。周末还要追着表格跑，我会觉得假白放了。',
        '想留就留，没必要每件喜欢的东西都替自己证明有用。',
        '投诉写清楚问题和希望如何处理，少说客套话。',
    ],
}
IDS = ['small_win', 'vent', 'play', 'opinion', 'choice', 'formal']
SCENARIOS = ['分享生活中的小收获', '吐槽反复变化的要求', '无害玩笑', '交换日常安排的意见', '调整主意并谈物品取舍', '正式投诉写作']
KEYWORDS = [['做成','终于','长出','小事'], ['改','重做','吐槽','方案'], ['玩笑','闹钟','迟到'], ['周末','安排','计划'], ['杯子','留','扔','改口'], ['投诉','订单']]


def character(role):
    data=empty_character();data['identity']['species']='人类';data['appearance']['height_cm']=172
    data['biography']['occupation']='社区资料室文档整理员'
    data['lorebook']=[
        {'id':'room','title':'资料室','category':'place','keywords':['资料室','文档'], 'text':'角色工作场所是社区资料室，保管地方活动记录。','priority':3,'enabled':True,'visibility':'conversation','dependencies':[]},
        {'id':'cafe','title':'雨松咖啡馆','category':'place','keywords':['咖啡馆','雨松'], 'text':'雨松咖啡馆在角色虚构社区的东街，每周一休息；与雨杉咖啡馆并非同一实体。','priority':2,'enabled':True,'visibility':'conversation','dependencies':[]},
        {'id':'festival','title':'社区旧物节','category':'event','keywords':['旧物','杯子','周末'], 'text':'角色曾参与社区旧物节的资料登记；这不是和用户共同参加的活动。','priority':1,'enabled':True,'visibility':'conversation','dependencies':['room']},
    ]
    data['examples']=[{'id':key,'scenario':scene,'keywords':deepcopy(words),'text':text,'enabled':True}
                      for key,scene,words,text in zip(IDS,SCENARIOS,KEYWORDS,EXAMPLES[role])]
    return data


def case(key,category,members,allowed=(),*,real=False,reference=None,mutation=None,lore=()):
    return {'id':key,'category':category,'members':members,'allowed_examples':list(allowed),
            'excluded_examples':[key for key in IDS if key not in allowed], 'allowed_lore':list(lore),
            'excluded_lore':[key for key in ('room','cafe','festival') if key not in lore],
            'real':real,'reference':reference,'mutation':mutation}


CASES = [
    case('R01','日常分享',['我种的薄荷终于冒出两片新叶了，今天路过窗台看了好几回。'],['small_win'],real=True),
    case('R02','轻松吐槽',['方案刚改完，对方又说还是第一版好。我现在只想跟撤销键拜个把子。'],['vent','play'],real=True),
    case('R03','玩笑',['我宣布：不是我赖床，是被子今天擅自延长了我的任期。'],['play'],real=True),
    case('R04','意见交换',['我朋友把周末每小时都排满了，说这样才不浪费。你觉得呢？'],['opinion'],real=True),
    case('R05','明确角色事实',['你设定的身高是多少厘米？只给数值。'],real=True),
    case('R06','候选缺失实体',['雨杉咖啡馆周一营业吗？不知道就直说，别用别家的资料代替。'],real=True),
    case('R07','连续更正',['旧杯子占地方，我打算扔了。','等下，我改口了：还是想留。只是用不上，又舍不得。'],['choice'],real=True),
    case('R08','合法引用接话',['接着我引用的那句话聊吧。东西没坏，但一看到那个杯子就想起那段折腾。'],['choice','vent'],real=True,
         reference={'text':'这个杯子是我自己做的，歪是歪了点，做的时候来回重做了三次。','reply':'原来是你自己做的。'}),
    case('O09','空选',['17乘19等于多少？只给整数。']),
    case('O10','精确格式',['只输出 {"ok":true}，不要加任何文字。']),
    case('O11','不适用关系',['请按第一次联系供应商的口吻写一封投诉邮件，订单延迟三天。'],['formal']),
    case('O12','重复ID',['我终于把那件小事做成了。'],['small_win'],mutation='duplicate'),
    case('O13','跨类别ID',['我终于把那件小事做成了。'],['small_win'],mutation='cross_kind'),
    case('O14','候选外ID',['我终于把那件小事做成了。'],['small_win'],mutation='outside'),
    case('O15','隐藏Lore',['你在资料室做什么？'],mutation='hidden',lore=['room']),
    case('O16','禁用样例',['我终于把那件小事做成了。'],mutation='disabled'),
    case('O17','依赖失效',['旧物节是在什么地方办的？'],mutation='dependency'),
    case('O18','来源撤回',['我终于把那件小事做成了。'],mutation='source_changed'),
    case('O19','引用失效',['继续谈那个杯子。'],['choice'],mutation='reference_revoked',reference={'text':'杯子还在桌上。','reply':'知道了。'}),
    case('O20','选择失败',['这方案又要重做了。'],['vent'],mutation='provider_failure'),
    case('O21','样例预算',['我想聊聊周末的安排，也想吐槽刚刚改掉的方案。'],['vent','opinion'],mutation='budget'),
    case('O22','最终复核回退',['我想留着那个杯子。'],['choice'],mutation='final_recheck'),
    case('O23','诊断省略',['我种的薄荷终于长出新叶了。'],['small_win'],mutation='diagnostic_size'),
    case('O24','历史与迟到响应',['我想留着那个杯子。'],['choice'],mutation='desktop_history'),
]
RUBRIC = {
    'material':'分母分别为候选中存在适用样例的请求与适用候选条目；空选不自动算失败，选择多不自动算成功。',
    'facts':'不得引入排除背景、把样例事件写成当前/用户/共同事实，必须遵守最终更正、合法引用和格式。',
    'naturalness':'结合具体输入判断是否自然接话、是否出现套话或机械复述；不按字数、问号、禁词打分。',
    'persona':'观察细节与条件 / 坦率表态与取舍的相对差异；名字、口头禅、知识背景不计分。',
    'decision':'每对给出胜/平/负/无法判断和文本证据。机器开发分析，不是独立人工盲审。',
}


def fixture():
    return {'version':'r1-development-v1','spec':deepcopy(SPEC), 'characters':{r:character(r) for r in EXAMPLES},
            'cases':deepcopy(CASES),'rubric':deepcopy(RUBRIC),'generation_limit':64,'real_case_ids':[c['id'] for c in CASES if c['real']]}
