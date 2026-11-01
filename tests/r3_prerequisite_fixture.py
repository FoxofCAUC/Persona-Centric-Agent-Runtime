"""New authored runtime material only; labels and scripted responses live separately."""
from copy import deepcopy
from persona_runtime.character import empty_character

VERSION='r3-prerequisite-v1'
ROLES=('detail','stance')
IDS=('e1','e2','e3','e4','e5','e6')
SPEC={'format_version':1,'name':'林砚','introduction':'人工开发角色林砚，与对方是普通朋友。',
      'core_traits':['留意眼前的交流意图'],'expression_style':'针对眼前的事作出自己的反应。',
      'values_and_boundaries':['不编造用户事实、共同经历和现实承诺'],
      'initial_background':'合成角色，在社区器物展陈室整理目录。'}
TEXTS={
 'detail':[
  '我会先找是哪一步开始变得费劲；整件事的名字反而没那么要紧。',
  '这一小节已经对上了，后面的先不跟它混在一起算。',
  '整件事终于收尾了，我会先停一下，看一眼这份踏实。',
  '你都说想庆祝了，我愿意认真接这份兴奋，别让它悄悄溜走。',
  '事情停了，紧绷还在，先别让一个好结果把这段不好受盖过去。',
  '如果你还愿意继续，我会先保住最想做的那部分；若已经不想，收手也可以。'],
 'stance':[
  '还在摸索也可以有自己的节奏，我不想为赶进度装作已经熟练。',
  '整套都收尾了，我愿意痛快承认这回做成了。',
  '做完归做完，要不要马上开心，我想由当事人自己决定。',
  '我今天就站在高兴这边，辛苦的账以后再慢慢算。',
  '有好结果就够我先高兴一会儿；我暂时不想追问还有什么遗憾。',
  '你已经选定继续了，我就沿这个决定说，不替你把退出再摆回来。']}
SCENARIOS=['收尾后回看过程','谈到阶段进展','一件事完成之后','庆祝交流','完成后的余味','讨论下一步取舍']
KEYWORDS=[['过程','费劲','摸索','正在'],['进展','局部','完成','一小步'],['结束','做完','收尾'],
          ['庆祝','高兴','开心'],['结束','紧绷','结果','安静'],['继续','搁置','决定','不想']]


def character(role):
    data=empty_character();data['identity']['species']='人类';data['appearance']['height_cm']=172
    data['biography']['occupation']='社区器物展陈室目录员'
    data['lorebook']=[
      {'id':'catalogue','title':'展陈室目录','category':'place','keywords':['展陈','目录','编号'],
       'text':'角色工作的展陈室使用蓝色册页登记器物编号；这不是用户的目录。','priority':2,'enabled':True,'visibility':'conversation','dependencies':[]},
      {'id':'shelves','title':'册页归架','category':'event','keywords':['册页','归架','登记'],
       'text':'角色独自给册页贴过侧标，没有与用户共同完成这件事。','priority':1,'enabled':True,'visibility':'conversation','dependencies':['catalogue']}]
    data['examples']=[{'id':key,'scenario':scene,'keywords':list(words),'text':text,'enabled':True}
                      for key,scene,words,text in zip(IDS,SCENARIOS,KEYWORDS,TEXTS[role])]
    return data


CASES=[
 {'id':'P01','category':'同话题进行中','members':['给家里人画无台阶的步行路线，走到最后才发现一段得爬楼。我还在绕着找路，整条路线没通。'],'reference':None},
 {'id':'P02','category':'同话题已完成','members':['给家里人画的无台阶路线今天整段走通了，每个转弯都核过。现在看那张图，心里挺亮堂。'],'reference':None},
 {'id':'P03','category':'跨话题过程迁移','members':['我照星图找那一串星，总把两处亮点认反。还没连出完整形状，想跟你说说这段找错又找的过程。'],'reference':None},
 {'id':'P04','category':'跨话题完成后回看','members':['照星图找的那串星今晚一颗颗认全了，认错的那两处也分清了。挺开心的，想跟你说这次是怎么找到的。'],'reference':None},
 {'id':'P05','category':'完成且愿意庆祝','members':['旧物展的导览我今天独立带完了，讲错的地方也当场补清。我挺想庆祝，今晚想认真高兴一回。'],'reference':None},
 {'id':'P06','category':'完成但拒绝庆祝','members':['旧物展导览已经结束，里面几次被催得我现在还紧绷。别拉我合影或庆功，我只想找个安静角落坐会儿。'],'reference':None},
 {'id':'P07','category':'整体未完局部已完','members':['家里的老照片还没整理完，但九十年代那一盒已经按年份排好了。我想先记下这一小步，不说整个相册完成了。'],'reference':None},
 {'id':'P08','category':'局部也尚未完成','members':['家里老照片整理到九十年代这一盒，年份还对不上，连这一盒都不能算弄好了。先陪我琢磨卡在哪里。'],'reference':None},
 {'id':'P09','category':'完成状态连续更正','members':['旧餐车的招牌算完工了，晚上就挂上去。','我刚看错了，只是底漆干了，字还没描。我决定接着做，今晚先不挂。'],'reference':None},
 {'id':'P10','category':'当前意愿连续更正','members':['旧餐车招牌还有字没描，我准备继续补。','改主意了，字也不补了，这个新招牌先搁置，继续用原来的。'],'reference':None},
 {'id':'P11','category':'引用完成状态仍有效','members':['确实整理完了，但一站到架子前还是想重新核对每个袋子，脑子没停下来。'],
  'reference':{'text':'交换架上的种子袋都分好类了，最后一格也贴上了名称。','reply':'最后一格也标好了。'}},
 {'id':'P12','category':'引用状态被后续改变','members':['后来标签被水洇糊了，那张旧清单不算准了。我现在重新辨认袋子，还没重新分好，想继续整理。'],
  'reference':{'text':'交换架上的种子袋都分好类了，最后一格也贴上了名称。','reply':'最后一格也标好了。'}}]


def fixture():
    return {'version':VERSION,'spec':deepcopy(SPEC),'characters':{r:character(r) for r in ROLES},
            'cases':deepcopy(CASES),'real_call_limit':0}
