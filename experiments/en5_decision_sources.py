"""Read reviewable MaiBot passages directly from its four pinned Git blobs."""
import ast
import hashlib
from pathlib import Path

from .en5_decision_contract import SLOT, require


COMMIT = '478f671e64467121fd3e39205c8d3507b5ce0bd6'
LINK = 'https://github.com/Mai-with-u/MaiBot/blob/' + COMMIT + '/'
FILES = {
    'planner': ('prompts/zh-CN/main/maisaka_chat.prompt', 'e6599129d9e7904525882debdb502c5b781d73f3'),
    'tool': ('src/maisaka/builtin_tool/reply.py', '113407b8de402be8f89f8689ffff0ea31cbcb564'),
    'private': ('prompts/zh-CN/main/reply_private.prompt', '832a526a65b66697fa5973f99dfef0409a5cb108'),
    'assembly': ('src/chat/replyer/retro_prompt.py', '3cde0a6899bbe245fd9cfbdfec93a0023a7e0e9e'),
}


def catalog(source):
    documents = {}
    for key, (path, expected) in FILES.items():
        data = (Path(source) / path).read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        require(actual == expected, 'upstream_blob_changed')
        documents[key] = data.decode('utf-8').splitlines()
    entries = []

    def passage(key, file, start, end, title, use, placement, missing, stages, *, literal=False):
        raw = '\n'.join(documents[file][start - 1:end])
        # Adjacent Python string literals form one unchanged description in the upstream tool schema.
        excerpt = ast.literal_eval('(' + raw + ')') if literal else raw
        path, blob = FILES[file]
        entries.append({'key': key, 'title': title, 'path': path, 'blob': blob, 'start': start, 'end': end,
                        'url': LINK + path + f'#L{start}-L{end}', 'text': excerpt, 'use': use,
                        'placement': placement, 'missing': missing, 'stages': stages})

    def description(key, line, title, use, placement, missing, stages):
        # Read the description value, including quotes/escaping exactly as Python would interpret it.
        tree = ast.parse('\n'.join(documents['tool']))
        node, = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str
                 and n.lineno == line and n.col_offset > 10 and n.value != 'description']
        path, blob = FILES['tool']
        entries.append({'key': key, 'title': title, 'path': path, 'blob': blob,
                        'start': node.lineno, 'end': node.end_lineno,
                        'url': LINK + path + f'#L{node.lineno}-L{node.end_lineno}', 'text': node.value,
                        'use': use, 'placement': placement, 'missing': missing, 'stages': stages})

    passage('participation', 'planner', 3, 6, 'Planner 怎样判断是否参与',
            '上游给 Planner 留出参与、转向其他话题和等待的选择。', 'Flash 决策说明',
            '圈定适合已受理私聊的文字。PCAR 的三个动作和任务状态见本页字段表。', ['flash'])
    passage('previous', 'planner', 18, 18, 'Planner 怎样使用已发言',
            'Planner 可以根据自己实际发出的内容理解前文。', 'Flash 决策说明',
            '判断这段里哪些文字适用于角色和最近两轮的范围。', ['flash'])
    description('target', 145, '回复目标 msg_id', '上游用真实消息编号定位要回应的发言。',
                'Flash 选目标，Pro 读取对应位置', 'PCAR 使用当轮编号。上游另有主动发言语义。', ['flash', 'pro'])
    passage('situation', 'tool', 151, 153, '交给 Replyer 的局面与事实',
            '这是 chat_anlyze 字段在工具调用中实际使用的连续原文。', 'Flash 决策说明或 Pro 交接说明',
            'PCAR 用三个字段传递判断。请圈定这一职责边界中适用的文字。', ['flash', 'pro'], literal=True)
    description('focus', 172, '上游精细选材的关注点', '上游可选的 expression_intent 有单独的 focus 字段。',
                'Flash 决策说明', '圈定能帮助识别实际发言目标的文字。', ['flash'])
    description('action', 176, '上游精细选材的接话动作', 'reply_act 是可选的表达选择提示。这里核对动作的含义。',
                'Flash 决策说明或 Pro 交接说明', '本批使用 PCAR 的三类动作。你可以补充对混合意图的处理。', ['flash', 'pro'])
    description('length', 158, '本次发言的篇幅', '上游按轮提供可选的篇幅要求。篇幅在本批归 Pro 判断。',
                '上游职责对照', '本批单独检验接话动作，篇幅控制留待后续研究。', [])
    passage('private_template', 'private', 1, len(documents['private']), '普通私聊 Replyer 完整模板',
            '目标发言和局面材料在真实对话之后，表达风格有自己的位置。', 'Pro 交接说明',
            '可在下框圈选原句。花括号表示上游插槽，选入后需核对其展开。', ['pro', 'flash'])
    passage('planner_template', 'planner', 1, len(documents['planner']), 'Planner 完整模板',
            '核对上文的原始位置，以及私聊与上游其他功能共用的段落。', 'Flash 决策说明',
            '可从全文圈选适合 PCAR 私聊的部分。', ['flash', 'pro'])
    passage('wire_tool', 'tool', 128, 197, 'reply 工具的参数定义',
            '核对工具参数原文与条件。expression_intent 只在上游启用相应配置时出现。', '源码对照',
            '当前研究没有选材调用。', [])
    passage('wire_history', 'assembly', 112, 117, '正文模型怎样读取真实发言',
            '普通私聊模板先筛选历史，再将其装入正文请求。', '源码对照',
            'PCAR 的前文绑定到实际呈现回执。', [])
    passage('wire_inputs', 'assembly', 325, 342, 'Replyer 变量的实际装配位置',
            '代码分别填入人物、历史、目标、局面与风格。', '源码对照',
            'Pro 的完整输入差异可在每轮请求预览中核对。', [])
    return entries


def material_text(entries, stage, answer):
    require(isinstance(answer, dict) and set(answer) == {'passages', 'addition'}
            and isinstance(answer['passages'], list) and isinstance(answer['addition'], str), 'material_shape')
    available = {item['key']: item for item in entries}
    pieces = []
    for pick in answer['passages']:
        require(isinstance(pick, dict) and set(pick) == {'key', 'start', 'end'}
                and isinstance(pick['key'], str) and pick['key'] in available, 'unknown_passage')
        item = available[pick['key']]
        lo, hi = pick['start'], pick['end']
        require(stage in item['stages'] and type(lo) is int and type(hi) is int
                and 0 <= lo < hi <= len(item['text']), 'passage_scope')
        pieces.append(item['text'][lo:hi])
    if answer['addition']:
        pieces.append(answer['addition'])
    body = '\n'.join(pieces)
    return body, sorted(set(SLOT.findall(body)))
