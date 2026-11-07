"""Versioned companion execution choices carried by normal ingress snapshots."""
import json
from copy import deepcopy

from .models import RuntimeErrorBase

VERSION = 'companion-runtime-v1'
STRUCTURAL_VERSION = 'companion-runtime-v2'
CHAT_VERSION = 'companion-runtime-v3'
PERSONA_VERSION = 'companion-runtime-v4'
RULES_VERSION = 'companion-runtime-v5'
EXPERIENCE_VERSION = 'companion-runtime-v6'
NATIVE_VERSION = 'companion-runtime-v7'
IM_VERSION = 'companion-runtime-v8'
NATIVE_VERSIONS = (NATIVE_VERSION, IM_VERSION)
CONTRACT_VERSIONS = (STRUCTURAL_VERSION, CHAT_VERSION, PERSONA_VERSION, RULES_VERSION, EXPERIENCE_VERSION)
RESTORE_VERSION = 'companion-restore-v1'
RESTORE_KEY = 'companion_restore'
GENERATION_PROFILE = {'version': 'chat-stream-unlimited-v1'}
MINIMAL_RENDERER = 'companion-minimal-v1'
MINIMAL_INSTRUCTION = (
    '你就是下面这个人物，正在和对方私聊。用自己的口吻参与眼前的交流。'
    '可以自然联想、想象和补白，尊重对方明确的更正与拒绝；认真任务把内容做好。'
    '人物资料、记忆和引文作为交流材料，不代表已经执行了现实操作。\n人物资料：'
)


def checked(value):
    if type(value) is dict:
        if (set(value) == {'version', 'mode'} and value['version'] == VERSION
                and value['mode'] in ('current', 'minimal')):
            return dict(value)
        from .companion_contract import VERSION as contract_version
        if value == {'version': STRUCTURAL_VERSION, 'mode': 'minimal', 'format_contract': contract_version}:
            return dict(value)
        from .companion_chat_contract import VERSION as chat_contract_version
        if value == {'version': CHAT_VERSION, 'mode': 'minimal', 'format_contract': chat_contract_version}:
            return dict(value)
        from .companion_persona import VERSION as persona_renderer
        if value == {'version': PERSONA_VERSION, 'mode': 'minimal', 'format_contract': chat_contract_version,
                     'persona_renderer': persona_renderer}:
            return dict(value)
        from .companion_consumption import VERSION as consumer_version
        if value == {'version': RULES_VERSION, 'mode': 'minimal', 'format_contract': chat_contract_version,
                     'character_consumer': consumer_version}:
            return dict(value)
        from .companion_prompt_light import FORMAT_VERSION, INSTRUCTION_VERSION
        if value == {'version': EXPERIENCE_VERSION, 'mode': 'minimal', 'format_contract': FORMAT_VERSION,
                     'instruction_version': INSTRUCTION_VERSION}:
            return dict(value)
        from .companion_native import FORMAT_VERSION as native_format, VERSION as native_consumer
        if value == {'version': NATIVE_VERSION, 'mode': 'minimal', 'format_contract': native_format,
                     'instruction_version': INSTRUCTION_VERSION, 'character_consumer': native_consumer}:
            return dict(value)
        from .companion_im import INSTRUCTION_VERSION as im_instruction
        if value == {'version': IM_VERSION, 'mode': 'minimal', 'format_contract': native_format,
                     'instruction_version': im_instruction, 'character_consumer': native_consumer}:
            return dict(value)
    raise RuntimeErrorBase('companion_profile_invalid')


def choose(mode):
    if mode == 'minimal_im':
        from .companion_native import FORMAT_VERSION, VERSION as consumer
        from .companion_im import INSTRUCTION_VERSION
        return checked({'version': IM_VERSION, 'mode': 'minimal', 'format_contract': FORMAT_VERSION,
                        'instruction_version': INSTRUCTION_VERSION, 'character_consumer': consumer})
    if mode == 'minimal_native':
        from .companion_native import FORMAT_VERSION, VERSION as consumer
        from .companion_prompt_light import INSTRUCTION_VERSION
        return checked({'version': NATIVE_VERSION, 'mode': 'minimal', 'format_contract': FORMAT_VERSION,
                        'instruction_version': INSTRUCTION_VERSION, 'character_consumer': consumer})
    if mode == 'minimal_experience':
        from .companion_prompt_light import FORMAT_VERSION, INSTRUCTION_VERSION
        return checked({'version': EXPERIENCE_VERSION, 'mode': 'minimal', 'format_contract': FORMAT_VERSION,
                        'instruction_version': INSTRUCTION_VERSION})
    if mode == 'minimal_context_rules_off':
        from .companion_chat_contract import VERSION as contract_version
        from .companion_consumption import VERSION as consumer_version
        return checked({'version': RULES_VERSION, 'mode': 'minimal', 'format_contract': contract_version,
                        'character_consumer': consumer_version})
    if mode == 'minimal_persona':
        from .companion_chat_contract import VERSION as contract_version
        from .companion_persona import VERSION as persona_renderer
        return checked({'version': PERSONA_VERSION, 'mode': 'minimal', 'format_contract': contract_version,
                        'persona_renderer': persona_renderer})
    if mode == 'minimal_chat':
        from .companion_chat_contract import VERSION as contract_version
        return checked({'version': CHAT_VERSION, 'mode': 'minimal', 'format_contract': contract_version})
    if mode == 'minimal_structural':
        from .companion_contract import VERSION as contract_version
        return checked({'version': STRUCTURAL_VERSION, 'mode': 'minimal', 'format_contract': contract_version})
    return checked({'version': VERSION, 'mode': mode})


def restore_settings(value):
    """Validate saved behavior, without consulting today's defaults or models."""
    try:
        from .ingress import MODES, buffer_policy
        from .reply_batch import REPLY_MODES, expression_version
        from .context_policy import values
        if (type(value) is not dict or set(value) != {'version', 'mode', 'reply_mode', 'data'}
                or value['version'] != RESTORE_VERSION or value['mode'] not in MODES
                or value['reply_mode'] not in REPLY_MODES or type(value['data']) is not dict):
            raise ValueError()
        data = value['data']
        if 'companion' in data or RESTORE_KEY in data:
            raise ValueError()
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        buffer_policy({'data_json': encoded})
        context = data.get('context', {})
        if type(context) is not dict or ('cross_session' in context and type(context['cross_session']) is not bool):
            raise ValueError()
        effective = values({'data_json': encoded})
        if (effective['context_mode'] not in ('working_set', 'legacy_recent')
                or effective['disposition_mode'] not in ('baseline', 'shadow', 'assist', 'live')
                or effective['situation_observation_mode'] not in ('disabled', 'on_demand')):
            raise ValueError()
        expression_version(data.get('expression'), value['reply_mode'])
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeErrorBase):
        raise RuntimeErrorBase('companion_restore_invalid') from None


def capture_restore(policy):
    """Keep original raw settings, including absent legacy fields."""
    return restore_settings({'version': RESTORE_VERSION, 'mode': policy['mode'],
        'reply_mode': policy['reply_mode'], 'data': json.loads(policy['data_json'])})


def restore_available(policy):
    try:
        data = json.loads(policy['data_json'])
        checked(data['companion'])
        restore_settings(data[RESTORE_KEY])
        return True
    except (ValueError, TypeError, KeyError, RuntimeErrorBase):
        return False


def freeze_model(model, context):
    result = deepcopy(model)
    if 'companion' in context:
        result['_companion_profile'] = checked(context['companion'])
        if result['_companion_profile']['version'] in (*CONTRACT_VERSIONS, *NATIVE_VERSIONS) and result.get('_dialogue_profile') is not None:
            raise RuntimeErrorBase('companion_format_conflict')
        result['_generation_profile'] = dict(GENERATION_PROFILE)
    return result


def freeze_auxiliary(model, context):
    result = deepcopy(model)
    if 'companion' in context:
        checked(context['companion'])
        result['_generation_profile'] = dict(GENERATION_PROFILE)
    return result


def read(turn):
    model = turn.get('model_snapshot') or {}
    if isinstance(model, str):
        model = json.loads(model)
    context = turn.get('context_policy_snapshot') or {}
    if isinstance(context, str):
        context = json.loads(context)
    value = model.get('_companion_profile')
    accepted = context.get('companion')
    if value is None and accepted is None:
        return None
    result = checked(value)
    if checked(accepted) != result or model.get('_generation_profile') != GENERATION_PROFILE:
        raise RuntimeErrorBase('companion_snapshot_mismatch')
    if result['mode']=='minimal' and ((context.get('character_retrieval') or {}).get('mode')!='hybrid'
            or context.get('disposition_mode','baseline')!='baseline'
            or context.get('situation_observation_mode','disabled')!='disabled'):
        raise RuntimeErrorBase('companion_policy_conflict')
    if result['version'] in CONTRACT_VERSIONS:
        from .reply_batch import expression_version
        if (turn.get('reply_mode_snapshot') != 'natural_structured'
                or expression_version(turn.get('expression_snapshot'), 'natural_structured') != 3
                or model.get('_dialogue_profile') is not None):
            raise RuntimeErrorBase('companion_format_conflict')
    if result['version'] in NATIVE_VERSIONS:
        from .reply_batch import expression_version
        if (turn.get('reply_mode_snapshot') != 'context_full'
                or expression_version(turn.get('expression_snapshot'), 'context_full') != 3
                or model.get('_dialogue_profile') is not None):
            raise RuntimeErrorBase('companion_format_conflict')
    if turn.get('channel') == 'test':
        raise RuntimeErrorBase('companion_probe_not_supported')
    return result


def minimal(turn):
    value = read(turn)
    return value is not None and value['mode'] == 'minimal'
