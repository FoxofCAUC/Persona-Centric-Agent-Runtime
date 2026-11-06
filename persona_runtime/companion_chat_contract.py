"""Candidate chat wording with the existing schema and delivery semantics."""
from .companion_contract import structural_contract
from .models import RuntimeErrorBase

VERSION = 'schema3-companion-v2'
RENDERER = 'bubbles-schema3-companion-v2'

REPLACEMENTS = (
    ('capacity_wording',
     '只允许这些字段，messages为1至12项，不凑数；单项最多8000字符，总正文最多12000字符。',
     '只允许这些字段，messages为1至12项，不凑数。'),
    ('batch_response',
     '每一项直接对应一个聊天气泡。综合整批输入回答，不机械逐条回答输入。',
     '每一项直接对应一个聊天气泡。综合整批输入，不机械逐条回应。'
     '嗯、好等短回应也可以。只需确认时仅“好”即可。'),
    ('task_completeness',
     '不能为短句删除条件、事实或必要说明；技术、代码、网址、数值与引用保持完整，保留必要标点。',
     '当前有明确任务时，保留完成该任务所需的条件、事实和必要说明。'
     '技术、代码、网址、数值与引用保持完整，保留必要标点。'),
    ('required_delivery',
     '第一项、technical/code/quote以及所有必需答案必须cancel_if_new_input=false。',
     '第一项、technical/code/quote以及完成当前明确请求不可缺少的内容必须cancel_if_new_input=false。'),
    ('optional_delivery',
     '仅完全省略也不影响回答完整性的补充或结束语可为true。',
     'cancel_if_new_input=true仅用于被新输入打断后可省略的非必要内容。'),
)


def chat_contract(full_protocol=None):
    result = structural_contract(full_protocol)
    for _, before, after in REPLACEMENTS:
        if result.count(before) != 1:
            raise RuntimeErrorBase('companion_chat_contract_boundary_changed')
        result = result.replace(before, after, 1)
    return result
