"""Independent CR1 transport fixtures, never evidence of expression quality.

Choices are authored without importing evaluation labels. A completed but
inapplicable example choice is intentionally retained if it was recalled.
"""
CHOICES = {'D01': ['e5'], 'D04': ['e5'], 'D05': ['e2'], 'D07': ['e3'],
           'D08': ['e6'], 'D10': [], 'D13': ['e4'], 'D16': ['e5'],
           'D18': [], 'D19': [], 'D22': ['e6'], 'D23': []}

REPLIES = {
    'D01': '你这条路线还差点顺手领了个后台的活。',
    'D04': '每走一步都在提醒大家：我来了。',
    'D05': '这项隐藏任务你做得漂亮，后面进电梯的人都省心了。',
    'D07': '如果是一直想看的那场，我会留着等，不只是为了凑个热闹。',
    'D08': '我不接整批了，两百张不是顺手几分钟，我还想看电影。',
    'D10': '谢啦，围巾都替我捎出来了，差点又把它留在教室过夜。',
    'D13': '先吃饭，三分钟也是你一点点校出来的，后面那段吃完再说。',
    'D16': '绕了一整天，任务奖励居然还是你自己的伞。',
    'D18': '133',
    'D19': '周四借投影仪，你负责取，小林负责还。',
    'D22': '您好，原来周五两点的预约不变，请问我可以提前十分钟到吗？',
    'D23': '入场后请关闭闪光灯。我们提前到。',
}


def selection(case_id, pool):
    wanted = CHOICES[case_id]
    available = set(pool['payload']['roots']['example'])
    return {'schema_version': 1, 'lore_ids': [],
            'example_ids': [key for key in wanted if key in available]}, [key for key in wanted if key not in available]


def reply(case_id):
    return {'schema_version': 3, 'behavior_intent': 'respond',
            'messages': [{'content': REPLIES[case_id], 'content_kind': 'chat', 'cancel_if_new_input': False}]}
