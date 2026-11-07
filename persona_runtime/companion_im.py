"""Small positive IM guidance, applied only to the explicitly frozen v8 profile."""
from .companion_prompt_light import INSTRUCTION as PREVIOUS_INSTRUCTION
from .models import RuntimeErrorBase
from .prompt_assembly import PromptBlock

INSTRUCTION_VERSION = 'companion-im-instruction-v1'
GUIDANCE = (
    '随口说此刻真正想回应的内容，短接话、自然省略都合适，意思到了就收住。'
    '建议和解释按当前话题与对方意愿取舍；普通闲聊的句尾通常留空，标点随语气和意思使用。'
)
INSTRUCTION = PREVIOUS_INSTRUCTION.removesuffix('\n人物资料：') + GUIDANCE + '\n人物资料：'


def render_blocks(blocks):
    """Change only the owned instruction prefix, never author or reply bodies."""
    if sum(b.name == 'persona_expression' for b in blocks) != 1:
        raise RuntimeErrorBase('companion_im_block_boundary_changed')
    result = []
    for block in blocks:
        if block.name == 'persona_expression':
            if not block.content.startswith(PREVIOUS_INSTRUCTION):
                raise RuntimeErrorBase('companion_im_instruction_boundary_changed')
            block = PromptBlock(block.name, INSTRUCTION + block.content[len(PREVIOUS_INSTRUCTION):],
                                'companion_im', INSTRUCTION_VERSION)
        result.append(block)
    return result
