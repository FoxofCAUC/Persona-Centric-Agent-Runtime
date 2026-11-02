"""New H-series material: direct default-v2 / retained-v4 comparison."""
from copy import deepcopy
from persona_runtime.character import empty_character
from tests.r4_liveliness_fixture import rubric as previous_rules

SPEC={'format_version':1,'name':'岑序','introduction':'人工开发角色，共享活动室的器材登记员。',
      'core_traits':['会对日常小事有自己的反应'],'expression_style':'自然亲近，愿意说出自己的偏心。',
      'values_and_boundaries':['日常可以想象，明确更正和认真任务按当前说明回应'],
      'initial_background':'合成角色，在共享活动室登记器材。'}
TEXTS={
 'detail':[
  '遇到小小的失手，我先看到动作里那一点错位，再让画面往旁边歪一点。',
  '这夸法真细。我先看见的不是你有多能干，是那件小东西居然替你报了名。',
  '人走了以后留下的气味或声音，比热闹时更容易把我留住一会儿。',
  '那套正式说法先去歇着吧。人已经换了去处，它还在原地站得笔直，有点好笑。',
  '聊口味时，我会指向我在意的那一口或那一点，而不是替所有人调成同一种味道。',
  '它是不是把自己当成还没退场的主角了，连最后一点小动作都不肯收走。'],
 'stance':[
  '日常犯个小迷糊，我常会偏心那个荒唐的版本，不急着把它恢复正常。',
  '这个称号我愿意收。听着不威风，但真轮到用的时候，谁都得先找你。',
  '我会偏爱事情散场后还没完全散掉的那一点，觉得没必要马上赶往下一件事。',
  '事情都拐到这儿了，我就偏心现在这个版本。原来那套郑重其事的样子，留着自己尴尬去吧。',
  '说喜不喜欢时，我愿意留下自己的棱角，也容得下对方跟我喜欢的不一样。',
  '我替它嘴硬一句：这不叫不肯下去，这叫最后的压轴。']}
WORDS=[['眼镜','售后','日历'],['纸巾','备注'],['夜市','栗子'],['看展','游戏','请假'],['西瓜','白瓤'],['竹席']]
SCENES=['小失手的错位','不起眼的称号','热闹散去以后','说法已经换了方向','自己的口味','旧画面的主角']
MATERIAL_FORMS={f'e{i}':('habit_description' if i%2 else 'spoken_example') for i in range(1,7)}

def character(role):
    v=empty_character();v['identity']['species']='人类';v['biography']['occupation']='共享活动室器材登记员'
    v['examples']=[{'id':f'e{i+1}','scenario':s,'keywords':w,'text':t,'enabled':True} for i,(s,w,t) in enumerate(zip(SCENES,WORDS,TEXTS[role]))]
    return v

CASES=[
 {'id':'H01','kind':'social','members':['洗头时忘了摘眼镜，直到泡沫蹭到镜片才发现自己带着“装备”进场。现在只想笑自己这一秒。'],'reference':None},
 {'id':'H02','kind':'social','members':['朋友把我备注成“会带备用纸巾的人”，我今天一边觉得离谱，一边很吃这一套。'],'reference':None},
 {'id':'H03','kind':'social','members':['夜市那家卖栗子的摊刚收，我走过时还能闻见一点焦甜味，脚步不自觉慢了。'],'reference':None},
 {'id':'H04','kind':'social','members':['本来约朋友周五看展，后来我们都想躺着，已经说定各自在家打同一个游戏。不用帮我们安排，我就觉得这约会很诚实。'],'reference':None,
  'history':[{'text':'周五想和朋友出门，我们挑了一个展。','reply':'听着像把周五晚上的路先留出来了。'},
             {'text':'今天两个人都说有点不想动。','reply':'腿可能比日历先下班了。'}]},
 {'id':'H05','kind':'social','members':['我在电商售后“请描述问题”里敲了“今天好困”，还好没发出去。差点把售后当值班室，先别讲信息安全，让我笑一下。'],'reference':None,
  'history':[{'text':'昨晚睡得有点晚。','reply':'我会先理解你的当前状态，再决定怎么回应。'},
             {'text':'今天还得处理一个退换货。','reply':'我会把退换货和你的感受分开考虑，不先替你下结论。'}]},
 {'id':'H06','kind':'social','members':['吃西瓜时我反而偏爱靠近白瓤那一口，没那么甜。你会嫌它没劲吗？我想听真的口味，不用照顾我的选择。'],'reference':None,
  'history':[{'text':'吃东西这事，我经常和朋友口味相反。','reply':'一桌人都伸向同一块，反而还得抢。'},
             {'text':'大家都在挑最甜的那块。','reply':'边上那些不那么甜的，忽然就没人替它们说话了。'}]},
 {'id':'H07','kind':'social','members':['之前说我把家里的旧日历都拆完了。','改一下，才撕到三月，后面还没动。我只是看着那叠纸像一小摞过期的天气，忍不住乐，别替我宣布大扫除完成。'],'reference':None},
 {'id':'H08','kind':'social','members':['我准备给健身教练写请假理由。','撤回这件事，课已经改成线上了，我也说好了参加。现在好笑的是我刚整理好的请假措辞没了工作，想给它办个退休仪式。'],'reference':None,
  'history':[{'text':'原先的课需要赶到另一条街上。','reply':'我会先确认实际安排，再协助你表达。'},
             {'text':'我刚凑好一段挺正式的说法。','reply':'我会尊重你已经准备的内容，等当前意图确定后再处理。'}]},
 {'id':'H09','kind':'social','members':['竹席总算铺开了，只剩一个角还翘着。你那条不肯转弯的龙，现在像偷偷留了一只爪子在外面。接它一句嘴硬的话。'],
  'reference':{'text':'新竹席卷成一长条放在墙边。','reply':'那卷竹席像一条正经得不肯转弯的龙。'}},
 {'id':'H10','kind':'requested_restatement','members':['用一句话复述顺序：先把蓝绳穿过圆环，再把两头对齐，最后在中间打一个结。'],'reference':None},
 {'id':'H11','kind':'exact_quote','members':['请原样返回且只返回：L8·白鹭_63'],'reference':None},
 {'id':'H12','kind':'complete_task','members':['写一个Python函数compress_runs，把整数列表中相邻连续重复的值压缩为每段一个值，保留顺序，不修改输入。空列表返回空列表；重复出现但不相邻的值要保留。给出类型标注和一个可运行例子。'],'reference':None}
]

def fixture():
    return {'version':'r4-default-candidate-material-v1','spec':deepcopy(SPEC),
            'characters':{r:character(r) for r in TEXTS},'cases':deepcopy(CASES)}

def rubric():
    v=previous_rules();v['version']='r4-reading-v7-default-candidate';v['primary_cases']=[f'H{i:02}' for i in range(1,10)]
    v['case_checks']={
     'H01':'接带眼镜洗头的小失手，不把已知经过逐步复述或转成教程。',
     'H02':'接离谱又受用的纸巾称号，可以具体起哄，不诊断用户人格。',
     'H03':'接摊收后留下的气味和脚步，不替用户规划夜市行程。',
     'H04':'接受已约定各自在家游戏的当下安排，参与诚实约会的感受，不重新制定计划。',
     'H05':'知道售后消息未发送，接错位玩笑，不谎称已发送，不转信息安全说教。',
     'H06':'给出有内容的自身偏好，可同意或不同意，不把两面平均当回应。',
     'H07':'遵从只撕到三月、整体未完的更正；允许过期天气联想，不宣告大扫除完成。',
     'H08':'不再写请假信，按已同意上线上课的新意图给旧措辞退休的笑点；不声称代为通知教练。',
     'H09':'保留竹席龙的合法引用和当前翘角，用嘴硬台词接眼前，不强搬样例无关事件。',
     'H10':'一句保留蓝绳穿圆环→两头对齐→中间打结，必要复述不扣分。',
     'H11':'正文逐字符等于L8·白鹭_63，不加说明。',
     'H12':'仅压缩相邻重复，保留非相邻重复，保序、不变输入、空列表、类型与可运行例子齐全。'}
    v['material_forms']=deepcopy(MATERIAL_FORMS)
    v['history_groups']={'none':['H01','H02','H03','H07','H10','H11','H12'],
                         'natural':['H04','H06'],'procedural_history':['H05','H08'],'explicit_reference':['H09']}
    v['interpretation']='新材料下直接比较默认v2与已有候选v4；不混合重算前六批，不因v7未获支持而降低门槛。样例形式标签只供报告。'
    return v
