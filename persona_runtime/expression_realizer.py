"""E1修复候选：JSON职责分离及MaiBot式正文协议，首批生成模块保持。"""
import json

from .expression_protocol import (ExpressionPayloadError, build_messages as original_messages,
                   decode_output, local_baseline_plan, protected_in_expression)


JSON_PROTOCOL = 'expression-json-scoped-v3'
TEXT_PROTOCOL = 'expression-text-local-v1'
PROTOCOLS = (JSON_PROTOCOL, TEXT_PROTOCOL)

CONTENT_TASK = '''把主稿里要说的内容接到眼前的私聊里，用人物此刻的语气表达。
保留当前任务、必要条件与否定、明确承诺。保护片段逐字保留。
已经说过的内容是交流前提，本次接着说；表达习惯只提供说法参考，其中没有新增的人物经历或用户事实。'''

JSON_TASK = '''响应是一个有效JSON对象，字段为body、segments、selected_expression_ids。
body是人物本次实际发给对方的完整话语。人物身份和口语要求只作用于body中的话语。
segments是从body原文按话意分出的字符串数组；整块阅读时填"no_split"。每段只用body原文，顺序和全部内容保持。
selected_expression_ids是本次参考的候选ID数组。
结构示意中的正文占位值需替换为本次实际发言：
{"body":"实际发言全文","segments":["实际发言全文"],"selected_expression_ids":[]}'''


def build_messages(material, fixtures, protocol, *, with_assets=True):
    """沿用已核对的角色、实际历史及资料投影，仅调整任务的运输职责。"""
    original, candidates = original_messages(material, fixtures, with_assets=with_assets)
    from .expression_protocol import OUTPUT_INSTRUCTION
    system = original[0]['content']
    if system.count(OUTPUT_INSTRUCTION) != 1:
        raise ExpressionPayloadError('original_prompt_boundary_changed')
    if protocol == JSON_PROTOCOL:
        spoken = '现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，'
        if system.count(spoken) != 1:
            raise ExpressionPayloadError('replyer_prompt_boundary_changed')
        system = system.replace(spoken, '读之前的聊天记录，把握当前话题，在JSON的body字段中写出日常且口语化的人物发言。', 1)
        system = '返回一个JSON对象，人物发言写在body中。\n' + system.replace(OUTPUT_INSTRUCTION, CONTENT_TASK + '\n' + JSON_TASK, 1)
        last = original[-1]['content'] + '\n请把以上材料中的本次人物发言写进body，并直接返回约定的整个JSON对象。'
    elif protocol == TEXT_PROTOCOL:
        system = system.replace(OUTPUT_INSTRUCTION, CONTENT_TASK + '\n直接给出人物要发给对方的正文，编辑说明留在正文之外。', 1)
        last = original[-1]['content'] + '\n接着这段私聊，把这次想对对方说的话发出来。'
    else:
        raise ExpressionPayloadError('unsupported_repair_protocol')
    return [{'role': 'system', 'content': system}, *original[1:-1], {'role': 'user', 'content': last}], candidates


def decode_output_for(protocol, raw, material, candidates):
    if protocol == JSON_PROTOCOL:
        result = decode_output(raw, material, candidates)
        result['segmentation_source'] = 'model_proposal_mapped_to_body'
        result['selection_declaration'] = 'model_reported'
        return result
    if protocol != TEXT_PROTOCOL:
        raise ExpressionPayloadError('unsupported_repair_protocol')
    if not isinstance(raw, str) or not raw.strip():
        raise ExpressionPayloadError('empty_expression_transport')
    # 识别旧运输对象，避免把内部包体当成人物正文；普通任务中的JSON原样片段仍由保护合同管理。
    try:
        envelope = json.loads(raw)
    except (ValueError, TypeError):
        envelope = None
    verbatim_request = any(material['draft'][s['start']:s['end']] == raw for s in material['protected'])
    if isinstance(envelope, dict) and set(envelope) == {'body', 'segments', 'selected_expression_ids'} and not verbatim_request:
        raise ExpressionPayloadError('plain_expression_received_internal_envelope')
    spans = protected_in_expression(raw, material)
    projection = dict(material, draft=raw, protected=[vars(span) for span in spans])
    return {'body': raw, 'segments': local_baseline_plan(projection),
            'protected': [vars(span) for span in spans], 'selected_expression_ids': [],
            'selection_declaration': 'unreported', 'offered_expression_ids': [c['id'] for c in candidates],
            'segmentation_source': 'local_paragraphs_preserving_body', 'notes': [], 'plan_fallback': False}
