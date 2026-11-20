"""E1授权材料投影及MaiBot原文分段适配；不派发请求或写业务事件。"""
import json
import re

from persona_runtime.expression_contract import ProtectedSpan
from .profile import model_persona_data
from .vendor.maibot_expression_components import (
    _extract_trailing_comma, _is_no_split_marker, _map_sentences_to_original,
    _strip_whitespace, build_expression_habits_block, calculate_typing_time,
    normalize_selected_ids, ProcessedResponseSegment,
)

WIRE_VERSION = 'expression-model-payload-v2'
PROMPT_VERSION = 'expression-maibot-private-v1'
TIMING_VERSION = 'expression-e1-ready-time-v1'

# Adapted from MaiBot prompts/zh-CN/main/maisaka_replyer.prompt at 478f671.
# License: GNU GPL version 3; docs/licenses/MaiBot-GPL-3.0.txt.
REPLYER_TEMPLATE = '''{identity}
现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，
{reply_style}
{replyer_output_instruction}'''

OUTPUT_INSTRUCTION = '''把主稿里要说的内容接到眼前的私聊里，用人物此刻的语气表达。
保留当前任务、必要条件与否定、明确承诺。保护片段逐字保留。
已经说过的内容是交流前提，本次接着说；表达习惯只提供说法参考，其中没有新增的人物经历或用户事实。
输出一个JSON对象，字段为body、segments、selected_expression_ids。body保存完整实际发言，分析和编辑说明留在正文之外。
segments为从body原文按话意分出的字符串数组；需要整块阅读时填"no_split"。每段只能用body已有文字，顺序和全部内容保持。
selected_expression_ids填写本次实际参考的候选ID数组。
JSON结构示意，正文占位处替换为本次实际发言：
{"body":"实际回复全文","segments":["实际回复全文"],"selected_expression_ids":[]}'''


class ExpressionPayloadError(ValueError):
    pass


def offered_candidates(material, fixtures, *, with_assets):
    if not with_assets:
        return []
    assets = {item['id']: item for item in fixtures['assets']}
    candidates = []
    for ident in material['candidate_ids']:
        item = assets.get(ident)
        if item is None or item['persona'] != material['persona']:
            raise ExpressionPayloadError('candidate_scope_changed')
        candidates.append({key: item[key] for key in ('id', 'situation', 'style')})
    return candidates


def build_messages(material, fixtures, *, with_assets=True):
    role = model_persona_data(material['role_projection'])
    projection = {key: role[key] for key in ('name', 'core_traits', 'expression_style', 'style', 'values_and_boundaries') if key in role}
    candidates = offered_candidates(material, fixtures, with_assets=with_assets)
    style = build_expression_habits_block(candidates)
    if candidates:
        style += '\n候选ID：\n' + '\n'.join(f"{item['id']}: {item['style']}" for item in candidates)
    identity = '你是' + role['name'] + '。人物资料：\n' + json.dumps(projection, ensure_ascii=False)
    messages = [{'role': 'system', 'content': REPLYER_TEMPLATE.format(
        identity=identity, reply_style=style, replyer_output_instruction=OUTPUT_INSTRUCTION)}]
    for line in material['history']:
        if line['speaker'] not in ('user', 'assistant') or not isinstance(line['text'], str):
            raise ExpressionPayloadError('history_projection_invalid')
        messages.append({'role': line['speaker'], 'content': line['text']})
    protected = [{'id': span['span_id'], 'text': material['draft'][span['start']:span['end']]}
                 for span in material['protected']]
    current = {'当前消息': material['user'], '本轮已经说过': material['already_spoken'],
               '主稿': material['draft'], '保护片段': protected}
    messages.append({'role': 'user', 'content': json.dumps(current, ensure_ascii=False)})
    return messages, candidates


def map_sentences(body, proposals):
    if (isinstance(proposals, str) and _is_no_split_marker(proposals)) or proposals == ['no_split']:
        return [body]
    if not isinstance(proposals, list) or not proposals or not all(isinstance(v, str) and v.strip() for v in proposals):
        raise ExpressionPayloadError('segments_invalid')
    if _strip_whitespace(''.join(proposals)) != _strip_whitespace(body):
        raise ExpressionPayloadError('segments_changed_body')
    pieces = _map_sentences_to_original(body, proposals)
    if ''.join(pieces) != body:
        raise ExpressionPayloadError('segments_lost_characters')
    return pieces


def protected_in_expression(body, material):
    occupied = []
    spans = []
    for original in material['protected']:
        text = material['draft'][original['start']:original['end']]
        cursor = 0
        while True:
            start = body.find(text, cursor)
            if start < 0:
                raise ExpressionPayloadError('protected_text_changed')
            end = start + len(text)
            if all(end <= a or start >= b for a, b in occupied):
                break
            cursor = start + 1
        occupied.append((start, end))
        spans.append(ProtectedSpan(original['span_id'], start, end, original['kind']))
    return tuple(sorted(spans, key=lambda item: item.start))


def bubble_plan(body, spans, pieces):
    result = []
    start = 0
    remaining = 6000
    for index, piece in enumerate(pieces):
        end = start + len(piece)
        if any(span.start < end < span.end for span in spans):
            raise ExpressionPayloadError('protected_text_split')
        text, separator = _extract_trailing_comma(piece)
        frame = ProcessedResponseSegment(text=text, separator=separator)
        wait = 0 if index == 0 else min(1200, remaining, round(calculate_typing_time(piece, typing_speed=0.08) * 1000))
        remaining -= wait
        literal = any(start <= span.start and span.end <= end for span in spans)
        result.append({'start': start, 'end': end, 'text': frame.text + frame.separator,
                       'kind': 'technical' if literal else 'chat', 'required': True,
                       'delay_ms': wait, 'upstream_separator': separator})
        start = end
    if start != len(body) or ''.join(item['text'] for item in result) != body:
        raise ExpressionPayloadError('incomplete_presentation_ranges')
    return result


def local_baseline_plan(material):
    body = material['draft']
    spans = tuple(ProtectedSpan(**span) for span in material['protected'])
    boundaries = []
    previous = 0
    for match in re.finditer(r'\n\s*\n', body):
        end = match.end()
        if body[previous:end].strip() and body[end:].strip() and not any(s.start < end < s.end for s in spans):
            boundaries.append(end)
            previous = end
    pieces = []
    start = 0
    for end in (*boundaries, len(body)):
        pieces.append(body[start:end])
        start = end
    return bubble_plan(body, spans, pieces)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExpressionPayloadError('duplicate_json_key')
        result[key] = value
    return result


def decode_output(raw, material, candidates):
    if not isinstance(raw, str) or not raw.strip():
        raise ExpressionPayloadError('empty_expression_transport')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ExpressionPayloadError('invalid_json_constant')))
    except json.JSONDecodeError:
        raise ExpressionPayloadError('invalid_expression_json') from None
    if not isinstance(value, dict) or set(value) - {'body', 'segments', 'selected_expression_ids'}:
        raise ExpressionPayloadError('invalid_expression_envelope')
    body = value.get('body')
    if not isinstance(body, str) or not body.strip():
        raise ExpressionPayloadError('empty_expression_body')
    spans = protected_in_expression(body, material)
    selected = normalize_selected_ids(value, candidates)
    notes = []
    if value.get('selected_expression_ids', []) != selected:
        notes.append('selected_ids_normalized')
    try:
        pieces = map_sentences(body, value.get('segments'))
        plan = bubble_plan(body, spans, pieces)
    except (ExpressionPayloadError, IndexError) as error:
        notes.append(str(error) if isinstance(error, ExpressionPayloadError) else 'segments_mapping_invalid')
        plan = bubble_plan(body, spans, [body])
    return {'body': body, 'segments': plan, 'protected': [vars(s) for s in spans],
            'selected_expression_ids': selected, 'notes': notes,
            'plan_fallback': bool(any(n != 'selected_ids_normalized' for n in notes))}
