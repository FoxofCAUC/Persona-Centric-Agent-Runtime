"""Opt-in schema3 contract with only the tested language guidance removed."""
from .models import RuntimeErrorBase
from .reply_batch import bubble_prompt

VERSION = 'schema3-structural-v1'
RENDERER = 'bubbles-schema3-structural-v1'
REMOVALS = (
    ('short_length_and_purpose', '普通chat一项围绕一个主要表达目的，通常约8至35字；嗯、好等短回应也可以，不能为字数填充。'),
    ('split_and_comma_guidance', '独立建议、确认与另起追问分别放不同项；条件与结果、原因与解释可以用逗号放在同一项。'),
    ('ordinary_punctuation_guidance', '通常省略普通句末句号，保留合理逗号、顿号和表达疑问或情绪的问号、感叹号、省略号、波浪号。'),
    ('phrase_segmentation_guidance', '不要把多项建议堆成无标点长串，也不要把每个短语拆碎；不要用空行冒充多个气泡。'),
    ('confirmation_and_condition_examples', '示例：确认与追问可分为“看到了”和“刚才怎么啦？”；条件结果可为“雨还没停，等小一点再走”。'),
    ('suggestion_examples', '多项建议可分为“杯子放右侧”和“线从桌后绕过去”；只需确认时仅“好”即可。示例不照抄。'),
    ('closing_turn_guidance', '不要为了凑数添加追问或结束语。'),
)


def structural_contract(full_protocol=None):
    """Transform only the known protocol, never arbitrary persona/history text."""
    original = bubble_prompt()
    if full_protocol is not None and full_protocol != original:
        raise RuntimeErrorBase('contract_language_clause_boundary_changed')
    reduced = original
    for _, text in REMOVALS:
        if original.count(text) != 1 or reduced.count(text) != 1:
            raise RuntimeErrorBase('contract_language_clause_boundary_changed')
        reduced = reduced.replace(text, '', 1)
    return reduced
