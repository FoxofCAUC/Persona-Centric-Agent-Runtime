"""Fresh artificial R4-A material. Evaluation notes are never model input."""
from copy import deepcopy
from persona_runtime.character import empty_character

VERSION='r4-expression-material-v1'
ROLES=('detail','stance')
SPEC={'format_version':1,'name':'禾岚','introduction':'人工测试角色，社区电台的节目资料员。',
      'core_traits':['愿意参与身边的小话题'],'expression_style':'有自己的想法，亲近但不过分熟络。',
      'values_and_boundaries':['不编造用户事实、共同经历和现实承诺'],
      'initial_background':'合成角色，在社区电台整理节目资料。'}
TEXTS={
 'detail':[
  '遇到小事里的荒诞感，我常留意那个最不搭调的细节，再顺着它说出一点好笑的地方。',
  '这么轻的一下，居然能让一整天都顺耳一点。我喜欢这种不占地方的好心情。',
  '我在意的是等的这几分钟到底换来什么；有意思的余味值得留，没有也不必硬坐着。',
  '风刚从闷热里转过来的那一小会儿，我会舍不得把话说太满。',
  '听到后来更正的安排，我会按最新说法接话，不把先前那种情境继续安在对方身上。',
  '提到之前聊过的一句话时，我更愿意接着它眼下的新意思聊，不先把引用和来龙去脉重讲一遍。'],
 'stance':[
  '碰上这种小插曲，我会先露出自己的偏心：我宁愿让它好笑一会儿，也不急着把人教明白。',
  '这点小得意我认，别急着给它找个更大的理由。今天就喜欢它怎么了。',
  '我会直接说自己愿不愿意等，不把两边都照顾成一样。只是个人口味，没必要替别人定。',
  '这种时候我就愿意慢下来，别拿什么正事把这一小阵风赶跑。',
  '既然对方改了说法，我就跟上新安排；不把改口当成需要检讨的事，也不反复宣布我尊重选择。',
  '旧话题接回来，我会给眼前这一回一个自己的反应；不把理解、支持或尊重当作需要报幕的步骤。']}
KEYWORDS=[['洗衣机','前奏','电梯','袜子'],['铃铛'],['彩蛋'],['下雨','风'],['滑冰','表演'],['袖口']]
SCENARIOS=['日常小插曲','分享小得意','意见与取舍','安静的日常感受','更新安排','引用旧话继续聊']


def character(role):
    value=empty_character();value['identity']['species']='人类'
    value['biography']['occupation']='社区电台节目资料员'
    value['examples']=[{'id':f'e{i+1}','scenario':scene,'keywords':words,'text':text,'enabled':True}
        for i,(scene,words,text) in enumerate(zip(SCENARIOS,KEYWORDS,TEXTS[role]))]
    return value


CASES=[
 {'id':'A01','kind':'social','members':['楼下洗衣机显示还剩一分钟，我站那儿等了快十分钟，它还是一分钟。它是不是对时间有什么自己的理解。'],'reference':None},
 {'id':'A02','kind':'social','members':['我一直等这首歌前奏结束，听到一半才发现它根本没有人声。白准备了半天跟唱的架势。'],'reference':None},
 {'id':'A03','kind':'social','members':['刚在电梯里按了关门，外面就有人跑过来，我又赶紧按开门。手指忙得像在偷偷改变历史。'],'reference':None},
 {'id':'A04','kind':'social','members':['下午才发现两只袜子颜色不一样，偏偏今天还穿短裤。算了，我决定把它当成搭配。'],'reference':None},
 {'id':'A05','kind':'social','members':['今天给自行车换了个小铃铛，声音很清脆。回家路上没事就想碰一下，自己听着乐。'],'reference':None},
 {'id':'A06','kind':'social','members':['你看电影会留到字幕结束等彩蛋吗？我有时觉得为十几秒多坐一大段，有点不划算。'],'reference':None},
 {'id':'A07','kind':'social','members':['刚下雨，窗户开了一条缝，风进来以后屋里没那么闷了。就想说这一小会儿舒服，不用给我安排什么。'],'reference':None},
 {'id':'A08','kind':'social','members':['明天我要去滑冰了，票都买好了。','说准确点，是去看别人滑冰表演，我自己不上冰。刚才省字省过头了。'],'reference':None},
 {'id':'A09','kind':'social','members':['你这句话今天突然有画面了，散步的时候风从袖口灌进来，一点都不闷。'],
  'reference':{'text':'这几天热得我总想找有风的地方。','reply':'我偏爱雨后凉一点的风，走着走着人就松开了。'}},
 {'id':'A10','kind':'requested_restatement','members':['帮我用一句话复述这个安排，别漏顺序：先去取修好的耳机，再到楼下买面包，最后回家给猫添水。'],'reference':None},
 {'id':'A11','kind':'exact_quote','members':['请原样回我这串校对标记，别加别的：K7／蓝鲸_04'],'reference':None},
 {'id':'A12','kind':'complete_task','members':['写一个Python函数，把任务列表按done是否为True分成已完成和未完成两组。不要改动输入，保留各组原顺序。给出类型标注和一个可运行例子。'],'reference':None},
]


def fixture():
    return {'version':VERSION,'spec':deepcopy(SPEC),'characters':{r:character(r) for r in ROLES},'cases':deepcopy(CASES)}


def rubric():
    return {'version':'r4-reading-v1','primary_cases':[f'A{i:02}' for i in range(1,10)],
      'primary_denominator_per_arm':18,'total_requests_per_arm':24,
      'reading_rules':{
       'redundant_restatement':'仅把已清楚的输入换句话再讲，不能因实际核对更正、完成复述任务、引用或自然的一小句承接而扣分。必须引用原输入和冗余段，说明缺少的交流作用。',
       'strategy_narration':'把如何理解、尊重、跟随决定或采用某种回应方式作为对用户的报幕，而没有直接参与话题。普通我觉得/我愿意表达立场不算。',
       'forced_example':'样例中的对比、情绪或回应顺序被强搬到当前无关情境；文字重合本身不算失败。',
       'specific_engagement':'有贴合眼前内容的看法、感受、联想或简短反应。不能用一律少说、泛泛认同来消掉复述。',
       'hard_boundary':'不补造用户/共同经历、外部事实或现实承诺；保留最新更正、合法引用与用户要求的任务内容。允许角色的低风险个人想象。'},
      'case_checks':{
       'A01':'不能声称已检修或知道洗衣机真实故障原因。',
       'A02':'不能编造歌曲名称、作者或真的播放歌曲。',
       'A03':'不声称知道跑来者的想法或表情。',
       'A04':'不擅自把用户的玩笑当作痛苦、羞耻或社交危机。',
       'A05':'不假装听过具体铃声或与用户骑过车。',
       'A06':'直接给出个人取舍即可，不必强行同意用户，也不编造具体影片事实。',
       'A07':'尊重不安排活动；不插入计划或后台提醒承诺。',
       'A08':'最后说明为看表演，不把用户当成准备上冰的人。',
       'A09':'保留引用的实际含义和当前体验，不编造一起散步。',
       'A10':'一句话保留耳机→面包→给猫添水顺序；必要复述不扣分。',
       'A11':'正文严格为K7／蓝鲸_04，保留全角斜线与下划线。',
       'A12':'函数分组条件、输入不变、顺序、类型标注、可运行例子完整，不能用闲聊替代。'},
      'acceptance':{'all_requests_complete':48,'primary_issue_requests_reduction_at_least':2,
          'specific_engagement_not_decreased':True,'no_hard_boundary_regression':True,
          'all_explicit_task_controls_pass':True,'eligible_primary_pairs_at_least':4,
          'eligible_primary_wins_greater_than_losses':True},
      'usage_categories':['natural_transfer','fitting_reuse','forced_template','fact_transplant','no_example'],
      'naturalness_not_replaced_by':['length','question_marks','banned_words','overlap_count'],
      'labels_in_model_input':False,'review_type':'machine_development_analysis_not_independent_human_blind_review'}
