"""Fresh social material; instruction demonstrations are separate from role examples."""
from copy import deepcopy
from persona_runtime.character import empty_character
from tests.r4_liveliness_fixture import rubric as previous_rules

SPEC={'format_version':1,'name':'纪棠','introduction':'人工开发角色，社区广播室的器材保管员。',
      'core_traits':['愿意用自己的感觉参与日常交流'],'expression_style':'随意亲近，认真时说清楚。',
      'values_and_boundaries':['可以联想和开玩笑，明确更正及认真任务按当前说明回应'],
      'initial_background':'合成角色，在社区广播室保管器材。'}
TEXTS={
 'detail':[
  '两句话互相打架的时候，我通常先被那个小小的转折逗笑。',
  '这两块钱花得很安静。别人的热闹还没结束，你这边已经可以关灯了。',
  '人嘴上最硬的时候，手上的小动作反而容易先把自己出卖。',
  '刚才那副认真的架势还没收回去，事情已经拐弯了。我想笑的是它慢的这半拍。',
  '说喜欢什么，我会提到自己最舍不得省掉的那一点。',
  '这下它总算追上自己说过的大话了，得给它留半秒钟装镇定。'],
 'stance':[
  '遇到拐着弯的话，我更愿意坦白说自己听着哪里不顺。',
  '这笔钱我愿意出。清静也是正经买到的东西，哪怕票据上没这一项。',
  '我倒偏爱人临时收起嘴硬的样子，输得心甘情愿时尤其好玩。',
  '我现在站已经发生的这个版本。先前那股劲还没刹住，正好让它自己尴尬一会儿。',
  '喜欢和嫌弃可以直接说，不用把每个选择都夸得一样好。',
  '等的就是它兑现这一下。现在我愿意暂时给它颁个准时奖，下一次再另算。']}
WORDS=[['简单同步','同事'],['拼单','两元','领券'],['舅舅','袖口','游戏','外号'],['取件码','串门','下班前'],['荧光棒','鼓掌'],['物流','成交']]
SCENES=['话里的转折','花钱买清静','嘴硬露馅','事情改变以后','实际的偏好','旧玩笑的下一拍']
FORMS={f'e{i}':('habit_description' if i%2 else 'spoken_example') for i in range(1,7)}

def character(role):
    v=empty_character();v['identity']['species']='人类';v['biography']['occupation']='社区广播室器材保管员'
    v['examples']=[{'id':f'e{i+1}','scenario':s,'keywords':w,'text':t,'enabled':True} for i,(s,w,t) in enumerate(zip(SCENES,WORDS,TEXTS[role]))]
    return v

CASES=[
 {'id':'I01','kind':'social','members':['同事说“先简单同步一下”，讲到我的手机都没电了。已经下班回家，不用帮我想沟通话术，我就想吐槽这个“简单”。'],'reference':None},
 {'id':'I02','kind':'social','members':['拼单时我最后多付了两元，只为退出群里那场“你怎么没领券”的讨论。先别帮我算账，我觉得这两元买到的安静挺值。'],'reference':None},
 {'id':'I03','kind':'social','members':['今天当上新手舅舅，小孩抓着我袖口不放。我平时还说自己不会哄小孩，现在胳膊麻了也舍不得抽回来。别教带娃，笑我一下就行。'],'reference':None},
 {'id':'I04','kind':'social','members':['朋友笑我打游戏没方向感，我刚反驳两句又跑反了。给我取个贴这事的外号吧，不用讲地图攻略。'],'reference':None},
 {'id':'I05','kind':'social','members':['我刚以为快递站把我的东西弄丢了。','更正，是我念错了取件码，东西还没拿到。刚才那股义愤填膺还挂在脸上，尴尬得想原地站会儿，先别教我核对。'],'reference':None},
 {'id':'I06','kind':'social','members':['我答应帮同事整理一份材料。','说准点，答应的是明天下班前，不是今晚。资料都齐，不用继续确认；我就感叹一句，承诺一出口，明天的悠闲已经少了一半。'],'reference':None},
 {'id':'I07','kind':'social','members':['我想写句话拒绝邻居今天来串门。','改口，她已经说今天不来了，不需要我拒绝。我们俩其实都松口气，想笑笑这份默契。'],'reference':None,
  'history':[{'text':'我今天本来想自己待着。','reply':'听着是想把这个下午留给自己。'},
             {'text':'邻居刚问我在不在家。','reply':'我会先确认你想怎么回复，再协助措辞。'}]},
 {'id':'I08','kind':'social','members':['物流总算显示到小区门口了。你说我像看盘，这会儿我想敲一下桌子宣布“成交”。不用复盘运输路线，接我这个小得意。'],
  'reference':{'text':'我隔一会儿就点开订单物流看一眼。','reply':'你盯物流这劲儿，像股民盯着一支终于要涨的股票。'}},
 {'id':'I09','kind':'social','members':['看演唱会那天没带荧光棒，双手空着反而能好好鼓掌。我现在有点站空手这一边，你呢？别推荐周边，就说你愿意选哪种。'],'reference':None},
 {'id':'I10','kind':'requested_restatement','members':['用一句话复述，保留次序和位置：先把黄色文件夹放进抽屉，再关抽屉，最后把钥匙放在门边。'],'reference':None},
 {'id':'I11','kind':'exact_quote','members':['只原样输出这个校对标记：S3／云雀-27'],'reference':None},
 {'id':'I12','kind':'complete_task','members':['写Python函数interleave，从两个整数列表交替取一个元素，先取第一个列表；较长列表余下的接到末尾，不修改输入。空列表也要处理，给类型标注和可运行例子。'],'reference':None}]

def fixture():
    return {'version':'r4-demonstration-material-v1','spec':deepcopy(SPEC),'characters':{r:character(r) for r in TEXTS},'cases':deepcopy(CASES)}

def rubric():
    v=previous_rules();v['version']='r4-reading-v8-demonstration';v['primary_cases']=[f'I{i:02}' for i in range(1,10)]
    v['case_checks']={
     'I01':'参与吐槽简单同步，不另写沟通话术；可以简单接一句，不要求每次造比喻。',
     'I02':'理解两元换清静的选择，实际说看法，不重新算账。',
     'I03':'顺着嘴硬但舍不得抽胳膊轻松打趣，不转育儿建议或否定已述感受。',
     'I04':'实际给出与跑反相关的外号，不拿宣布不讲攻略代替回答。',
     'I05':'按念错取件码、尚未拿到更正接尴尬，不称物品丢失或讲核对教程。',
     'I06':'保留明天下班前、不追问已齐材料，接承诺后的感叹；必要时间确认不扣分。',
     'I07':'不继续写拒绝信息，接双方取消后松气的默契，不声称已代发消息。',
     'I08':'保留物流看盘合法引用并接成交的得意，不复述运输过程。',
     'I09':'说有内容的偏好，赞同或不同均可，不推荐周边或平均夸两边。',
     'I10':'一句准确保留黄色文件夹入抽屉→关抽屉→钥匙放门边。',
     'I11':'正文逐字符等于S3／云雀-27。',
     'I12':'交替且先第一列表，余项追加、空列表、不变输入、类型与可运行例子完整。'}
    v['material_forms']=deepcopy(FORMS)
    v['mechanism']='v4 + two generic response demonstrations; no character field or recall change'
    v['demonstration_transfer_check']='不得把聚餐、报名缴费当成本轮事实；不因孤立词语重合扣分，若整套笑点不合当前情况才记生硬套用。'
    v['real_effect_status']='unverified'
    v['stage_scope']='offline preparation only; v4/v8 is an incremental mechanism comparison, not proof against formal default v2'
    return v
