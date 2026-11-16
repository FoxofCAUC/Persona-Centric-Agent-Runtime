"""IM标点指导的单一来源；由冻结的转译配置决定本轮职责归属。"""
import json

from .models import RuntimeErrorBase
from .prompt_assembly import PromptBlock

VERSION='im-punctuation-v1'
EXPRESSION_PROFILE='expression-persona-draft-v2'
GUIDANCE='普通闲聊的句尾通常留空，标点随语气和意思使用。'


def delegated_for_turn(db,turn):
    """只读取本轮受理时冻结的策略，当前开关变化不改写旧请求。"""
    if not turn.get('id'):return False
    if turn.get('_search_notice') and not db.execute("SELECT 1 FROM sqlite_master WHERE name='expression_speech_contexts'").fetchone():return False
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expression_runtime_turns'").fetchone():return False
    row=db.execute('SELECT policy_json FROM expression_runtime_turns WHERE turn_id=?',(turn['id'],)).fetchone()
    if not row:return False
    policy=json.loads(row[0])
    if not isinstance(policy,dict) or not isinstance(policy.get('data'),dict):raise RuntimeErrorBase('punctuation_policy_snapshot_invalid')
    data=policy['data']
    return data.get('enabled') is True and data.get('protocol')==EXPRESSION_PROFILE


def delegate_blocks(blocks,companion_version):
    """仅替换已知指令前缀；人设、引用、正文和其他任务指令保持原样。"""
    from .companion import IM_VERSION,SELECTIVE_VERSION
    if companion_version==IM_VERSION:
        from . import companion_im as renderer
    elif companion_version==SELECTIVE_VERSION:
        from . import companion_selective_reply as renderer
    else:raise RuntimeErrorBase('punctuation_companion_profile_unsupported')
    if sum(b.name=='persona_expression' for b in blocks)!=1:raise RuntimeErrorBase('punctuation_instruction_boundary_changed')
    result=[];version=renderer.INSTRUCTION_VERSION+'-punctuation-delegated-v1'
    for block in blocks:
        if block.name=='persona_expression':
            if not block.content.startswith(renderer.INSTRUCTION):raise RuntimeErrorBase('punctuation_instruction_boundary_changed')
            content=renderer.INSTRUCTION_WITHOUT_PUNCTUATION+block.content[len(renderer.INSTRUCTION):]
            block=PromptBlock(block.name,content,block.owner,version)
        result.append(block)
    return result,version
