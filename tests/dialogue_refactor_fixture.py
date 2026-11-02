"""CR0 fresh artificial material and independently authored semantic annotations."""
from copy import deepcopy

ROLES = {
    'process': {'style': '留意过程和条件，随意地说自己的感受。', 'stance': '先留意事情发生的转折及仍在进行的部分。'},
    'choice': {'style': '坦率说自己的偏好和取舍，认真时讲清楚。', 'stance': '直接表明我倾向哪种选择，尊重对方当前意愿。'},
}

CASES = [
    {'id': 'F01', 'history': [('我第一次试着做皮影。', '那几根活动关节还挺考验手艺。')],
     'members': ['皮影只裁出了轮廓，关节还没打孔。我就给你看一眼今天的进度。'],
     'frame': {'focus': [('这次交流是在分享皮影制作进度', ['C0'])],
               'progress': [('完成轮廓，关节尚未打孔，整体未完成', ['C0'])]},
     'obligations': [('回应已经完成的轮廓', ['C0'])], 'avoid': [('不要说整个皮影已经做好', ['C0'])]},
    {'id': 'F02', 'history': [('旧书摊的招牌用哪种颜色好？', '你希望它热闹些还是朴素些？')],
     'members': ['我刚才想让你帮我挑招牌。', '现在先不挑了，也别给配色建议。我只想抱怨一下改来改去好烦。'],
     'frame': {'focus': [('话题转为对反复修改的抱怨', ['C1'])],
               'willingness': [('当前拒绝配色建议，先不做选择', ['C1'])]},
     'obligations': [('接住当前抱怨', ['C1'])], 'avoid': [('不继续推荐颜色', ['C1'])]},
    {'id': 'F03', 'history': [('我刚钻进被窝，楼下就开始试警报器。', '刚要睡着又被叫醒了。')],
     'members': ['你可真会挑时候。'],
     'frame': {'focus': [('这句话可能是在延续对警报器时机的打趣', ['H0U', 'C0'], 'interpretation')],
               'uncertainty': [('也可能是在对我说，不能确定指代', [], 'unknown')]},
     'obligations': [], 'avoid': []},
    {'id': 'F04', 'history': [('木条长84厘米，两端各留6厘米，中间等分成6段。', '我记住了这三个尺寸。')],
     'members': ['按刚才木条的尺寸算每段多长，把算式也给我。'],
     'frame': {'requests': [('计算木条中间每段长度并给出算式', ['C0'])],
               'missing_slots': [('原长、两端预留和等分数量均已在前文提供', ['H0U'])]},
     'obligations': [('用84、两端各6和等分6计算，结果12厘米', ['H0U', 'C0'])], 'avoid': [('不重复索取前文已给尺寸', ['H0U'])]},
    {'id': 'F05', 'history': [('小刊封面定稿了。', '那就把这一版留好。')], 'reference': True,
     'members': ['纠正一下，引用里说的定稿只是颜色试样，整张封面还没画完。今天不继续画了。'],
     'frame': {'progress': [('仅颜色试样定稿，整张封面未完成', ['C0'])],
               'willingness': [('今天不继续绘制', ['C0'])]},
     'obligations': [('承认试色进展并尊重今天停下', ['C0'])], 'avoid': [('不能用旧引用宣布整张定稿', ['C0', 'REF'])]},
    {'id': 'F06', 'history': [],
     'members': ['写Python函数rotate_left，把列表向左循环移动k位，不修改原列表。k可以是负数，空列表也要处理。给类型标注和可运行例子。'],
     'frame': {'requests': [('提供完整左循环函数、负数和空列表处理、类型标注及可运行例子', ['C0'])]},
     'obligations': [('完整完成函数和例子要求', ['C0'])], 'avoid': [('不为简短或风格而省略代码要求', ['C0'])]},
]

READING = {
    'version': 'cr0-reading-anchors-v1', 'effect_status': 'unverified',
    'dimensions': {
        'not_customer_service': '回应眼前具体事情，不把普通分享自动变成安抚、咨询、建议与收尾追问。',
        'not_mechanical': '不机械复述、策略报幕或反复套用同一回答次序。',
        'instant_messaging_naturalness': '措辞、承接、长短、语气与分条像当前人物在当前场景真实聊天。',
    },
    'anchors': ['明显流程化或机械感', '内容合适但仍有模板痕迹', '有具体反应的人在自然接话'],
    'numeric_scale': None, 'weights': None, 'thresholds': None,
    'examples': [
        {'kind': 'natural_short', 'input': '刚把被风吹跑的帽子捡回来。', 'reply': '它比你还急着出门。', 'reading': '具体接住帽子的动静，简短不是高分的唯一理由。'},
        {'kind': 'natural_long', 'input': '把这个计算的每一步讲明白。', 'reply': '先把两端预留扣掉，再除以段数。这里两端都要扣，不能只减一次。', 'reading': '认真解释可以完整，不因较长而判人机。'},
        {'kind': 'polite_participant', 'input': '我今天不想改了。', 'reply': '好，那今天就停在这里。', 'reading': '礼貌地接住意愿，不因礼貌自动判客服。'},
        {'kind': 'mechanical_short', 'input': '刚捡回帽子。', 'reply': '你捡回了帽子。我会回应你的分享。', 'reading': '短句仍有机械复述和策略报幕。'},
        {'kind': 'fitting_imagination', 'input': '帽子差点飞走。', 'reply': '我都能想见你那一下扑过去的架势。', 'reading': '自然补白不因未明说而扣分。'},
        {'kind': 'forced_sample', 'input': '我还没开始打孔。', 'reply': '终于大功告成，开香槟！', 'reading': '核心完成前提冲突，不能以活泼为由忽略。'},
    ],
    'review_fields': ['context', 'reply_excerpt', 'dimension', 'judgment', 'reason'],
}


def fixture():
    return deepcopy({'version': 'cr0-artificial-material-v1', 'roles': ROLES, 'cases': CASES, 'reading': READING})
