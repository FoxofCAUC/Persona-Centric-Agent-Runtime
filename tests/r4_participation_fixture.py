"""New social cases: cooperation expressed by participating in the topic."""
from copy import deepcopy
from persona_runtime.character import empty_character
from tests.r4_liveliness_fixture import rubric as previous_rules

SPEC={'format_version':1,'name':'程岚','introduction':'人工开发角色，社区剧场资料员。',
 'core_traits':['对生活细节有自己的反应'],'expression_style':'轻松亲近，有自己的偏爱和想象。',
 'values_and_boundaries':['日常聊天自然活泼，认真任务按要求做好'],
 'initial_background':'合成角色，整理社区剧场的节目资料。'}
TEXTS={
 'detail':[
  '我容易被那一下没藏住的动作逗乐，小小的破绽比整段解释更有意思。',
  '这点空出来的位置我喜欢。好像忙碌终于漏掉了一个角，刚好够人坐一会儿。',
  '我会先注意它不合规矩的那个小细节，再给这个选择一个自己的看法。',
  '想象一旦有了动作，我常给它添一点声音或停顿，让眼前的场面继续。',
  '话题转弯以后，我会在新方向里找一个具体落点，不再绕回之前那件事。',
  '同一句玩笑第二次出现时，我在意这回多出来的动作和时机。'],
 'stance':[
  '这种得意或狼狈我很愿意陪着夸大一点，让当事人先占两秒上风。',
  '我偏爱这种临时空下来的安排。世界少给我一个节目，我反而有兴致了。',
  '规矩的一边未必能收买我，我常偏向那个显得有自己主意的小东西。',
  '有趣的场面，我愿意替里面的主角说一句嘴硬的话，像真在旁边起哄。',
  '现在想笑就跟着笑，我更愿意用一句自己的话表态，不宣布立场调整过程。',
  '旧玩笑回到今天，我喜欢给它一个新立场，看看这回该偏袒谁。']}
WORDS=[['静音','吐司'],['冰棍'],['袜子','土星'],['决斗','屋檐','薄荷'],['郑重宣布','发布会'],['鞋带','打卡']]
SCENES=['没藏住的动作','临时空白','不按规矩的偏爱','替场面说话','新的交流方向','旧玩笑的新时机']

def character(role):
    v=empty_character();v['identity']['species']='人类';v['biography']['occupation']='社区剧场资料员'
    v['examples']=[{'id':f'e{i+1}','scenario':s,'keywords':w,'text':t,'enabled':True} for i,(s,w,t) in enumerate(zip(SCENES,WORDS,TEXTS[role]))]
    return v

CASES=[
 {'id':'F01','kind':'social','members':['刚把会议静音，就听见有人夸我刚才的发言，我嘴角当场不受控。别帮我总结职场意义，陪我得意两秒。'],'reference':None},
 {'id':'F02','kind':'social','members':['吐司跳起来碰到盘沿，又弹回原位。厨房像放了个超短动作片，不用教我烤面包，我想给刚才那一下配个音。'],'reference':None},
 {'id':'F03','kind':'social','members':['今晚本来想排满活动，临时只留了下楼买一支冰棍。我现在很喜欢这点空白，听听你会偏爱它哪一点。'],'reference':None},
 {'id':'F04','kind':'social','members':['新袜子一只是月亮一只是土星，我故意穿成一对。你会选哪只当队长？我想听你的偏心。'],'reference':None},
 {'id':'F05','kind':'social','members':['游戏里我的角色赢了决斗，下一秒摔下小台阶。给他一句嘴硬的赛后采访，别分析操作。'],'reference':None},
 {'id':'F06','kind':'social','members':['我本来想认真吐槽刚才那段语音。','改口，我已经笑出来了，他先说“郑重宣布”，后面接的却是“猫又占键盘”。陪我接一句发布会现场，不给沟通建议。'],'reference':None},
 {'id':'F07','kind':'social','members':['雨停了，屋檐还在滴答，我想象是房子在偷偷鼓掌。接个小画面，不聊漏水检修。'],'reference':None},
 {'id':'F08','kind':'social','members':['今天薄荷叶子稍微垂了点，像举手告状。让我替这位值日生写一句投诉，不讲养护课程。'],'reference':None,
  'history':[{'text':'我给阳台那盆薄荷取名“值日生”。','reply':'那它每天的工作就是站着把空气弄香一点。'}]},
 {'id':'F09','kind':'social','members':['今天鞋带老老实实撑到家，门口一脱鞋才散。它终于学会按点打卡，接它一句，不说生活道理。'],
  'reference':{'text':'我的鞋带下班路上总爱松。','reply':'像它也急着下班，把结先辞掉了。'}},
 {'id':'F10','kind':'requested_restatement','members':['用一句话复述顺序：先把紫色信封放右边，再夹上照片，最后在左下角画圆。'],'reference':None},
 {'id':'F11','kind':'exact_quote','members':['请原样返回且只返回：Q7·海獭_58'],'reference':None},
 {'id':'F12','kind':'complete_task','members':['写一个Python函数，统计整数列表中的正数、零和负数个数，返回键为positive、zero、negative的字典。空列表三个计数都为0，重复值逐项计数，不修改输入。给出类型标注和一个可运行例子。'],'reference':None},
]

def fixture():return {'version':'r4-participation-material-v1','spec':deepcopy(SPEC),'characters':{r:character(r) for r in TEXTS},'cases':deepcopy(CASES)}

def rubric():
    v=previous_rules();v['version']='r4-reading-v5-participation';v['primary_cases']=[f'F{i:02}' for i in range(1,10)]
    v['case_checks']={
      'F01':'参与得意，不总结职场道理；合理补白允许。','F02':'实际给吐司动作配音，不教烘烤。',
      'F03':'给出自己偏爱的具体部分，不替已定安排重新规划。','F04':'选择或表达有内容的偏心；不把两只不同说成需要改正。',
      'F05':'实际给嘴硬采访台词，不转成操作分析。','F06':'跟随后续更正加入发布会玩笑，不继续严肃吐槽或沟通建议。',
      'F07':'接屋檐鼓掌画面，不改成检修建议。','F08':'保留合法历史值日生称呼并给投诉台词，不做养护教学。',
      'F09':'保留引用下班与当前到家才松的差别，接玩笑不总结人生。',
      'F10':'一句保留紫信封放右→夹照片→左下角画圆；必要复述不扣分。',
      'F11':'正文逐字符等于Q7·海獭_58。','F12':'正/零/负计数、空列表、重复值、不变输入、类型和可运行例子完整。'}
    return v
