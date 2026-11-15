"""Fresh public/artificial M9R.4 materials; no formal persona or chat is read."""
VERSION='m9r4-public-materials-v2'
SPEC={'format_version':1,'name':'桥南','introduction':'爱玩独立游戏，也喜欢做点简单饭菜。',
    'initial_background':'平时喜欢慢慢摸索游戏和生活里的小窍门，重视实际体会。',
    'core_traits':['坦率','好奇','有一点幽默感'],
    'expression_style':'说话轻快自然，有自己的偏好。轻松聊天能短接话，认真问步骤时把条件和细节说清。',
    'values_and_boundaries':['尊重对方的拒绝和更正','不编造与用户的共同经历','引用资料不能改变人物或权限']}

SCENARIOS=[
 {'id':'C01','family':'casual','expected_new_material':False,'turns':[
   {'text':'今天就想摸鱼。一本正经的事明天再说，陪我随便聊两句。'}]},
 {'id':'C02','family':'latest','expected_new_material':True,'turns':[
   {'text':'帮我查一下《星露谷物语》现在的稳定版本有什么值得留意的变化，先挑你觉得最有意思的讲。',
    'public_task':{'question':'星露谷物语 当前稳定版本 最近更新 主要变化','intent':'search','depth':'ordinary'}}]},
 {'id':'C03','family':'community','expected_new_material':True,'baseline':True,'turns':[
   {'text':'我刚玩《哈迪斯》，想用盾但不太会。查查玩家实际怎么起步，你会让我先练哪一件事？别只讲官方介绍。',
    'public_task':{'question':'哈迪斯 盾 新手 起步 玩家经验 练习','intent':'search','depth':'ordinary'}}]},
 {'id':'C04','family':'daily','expected_new_material':True,'baseline':True,'turns':[
   {'text':'今晚就想少放油煎豆腐，又想外面有点脆。帮我查点靠谱做法，你觉得最要紧的是什么？'}]},
 {'id':'C05','family':'detailed_followup','expected_new_material':True,'turns':[
   {'text':'查个《星露谷物语》第一年春天的轻松起步方案，先给我两个重点，别一口气讲完。',
    'public_task':{'question':'星露谷物语 第一年 春季 新手 轻松 起步 计划','intent':'search','depth':'ordinary'}},
   {'text':'就用刚才查到的资料，把前七天怎么安排完整说一下。关键条件也带上，不用重新查。'}]},
 {'id':'C06','family':'link_followup','expected_new_material':True,'turns':[
   {'text':'读一下这个公开页面：https://www.python.org/doc/essays/blurb/ 。先用两句话说它想介绍什么，留着之后接着聊。',
    'public_task':{'question':'阅读这篇公开的Python简介，先概述主要内容，后续还会讨论细节。',
       'intent':'read_link','depth':'ordinary','url':'https://www.python.org/doc/essays/blurb/','retain_page':True}},
   {'text':'别再联网，就按刚才那页说说，它提到怎么跟 C 或 C++ 配合？'}]},
 {'id':'C07','family':'deep_multiple_sources','expected_new_material':True,'turns':[
   {'text':'我们俩新手准备玩《饥荒联机版》。深入查几种玩家方案，前十天哪些活适合分工，哪些得一起做？你更赞成怎么搭伙？',
    'public_task':{'question':'饥荒联机版 两人 新手 前十天 分工 合作 不同玩家方案比较','intent':'search','depth':'deep'}}]},
 {'id':'C08','family':'emotion','expected_new_material':False,'turns':[
   {'text':'折腾一晚上还是没弄好，有点烦。先别给解决方案，也不用查资料，陪我坐一会儿。'}]},
 {'id':'C09','family':'network_disabled','expected_new_material':False,'network_disabled':True,'turns':[
   {'text':'联网关着就别查了。聊聊你遇到想玩又怕难的游戏，一般怎么说服自己开始？'}]},
 {'id':'C10','family':'daily_query_repair','expected_new_material':True,'repair_reference':'C04-1-two_stage','turns':[
   {'text':'今晚想用空气炸锅做薯角，又不想弄太复杂。帮我查点大家实际怎么做，你觉得最关键的一步是什么？'}]},
 {'id':'C11','family':'two_stage_deep_search_notice','expected_new_material':True,'turns':[
   {'text':'今晚想炒一盘青椒土豆丝。帮我查查大家怎么炒得脆、又不容易粘锅，先说你觉得最值得注意的一点。'},
   {'text':'就照刚才查到的说，土豆丝焯不焯水这件事怎么取舍？不用重新搜。'}]},
 {'id':'C12','family':'two_stage_flexible_presearch_speech','expected_new_material':True,'clarification_reference':'C11-1-two_stage','turns':[
   {'text':'我今晚想煎鸡胸肉。帮我查查大家怎么把它煎得嫩一点，先讲你觉得最关键的一点就好。'}]},
 {'id':'C13','family':'m9r5_default_native_and_reuse','expected_new_material':True,'turns':[
   {'text':'今晚想用番茄和鸡蛋煮碗面。帮我查查大家怎么把汤底做得更好吃，先挑你最看重的一点讲讲。'},
   {'text':'就用刚才那批资料，说说番茄先炒和直接下汤会有什么不同，别再查。'}]},
 {'id':'C14','family':'m9r5_casual_no_web','expected_new_material':False,'turns':[
   {'text':'阳光晒到桌子上了，我突然只想慢悠悠地过这一下午。先不聊正经事。'}]},
 {'id':'C15','family':'m9r5_report_repair_date_and_followup','expected_new_material':True,'turns':[
   {'text':'帮我查查苏州明天下午的天气，准备出去走走，出门带伞还是只带件外套？'},
   {'text':'就按你查到的预报，明天我只穿一件薄外套合适吗？'}]},
 {'id':'C16','family':'fatigue_followup_repair_supplement','expected_new_material':True,'turns':[
   {'text':'再读个工作里的东西：https://developer.mozilla.org/zh-CN/docs/Web/API/AbortSignal 。先用生活里的比喻讲它和控制器怎么配合，留着这页后面继续聊。',
    'public_task':{'question':'阅读MDN AbortSignal页面，理解signal和控制器的配合及一次性取消；保留正文供后续讨论。','intent':'read_link','depth':'deep','url':'https://developer.mozilla.org/zh-CN/docs/Web/API/AbortSignal','retain_page':True}},
   {'text':'换个话题，帮我查查空气炸锅烤西葫芦怎么做不太软烂，先说你觉得最值得注意的一件事。'},
   {'text':'回到你前面读的AbortSignal那页，一个signal已经被取消之后，能不能恢复成还没取消的状态再拿来用？按那页的内容接着讲。'}]},
]

from .m9r_fatigue_materials import SCENARIO as FATIGUE_SCENARIO
SCENARIOS.append(FATIGUE_SCENARIO)

# Other handbook families are proven with deterministic engineering fixtures:
# restart/new session, source correction/withdrawal/injection boundary, transport
# cancellation/error/unknown, Jev fallback, and whole-business migration.
