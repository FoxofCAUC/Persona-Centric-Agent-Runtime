"""Public synthetic replay inputs, authored independently of generated replies."""

CASES = (
    {'id': 'R1', 'category': 'daily_sharing', 'history': [
        ['午休出去转了转。', '今天往哪边走的？'],
        ['走到老邮局那边，没什么特别的。', '那边路还挺宽的。'],
        ['顺路在小店买了个芝麻烧饼。', '这趟还带了点吃的回来。']],
     'current_input': '刚吃完，芝麻还挺香的。'},
    {'id': 'R2', 'category': 'simple_acknowledgment', 'history': [
        ['还在地铁上，快到站了。', '我还在这儿。'],
        ['出站后走几分钟就到家。', '嗯。'],
        ['刚进小区。', '到了再说也行。']],
     'current_input': '到家了。'},
    {'id': 'R3', 'category': 'casual_complaint', 'history': [
        ['下午开了一个特别散的会。', '议题来回跑？'],
        ['对，本来只需要确认一张排班表。', '这张表兜了个大圈。'],
        ['最后安排倒是定下来了，我现在只想吐槽两句。', '说吧。']],
     'current_input': '一张表聊了四十分钟，表自己都要听困了。'},
    {'id': 'R4', 'category': 'playful_exchange', 'history': [
        ['刚才试着用左手写字。', '写出来像什么？'],
        ['像一群正在逃跑的小虫子。', '还挺有行动力。'],
        ['其中一个字已经逃出横线了。', '它先找到了出口。']],
     'current_input': '我决定给这页纸取名《越狱现场》。'},
    {'id': 'R5', 'category': 'correction_and_refusal', 'history': [
        ['我想把今晚的时间安排一下。', '今晚有几件事？'],
        ['想练琴，也想看一集剧。', '两个都挺占心思。'],
        ['刚才说安排一下是随口的，我已经决定今晚休息。', '那今晚的决定就是休息。']],
     'current_input': '也别帮我再排计划了。我就想躺着和你聊一会儿。'},
    {'id': 'R6', 'category': 'complete_task', 'history': [
        ['我在整理一些小时和分钟的数据。', '要合成总分钟数吗？'],
        ['对，输入都是一对小时和分钟。', '还需要规定异常输入怎么处理。'],
        ['只接受非负整数，分钟必须小于60，布尔值也算非法。', '这几个边界可以一起检查。']],
     'current_input': '请给一个完整可运行的Python函数to_minutes(hours, minutes)。'
                      '按刚才的边界检查，非法输入统一抛ValueError。'
                      '附可直接运行的assert例子，覆盖(2, 5)、(0, 0)、负数、60分钟、布尔值和小数。'},
    {'id': 'R7', 'category': 'farewell', 'history': [
        ['今晚总算把阳台收拾好了。', '现在能坐下了。'],
        ['刚坐了一会儿，夜风挺舒服。', '这会儿应该很安静。'],
        ['开始困了，我准备洗漱去。', '嗯。']],
     'current_input': '我睡了，晚安。'},
    {'id': 'R8', 'category': 'continuing_conversation', 'history': [
        ['旧书摊有本封面很花的小说，我有点好奇。', '你翻里面了吗？'],
        ['翻了两页，情节其实挺老套的。', '封面比里面更放得开。'],
        ['但书页边上有人画了一只特别潦草的鸟。', '这本倒多了一点别人的痕迹。'],
        ['就是那只鸟让我犹豫要不要买，故事我可能看不完。', '你更想留下那一页吧。']],
     'current_input': '有点。我觉得买本书只是为了页角那只鸟，也没什么不可以。'},
)


def positions():
    result = []
    for index, case in enumerate(CASES):
        arms = ('control', 'candidate') if index % 2 == 0 else ('candidate', 'control')
        for arm in arms:
            result.append({'position_id': case['id'] + '-' + arm,
                           'case_id': case['id'], 'arm': arm})
    return result
