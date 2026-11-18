"""Strict Flash decision wire. The function is a data carrier for local routing."""
import json

from .archive import fingerprint

VERSION = 'deepseek-flash-reply-decision-tool-v1'
PROFILE = '_reply_decision_tool'
BASE = 'https://api.deepseek.com/beta'
MODEL = 'deepseek-flash'
FUNCTION = 'record_reply_decision'
FIELDS = ('target', 'action', 'task_state')
ACTIONS = ('参与交流', '回应求助', '承接转题或收尾')
STATES = ('无待办', '进行中', '已收束')
UNDETERMINED = '未定'


def model_profile(model):
    value = model.get(PROFILE)
    if (model.get('name') != MODEL or model.get('base_url') != BASE
            or type(model.get('max_tokens')) is not int or model['max_tokens'] != 128
            or not isinstance(value, dict) or set(value) != {'version', 'target_count'}
            or value['version'] != VERSION or type(value['target_count']) is not int
            or value['target_count'] < 1
            or any(k in model for k in ('_json_output', '_reasoning_effort', '_native_web_profile',
                                        '_generation_profile', '_reply_mode', '_dialogue_profile'))):
        raise ValueError('reply_decision_profile_invalid')
    return value


def tool(target_count):
    return {'type': 'function', 'function': {'name': FUNCTION, 'strict': True, 'parameters': {
        'type': 'object',
        'properties': {
            'target': {'type': 'integer', 'minimum': 0, 'maximum': target_count},
            'action': {'type': 'string', 'enum': [*ACTIONS, UNDETERMINED]},
            'task_state': {'type': 'string', 'enum': [*STATES, UNDETERMINED]},
        },
        'required': list(FIELDS),
        'additionalProperties': False,
    }}}


def describe(model):
    n = model_profile(model)['target_count']
    return {'version': VERSION, 'profile': 'deepseek-reply-decision-tool-v1',
            'response_format': 'tool_calls', 'thinking': 'disabled', 'tool_name': FUNCTION,
            'format_hash': fingerprint(tool(n)), 'application_validation_required': True}


def build_request(model, messages):
    contract = describe(model)
    wire = {'model': MODEL, 'messages': messages, 'max_tokens': 128, 'stream': False,
            'thinking': {'type': 'disabled'}, 'tools': [tool(model[PROFILE]['target_count'])],
            'tool_choice': {'type': 'function', 'function': {'name': FUNCTION}}}
    return wire, contract


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('reply_decision_duplicate_field')
    return result


def parse_receipt(raw, wire):
    """Validate one named function record. The result never contains free-form output."""
    from .provider_compat import usage_fields
    model = finish = usage = args_digest = None
    try:
        packet = json.loads(raw, object_pairs_hook=_unique)
        usage = usage_fields(packet.get('usage'), deepseek=True)
        model = packet.get('model')
        choice, = packet['choices']
        message = choice['message']
        finish = choice.get('finish_reason')
        if model != MODEL or model != wire['model']:
            raise ValueError('reply_decision_model_changed')
        if (finish != 'tool_calls' or type(choice.get('index', 0)) is not int
                or choice.get('index', 0) != 0 or message.get('role') != 'assistant'
                or message.get('refusal') or message.get('function_call')):
            raise ValueError('reply_decision_tool_required')
        spoken = message.get('content')
        if spoken is not None and (not isinstance(spoken, str) or spoken.strip()):
            raise ValueError('reply_decision_extra_text')
        calls = message.get('tool_calls')
        if not isinstance(calls, list) or len(calls) != 1:
            raise ValueError('reply_decision_single_tool_required')
        call = calls[0]
        function = call['function']
        if call.get('type') != 'function' or function.get('name') != FUNCTION:
            raise ValueError('reply_decision_wrong_tool')
        limit = wire['tools'][0]['function']['parameters']['properties']['target']['maximum']
        raw_args = function['arguments']
        if not isinstance(raw_args, str) or len(raw_args.encode('utf-8')) > 4096:
            raise ValueError('reply_decision_bad_arguments')
        args = json.loads(raw_args, object_pairs_hook=_unique)
        if not isinstance(args, dict) or set(args) != set(FIELDS):
            raise ValueError('reply_decision_fields')
        target, action, state = (args[k] for k in FIELDS)
        if type(target) is not int or not 0 <= target <= limit:
            raise ValueError('reply_decision_target_range')
        if target == 0:
            if action != UNDETERMINED or state != UNDETERMINED:
                raise ValueError('reply_decision_undetermined_pairing')
            target = None
        elif action not in ACTIONS or state not in STATES:
            raise ValueError('reply_decision_value')
        normalized = {'target': target, 'action': action, 'task_state': state}
        content = json.dumps(normalized, ensure_ascii=False, separators=(',', ':'))
        return {'body': content, 'model': model, 'finish_reason': finish, 'usage': usage,
                'valid': True, 'incomplete_stream': False, 'error_code': None}
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        code = str(error) if type(error) is ValueError and str(error).startswith('reply_decision_') else 'reply_decision_receipt_invalid'
        return {'body': None, 'model': model if model == MODEL else None,
                'finish_reason': finish if finish in ('stop','tool_calls','length','content_filter','insufficient_system_resource') else None,
                'usage': usage, 'valid': False, 'incomplete_stream': False, 'error_code': code}
