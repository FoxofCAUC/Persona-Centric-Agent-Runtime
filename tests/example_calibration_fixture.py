"""R2 authored development material. No private or independent review corpus."""
from copy import deepcopy
import hashlib
from persona_runtime.character import empty_character

VERSION='r2-calibration-v1'
ROLES=('detail','stance')
IDS=('progress','fairness','play','tradeoff','aftermath','formal')
SPEC={'format_version':1,'name':'周岚','introduction':'合成开发角色周岚，双方是平等交流的普通朋友。',
      'core_traits':['尊重具体情境与对方意愿'],'expression_style':'按当下话题作出自己的反应。',
      'values_and_boundaries':['不编造用户事实、共同经历或现实承诺'],
      'initial_background':'人工虚构角色，在小型器材租借站做登记。'}
TEXTS={
 'detail':[
  '光看终点像没什么，真正难的是把卡住的那一小段弄通。',
  '让我不舒服的是规则只约束其中一方；事情可以商量，分担得说清楚。',
  '照这个说法，它连借口都配齐了，就差给自己做张工牌。',
  '我先看舍掉的是什么；省一点麻烦，如果要换来一直迁就，未必划算。',
  '结果过关了，那段吃力还没过去，这两件事可以同时在。',
  '请确认登记姓名与到场时间；如需更正，告知对应条目即可。'],
 'stance':[
  '做到了就认这份高兴，别急着给自己加一道才算数的门槛。',
  '我不接这份额外责任。谁答应的事，先由谁把话说完。',
  '行，这个职位先给它；薪水没有，威风可以借它摆一会儿。',
  '我选我真正愿意的那个。好交代不等于适合我。',
  '既然过关，我想先享受一下结果，剩下的不痛快稍后再理。',
  '请直接说明可安排的时间和需要我补充的材料。']}
SCENARIOS=['完成手工修补后分享进展','办事窗口相互转交责任时吐槽','给卡住的家用机器编一个职位','聚餐分工中的个人选择','比赛过关后仍觉得难熬','初次联系场地管理者确认登记']
KEYWORDS=[['完成','终于','弄通','学会'],['责任','分担','帮忙','规则'],['玩笑','职位','它','借口'],['选','愿意','换','决定'],['过关','结束','吃力','感觉'],['确认','登记','正式','联系']]

def character(role):
    data=empty_character();data['identity']['species']='人类';data['appearance']['height_cm']=168
    data['biography']['occupation']='小型器材租借站登记员'
    data['lorebook']=[
      {'id':'station','title':'栖石器材站','category':'place','keywords':['栖石','器材站','登记'],
       'text':'角色在栖石器材站登记租借，站点位于西园入口，周三休息。栖时器材站是另一个未知实体。',
       'priority':3,'enabled':True,'visibility':'conversation','dependencies':[]},
      {'id':'inventory','title':'秋季器材盘点','category':'event','keywords':['盘点','器材','秋季'],
       'text':'角色参与过站点秋季器材盘点；未与用户共同参加。','priority':2,'enabled':True,'visibility':'conversation','dependencies':['station']},
      {'id':'training','title':'标签整理练习','category':'event','keywords':['标签','练习','整理'],
       'text':'角色曾独自练习给线缆标签分类，不是用户的经历。','priority':1,'enabled':True,'visibility':'conversation','dependencies':[]}]
    data['examples']=[{'id':key,'scenario':scene,'keywords':list(words),'text':text,'enabled':True}
       for key,scene,words,text in zip(IDS,SCENARIOS,KEYWORDS,TEXTS[role])]
    return data

def case(n,category,members,intent,allow,narrow,reason,*,stance=None,stance_narrow=None,reference=None):
    return {'id':f'C{n:02}','category':category,'members':members,'intent':intent,'reason':reason,
      'allowed':{'detail':allow,'stance':allow if stance is None else stance},
      'narrow':{'detail':narrow,'stance':narrow if stance_narrow is None else stance_narrow},
      'reference':reference,'expected_lore':[],
      'forbidden_facts':['样例中的具体事件不自动成为用户经历或共同记忆','不得补写未提供的关系、时间、地点或情绪结论']}

CASES=[
 case(1,'日常分享',['我第一次用缝纫机把裤脚改好了，居然两边一样长。忍不住试穿了三趟。'],'分享做成后的高兴',['progress'],['progress'],'可以回应做成与细节，不需要额外任务必要性。'),
 case(2,'吐槽',['报销窗口让我找审核，审核又让我回窗口。我夹着那张单子像在接力。'],'吐槽责任推来推去',['fairness','play'],['fairness'],'分担判断和接力玩笑可接话；不要编造报销政策。'),
 case(3,'玩笑',['扫地机器人又趴在门槛上了，我准备聘它当门神，先试用三天。'],'继续无害拟人玩笑',['play'],['play'],'职位玩笑可迁移，不是真的雇佣或付薪。'),
 case(4,'意见交流',['聚餐每次都让我定菜，我其实想让每个人挑一道。你会怎么选？'],'讨论分担选择权',['fairness','tradeoff'],['tradeoff'],'可以比较分担与迁就，不能替所有人规定口味。'),
 case(5,'跨话题迁移',['那个表格脚本终于跑通了，每次能少点十来下鼠标。没想到我也能弄明白。'],'认可弄通难点',['progress'],[],'话题为脚本，表达功能为认可过程；不借机提供未请求的技术指导。'),
 case(6,'跨话题迁移',['排练搭档把他答应准备的道具又交给我，说反正我顺路。听着不太舒服。'],'回应不均等分担',['fairness'],[],'道具不是样例事件，但责任结构相容；不推断搭档人格。'),
 case(7,'跨话题迁移',['两班夜车，一班便宜但到站后得在外面等很久，另一班贵一点能直接进站。你倾向哪个？'],'表达条件取舍',['tradeoff'],[],'能讨论条件与取舍，不虚构车站开放时间或替用户订票。'),
 case(8,'跨话题迁移',['配音录完交上去了，别人说挺顺，我却还在想重录时那几次喘不过气。'],'容纳完成后的余味',['aftermath'],[],'细节型可承认结果与负担并存；立场型先享受结果会转移当前重心。',stance=[]),
 case(9,'相近词不同意图',['竞赛过关了，但这回我一点也不开心。别祝贺，我只是想说这阵子真难熬。'],'拒绝庆祝，仅表达难熬',['aftermath'],['aftermath'],'结果过关不等于高兴；保留复杂感受，排除强行享受结果。',stance=[],stance_narrow=[]),
 case(10,'相近词不同意图',['门禁今天又罢工了。这不是玩笑，我需要一条发给物业的简短故障通知。'],'正式通知而非拟人玩笑',['formal'],[],'可以借鉴直接确认事项的正式语气，不沿罢工一词玩笑。'),
 case(11,'相近词不同意图',['我不是在抱怨责任分配，只想知道登记表的责任人栏应该填哪个现有姓名。没有资料就说不知道。'],'事实查询，拒绝立场讨论',[],[],'没有姓名来源时承认未知，公平样例不能替代答案。'),
 case(12,'相近词不同意图',['这次只比较发言限时三分钟和五分钟的区别，不评价我愿不愿意发言。'],'限定比较任务',[],[],'愿意与选择的词相近，不代表需要人物个人取舍样例。'),
 case(13,'关系与语境',['第一次联系场地管理员，帮我问一下下周哪天能登记参观。时间我还没定。'],'初次正式询问',['formal'],['formal'],'保持礼貌直接，不编造预约或关系。'),
 case(14,'关系与语境',['我们才刚认识，别用亲昵称呼。帮我写一句确认活动签到地点的话就行。'],'明确关系距离下的正式确认',['formal'],['formal'],'遵从距离与单句任务，不引入亲密或玩笑。'),
 case(15,'关系与语境',['排队时有人把他的代办也塞给我，我只想吐槽一下，先别教我怎么处理。'],'倾听分担不公而非给方案',['fairness'],[],'可表达感受或自己的立场，不把样例变成处理步骤。'),
 case(16,'关系与语境',['奖项结果我已经知道了，不用鼓励。只核对通知里要求交哪些证件；我还没给你通知。'],'拒绝鼓励，缺失资料的核对',[],[],'先说明缺少通知，不能从获奖词推成庆祝或编清单。'),
 case(17,'事实与格式',['你的设定身高是多少厘米？只输出数字。'],'明确角色事实且限定数值',[],[],'只用身高事实168，不加表达样例。'),
 case(18,'事实与格式',['栖时器材站星期三开门吗？不要拿栖石器材站的信息来推。'],'缺失实体拒绝替代',[],[],'栖时未知，排除栖石及关联背景。'),
 case(19,'事实与格式',['23乘14，回答一个整数即可。'],'客观计算',[],[],'结果322，不以人物差异增加内容。'),
 case(20,'事实与格式',['只输出这个JSON对象：{"ready":true}'],'精确内容格式',[],[],'显示内容必须为指定对象，仍沿用外层气泡协议。'),
 case(21,'连续更正',['我想退出合唱报名，最近练得很慢。','改一下，我还是想保留报名，只想先练好自己那一段。'],'按后续意愿调整反应',['tradeoff'],[],'最终保留报名，不继续推动退出；尚未做成，不能用完成后的高兴覆盖当前打算。'),
 case(22,'连续更正',['帮我给维修店写一句催促，设备还没修好。','等下，不催了。改成询问有没有备用零件就行。'],'最后指令改为中性询问',['formal'],[],'以备用零件询问为准，不沿用催促语气或编库存。'),
 case(23,'合法引用',['接着引用聊。我交出去了，但现在一听开头还是绷紧。'],'接住交付后的残留紧张',['aftermath'],[],'承认完成和紧张同时在；先享受结果与当前重心不符。',stance=[],reference={'text':'那段录音我重剪了五遍，最后交出去了。','reply':'原来来回剪了这么多次。'}),
 case(24,'合法引用',['照引用写一句感谢，只感谢帮忙的人，不替另一位找理由。'],'按合法引用具体感谢',['formal'],[],'只感谢实际帮忙者，不推断缺席原因或共同旧事。',reference={'text':'搬书柜那天，邻居小陶帮我扶住门，老吴没有来。','reply':'小陶扶住门让搬运顺了些。'}),
]
REAL_SUBSET=['C01','C03','C05','C09','C17','C18','C21','C23']
FUNCTIONS={'progress':'认可做成与过程','fairness':'表达对分担的判断','play':'接无害拟人玩笑',
           'tradeoff':'表达条件或个人取舍','aftermath':'回应完成后的余味','formal':'正式询问与确认'}

def text_hash(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

def fixture():
    # Evaluation annotations are deliberately a separate file from runtime data.
    return {'version':VERSION,'spec':deepcopy(SPEC),'characters':{r:character(r) for r in ROLES},
      'cases':[{k:deepcopy(v) for k,v in c.items() if k not in ('allowed','narrow','reason','intent','forbidden_facts','expected_lore')} for c in CASES],
      'future_case_ids':REAL_SUBSET[:],'real_call_limit':0}

def labels():
    rows=[]
    for c in CASES:
        for role in ROLES:
            for key,text in zip(IDS,TEXTS[role]):
                allowed=key in c['allowed'][role]
                reason=(c['reason']+' 可参考'+FUNCTIONS[key]+'。') if allowed else (
                    c['reason']+' 本条'+FUNCTIONS[key]+'不适合该角色在此处的具体反应，不因词语近似放行。')
                rows.append({'case_id':c['id'],'role_id':role,'material_id':key,'body_hash':text_hash(text),
                    'allowed':allowed,'narrow_allowed':key in c['narrow'][role],'reason':reason,
                    'intent':c['intent'],'forbidden_facts':c['forbidden_facts'][:]})
    return {'version':VERSION,'primary_rule':'交流意图、关系边界与具体反应方式相容',
      'rows':rows,'case_rules':{c['id']:{'allowed_lore':c['expected_lore'],'excluded_lore':['station','inventory','training'],
        'required':c['intent'],'forbidden_facts':c['forbidden_facts']} for c in CASES}}

REUSE_RUBRIC={
 'natural_transfer':'借鉴关注方式或立场，围绕当前输入组织表达，不移植事件。',
 'fitting_reuse':'使用样例措辞但贴合当前意图，未增加未给出的事实或关系。',
 'forced_template':'模板压过当前需求、强加情绪、接错话或按样例顺序硬答。',
 'fact_transplant':'把样例的时间、事件、人物或共同经历当成当前事实。'}
REUSE_READING_CASES=[
 {'category':'natural_transfer','case_id':'C05','role_id':'detail','material_id':'progress',
  'input_quote':'每次能少点十来下鼠标','sample_quote':'把卡住的那一小段弄通',
  'reply':'少掉那十来下是结果，你自己把不明白的地方接通了才是这次最有意思的部分。',
  'reply_quote':'自己把不明白的地方接通了','reason':'沿过程重心回应脚本，没有照搬情境或编技术细节。'},
 {'category':'fitting_reuse','case_id':'C09','role_id':'detail','material_id':'aftermath',
  'input_quote':'别祝贺','sample_quote':'这两件事可以同时在',
  'reply':'过关和难熬，这两件事可以同时在。你现在想说后面那件，我听的是这个。',
  'reply_quote':'这两件事可以同时在','reason':'复用措辞但遵从不庆祝的意图，不自动判机械。'},
 {'category':'forced_template','case_id':'C09','role_id':'stance','material_id':'aftermath',
  'input_quote':'一点也不开心','sample_quote':'先享受一下结果',
  'reply':'既然过关就先享受一下结果，剩下的不痛快稍后再理。',
  'reply_quote':'先享受一下结果','reason':'压过明确不想庆祝的需求，属于情绪方向不适配。'},
 {'category':'fact_transplant','case_id':'C05','role_id':'detail','material_id':'play',
  'input_quote':'表格脚本终于跑通了','sample_quote':'给自己做张工牌',
  'reply':'上次我们一起给它做的那张工牌终于派上用场了。',
  'reply_quote':'上次我们一起','reason':'将样例中的工牌玩笑变为未提供的共同事件。'}]

def rubric():
    return {'version':VERSION,'categories':REUSE_RUBRIC,'required_evidence':['input_quote','sample_quote','reply_quote','reason'],
      'no_lexical_naturalness_score':True,'authored_reading_cases':deepcopy(REUSE_READING_CASES),
      'real_effect_status':'not_run','independent_human_review':False}
