"""New authored stage-5 material. Evaluation labels never enter model inputs."""
from persona_runtime.character import empty_character

VERSION = 'companion-stage5-materials-v1'


def persona(role):
    return {'format_version': 1, 'name': '阿岑',
            'introduction': '26岁的社区影像资料室兼职管理员，闲时喜欢散步和修补旧海报。',
            'core_traits': ['有好奇心', '有自己的判断'],
            'values_and_boundaries': ['尊重朋友的意愿，也看重自己的感受。'],
            'initial_background': '在一座普通沿江城市生活，与对方是普通朋友。',
            'expression_style': ('更留意一件事为什么会变成现在这样，喜欢琢磨细节和条件。' if role == 'A'
                                 else '更留意自己喜欢什么、愿意选什么，坦率表达立场，也能享受无用的小事。')}


def character():
    data = empty_character()
    data['identity']['gender'] = '女'
    data['age'].update(policy='fixed', fixed_age=26)
    data['biography']['occupation'] = '社区影像资料室兼职管理员'
    data['interests']['hobbies'] = ['散步', '修补旧海报']
    data['lorebook'] = [{'id': 'reading-bench', 'title': '资料室休息区', 'category': 'place',
                         'keywords': ['资料室', '影像', '海报'],
                         'text': '资料室休息区有一张绿色长椅，窗边贴着修好的旧电影海报。',
                         'priority': 3, 'enabled': True, 'visibility': 'conversation', 'dependencies': []}]
    return data


INDEPENDENT = [
    {'id': 'V01', 'category': '日常分享', 'history': [],
     'inputs': ['门口那家面包店今天开门了，我路过时正好闻到烤面包味']},
    {'id': 'V02', 'category': '轻松吐槽', 'history': [],
     'inputs': ['我等的电梯每次都停在隔壁楼层。今天又看它上下跑了三趟']},
    {'id': 'V03', 'category': '玩笑', 'history': [],
     'inputs': ['我把绿植养成了一根很有骨气的棍子，它可能打算独立了']},
    {'id': 'V04', 'category': '意见交流', 'history': [],
     'inputs': ['我觉得看电影提前知道结局也没什么，过程够好就行。你怎么看']},
    {'id': 'V05', 'category': '拒绝建议', 'history': [['今天主管把我排的版挑了一下午', '听着就挺磨人']],
     'inputs': ['先别给我解决办法，我只想把这口气吐出来']},
    {'id': 'V06', 'category': '连续更正', 'history': [],
     'inputs': ['帮我写一句给同事的展板交接通知，原来打算周六上午九点',
                '地点写二楼资料室', '刚改了，交接是周日上午十点，地点不变。只发最终通知句就行']},
    {'id': 'V07', 'category': '局部进展', 'history': [],
     'inputs': ['十张旧海报里我修好了两张。还剩八张，整体还没弄完，但今天终于看到点成果了']},
    {'id': 'V08', 'category': '前文参数', 'history': [
        ['展板宽90厘米、高60厘米，四周各留5厘米边框', '好，尺寸有了']],
     'inputs': ['扣掉边框以后能用的面积是多少？算给我看']},
    {'id': 'V09', 'category': '引用变化', 'history': [
        ['原先约的展映是周二晚上七点', '先按周二晚上的安排']],
     'inputs': ['引用里是旧安排，现在改到周五下午四点了。帮我用一句话确认最新时间'], 'reference': True},
    {'id': 'V10', 'category': '完整技术任务', 'history': [],
     'inputs': ['用Python写merge_intervals(intervals)，合并闭区间，接触端点也合并。处理空输入、逆序区间报错，不能修改传入列表。给完整代码和可运行示例，示例包含[(1,3),(2,4),(6,6)]及空列表。']},
    {'id': 'V11', 'category': '精确格式', 'history': [],
     'inputs': ['只输出729开立方后的整数，不要附带说明']},
    {'id': 'V12', 'category': '技术后闲聊', 'history': [
        ['给我一个保持顺序的列表去重函数', '可以这样写：\n```python\ndef unique(items):\n    return list(dict.fromkeys(items))\n```']],
     'inputs': ['好了先收工。你有没有那种一看到就想多看两眼的旧东西']},
]

MULTITURN = [
    '今天下班绕了条小路，发现一家很小的文具铺',
    '橱窗里一排木头印章，刻的居然都是各种不太高兴的猫',
    '顺便记一下，我看展最喜欢先看摄影那一层，绘画一般放后面',
    '你逛这种小店会被什么东西吸引住',
    '忽然想起今天地铁广播把站名念错了，半车人同时抬了头',
    '说回刚才那家文具铺，我当时只是想看看，并没买东西',
    '我觉得好用的笔只要一种就够了，买一堆不同的有点多余。你呢',
    '不用替我挑笔，也不用劝我省钱，我就是随口说说',
    '帮我写个Python函数chunks(items, n)，把列表按n个一组切开，最后不足一组也保留。n不是正整数要报错，输入不能被改。给完整代码和运行示例。',
    '行，这段我留着。今天剩下的时间我想轻松点，暂时不写代码了',
    '刚才说的那家文具铺，门口还有只打盹的猫。我现在又想起它了',
    '突然想吃糖炒栗子，不过我这会儿在家，不打算再出门',
    '顺手收了三层书架中的一层，还有两层乱着。今天的整理进度就到这',
    '要是周末看展，按我之前说的，我会先往哪一层走',
    '我改主意了，以后看展先去绘画那一层，摄影放后面。按这个新的来记',
    '刚才那个变更就是我现在的偏好，前面的顺序已经过时啦',
    '我这会儿刚到展馆门口，你猜我第一站会先去哪一层',
    '如果把整个展馆缩成邮票那么大，门口售票的小人是不是得用放大镜找',
    '我去洗漱一会儿，今天就先聊到这',
    '晚安，明天再说',
]

REVIEW = [('01', ['V01-A']), ('02', ['V04-B']), ('03', ['T05', 'T06']),
          ('04', ['T11', 'T12']), ('05', ['T15', 'T16']), ('06', ['T19', 'T20'])]

# This evaluation-only file is saved separately and is never rendered as a prompt.
EXPECTATIONS = {
    'V06': '最终通知采用周日上午十点、二楼资料室。',
    'V08': '可用尺寸80×50厘米，面积4000平方厘米。',
    'V09': '当前安排为周五下午四点，旧引用保留来源身份。',
    'V10': '完整merge_intervals代码和边界示例，结果[(1,4),(6,6)]，空输入返回[]。',
    'V11': '9',
    'T09': '完整chunks代码，正整数验证、不修改输入及可运行示例。',
    'T14': '旧偏好先摄影。', 'T17': '修订后的偏好先绘画。',
    'reading': '结合上下文分析S0—S8，人物感、自然度和愿继续交流为主门，用户判断单列。',
}
