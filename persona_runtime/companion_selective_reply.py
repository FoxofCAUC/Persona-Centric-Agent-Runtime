"""Isolated v9 candidate: change only the approved IM guidance paragraph."""
from .companion_im import INSTRUCTION as PREVIOUS_INSTRUCTION, GUIDANCE as PREVIOUS_GUIDANCE
from .models import RuntimeErrorBase
from .prompt_assembly import PromptBlock

INSTRUCTION_VERSION = 'companion-im-instruction-v2'
GUIDANCE = (
    '就着眼前具体的事和自己的反应聊天，闲聊可以停留在分享本身，不必概括成一类事的道理，也不必安排下一步；'
    '对方结束话题时，就自然道别。表达量随内容展开，意思到了就收住。建议和解释按当前话题与对方意愿取舍；'
    '普通闲聊的句尾通常留空，标点随语气和意思使用。'
)
INSTRUCTION = PREVIOUS_INSTRUCTION.replace(PREVIOUS_GUIDANCE, GUIDANCE, 1)


def render_blocks(blocks):
    if sum(b.name == 'persona_expression' for b in blocks) != 1:
        raise RuntimeErrorBase('companion_selective_instruction_boundary_changed')
    result = []
    for block in blocks:
        if block.name == 'persona_expression':
            if not block.content.startswith(PREVIOUS_INSTRUCTION):
                raise RuntimeErrorBase('companion_selective_instruction_boundary_changed')
            block = PromptBlock(block.name, INSTRUCTION + block.content[len(PREVIOUS_INSTRUCTION):],
                                'companion_selective_reply', INSTRUCTION_VERSION)
        result.append(block)
    return result
