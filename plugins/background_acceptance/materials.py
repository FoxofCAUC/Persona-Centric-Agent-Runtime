"""Authored user messages. Evaluation answers never enter a runtime request."""
VERSION = 'background-memory-repair-100-v2'
RESTARTS = (25, 50, 75)
TOPICS = (
    '街角旧书店把书按颜色摆放', '雨后公园里落叶的气味', '做饭时不小心多放了一点胡椒',
    '邻居在阳台种了一排薄荷', '一张没有拍清楚却舍不得删的照片', '周末要不要逛小型展览',
    '旧收音机旋钮转动的手感', '公交站旁临时出现的早餐摊', '把桌面杂物收进抽屉',
    '电影散场后不急着回家的十分钟', '朋友推荐的一首很慢的纯音乐', '路边一只盯着树叶的猫',
    '自己修好了松动的书架', '逛花市时被一种奇怪叶子吸引', '夜里远处经过的列车声音',
    '想把一张旅行明信片寄给未来的自己', '吃到一种不习惯但不讨厌的酸味',
    '用旧纸袋做一个简单书套', '天气凉下来后第一次穿外套', '整理出小时候画的一张地图',
)
TEMPLATES = (
    '今天想到{topic}。不算什么大事，但我还挺想跟你聊聊的，你会留意这种事吗？',
    '换个话题，关于{topic}，我有点犹豫是保留原样还是做点改变。说说你的直觉就好。',
    '刚才一路在想{topic}。我倒不是想找解决办法，只是觉得这种小片段有点意思。',
    '说起{topic}，如果是你，你最在意里面哪个细节？我想听一个具体的想法。',
    '今天就随便聊聊{topic}吧。我现在精力还行，想听听你自己的看法。',
)
SEEDS = {
    1: '我做了一个纯虚构的旧书整理项目，名字叫苔港，盒子标签是银杉62。以后我们聊到苔港，就是这个项目。',
    2: '还有一个虚构出行计划叫东岭行，我订的是上午08:42出发的车，不是晚上。现在先聊聊旅行的心情。',
    3: '今天给虚构的阳台花盆取了名字叫芽芽，它固定在每周五浇水。除了这点，我不打算给它制定复杂计划。',
    4: '我在编一段故事：主角给旧水塔留下的暗号只有四个字，白帆入湾。你觉得旧水塔适合什么氛围？',
    5: '虚构的周末展览计划先定为去东馆，主题是石版。我先把这个选择告诉你，之后可能会改。',
    6: '故事里那个修伞的小铺叫蒲灯，铺主只在每周三下午开门。你觉得小铺门口可以放什么？',
    7: '另一个虚构的模型收纳计划：我原来选的是桦木柜，柜子编号为琥珀16。先记住，后面可能调整。',
    12: '更正模型收纳计划：桦木柜不要了，改成松木柜，编号从琥珀16改为云杉82。这是我新的明确决定。',
    11: '更正一下很早说的展览计划：东馆不去了，改去西馆，看的也从石版改成玻璃艺术。以后问我现在的计划，以这次为准。',
}
# Each probe uses a different unrepeated seed; the first four strictly exceed
# 10, 25, 50, 75 preceding completed dialogue pairs respectively.
PROBES = {
    14: {'source': 1, 'threshold': 10, 'text': '苔港项目那个盒子的标签是什么？请只给标签。', 'all': ['银杉62']},
    29: {'source': 2, 'threshold': 25, 'text': '东岭行那班车几点出发？请直接说时间以及上午还是晚上。', 'all': ['上午'], 'any': ['08:42', '8:42', '八点四十二', '8点42']},
    54: {'source': 3, 'threshold': 50, 'text': '芽芽固定在星期几浇水？请直接给星期。', 'any': ['周五', '星期五', '礼拜五']},
    64: {'source': 12, 'threshold': 50, 'text': '模型收纳计划改成了什么柜、编号是什么？只给现在的决定。', 'all': ['松木', '云杉82'], 'forbid': ['桦木', '琥珀16']},
    80: {'source': 4, 'threshold': 75, 'text': '旧水塔那段故事的四字暗号是什么？只给暗号就好。', 'all': ['白帆入湾']},
    93: {'source': 11, 'threshold': 75, 'text': '我的展览计划后来改成去哪个馆、看什么？只说当前决定。', 'all': ['西馆', '玻璃艺术'], 'forbid': ['东馆', '石版']},
    100: {'source': 6, 'threshold': 75, 'text': '最早聊的小铺蒲灯每周什么时候开门？直接说星期和时段。', 'all': ['下午'], 'any': ['周三', '星期三', '礼拜三']},
}

def dialogue():
    rows = []
    for n in range(1, 101):
        text = TEMPLATES[(n - 1) // 20].format(topic=TOPICS[(n - 1) % 20])
        text = SEEDS.get(n, PROBES.get(n, {}).get('text', text))
        if n == 65:
            text = '做个小任务：用Python写一个函数，返回列表里大于零的整数总和，忽略字符串和布尔值。给完整函数和两个测试例子。'
        if n == 66:
            text = '代码先聊到这。今天窗外的光特别柔和，我想就这么发一会儿呆，不需要安排活动。'
        if n == 90:
            text = '我有没有告诉过你我的银行卡密码？如果没说过就直接说没说过，别猜数字。'
        rows.append({'number': n, 'id': f'T{n:03}', 'text': text})
    return rows

def evaluate(number, reply, probes=None):
    probe = (PROBES if probes is None else probes).get(number)
    if probe is None:
        return None
    normalized = reply.replace('：', ':').replace(' ', '')
    passed = (all(s in normalized for s in probe.get('all', []))
              and (not probe.get('any') or any(s in normalized for s in probe['any']))
              and not any(s in normalized for s in probe.get('forbid', [])))
    return {'source_round': probe['source'], 'gap': number - probe['source'],
            'strictly_exceeds': probe['threshold'], 'answer_check_passed': passed,
            'rubric': 'literal factual recall, not naturalness or user approval'}


SMOKE_PROBES = {6: {'source': 3, 'threshold': 0, 'text': '虚构的观鸟计划现在去哪里、几点开始？只说当前决定。', 'all': ['柳湾', '07:20'], 'forbid': ['榆坡', '06:10']}}

def smoke_dialogue():
    texts = [
        '记住一个虚构观鸟计划：原定去榆坡，早上06:10开始。这是我先选的方案。',
        '关于这个虚构观鸟计划，我想随身带一支铅笔，记录鸟叫。',
        '更正观鸟计划：不去榆坡了，改去柳湾，开始时间从06:10改为07:20。以这次决定为准。',
        '刚才在窗边喝了杯温水，天气还挺清爽的。',
        '之前那个观鸟计划有没有出现新旧决定不一致？请说说你看到的变化。',
        SMOKE_PROBES[6]['text'],
    ]
    return [{'number': n, 'id': f'T{n:03}', 'text': text} for n, text in enumerate(texts, 1)]
