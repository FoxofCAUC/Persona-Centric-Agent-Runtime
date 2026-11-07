"""Deterministic provider wire contracts; no config writes, probes or retries.

DeepSeek's Chat Completions supports json_object rather than JSON Schema.
The application validators remain authoritative. Current DeepSeek models default
to thinking; this interactive profile explicitly disables it within the existing
output and timeout limits. Unrecognized gateways keep the original protocol.
"""
import re
from urllib.parse import urlsplit

from .archive import fingerprint
from .reply_protocol import response_format

VERSION='provider-wire-v1'
STREAM_UNLIMITED_VERSION='chat-stream-unlimited-v1'
NATIVE_TEXT_VERSION='native-text-low-v1'
CURRENT_DEEPSEEK=frozenset(('deepseek-flash','deepseek-v4-pro','deepseek-v4-flash','deepseek-v4-flash-vision-exp'))
LEGACY_DEEPSEEK=frozenset(('deepseek-chat','deepseek-reasoner'))


def is_grok47(model):
    # A routed model name selects a wire profile, not a claim of xAI provenance.
    return not is_deepseek(model) and model.get('name','').strip().lower()=='grok-4.7'


def generation_profile(model):
    """Only explicitly frozen new requests opt into the unlimited SSE path."""
    value=model.get('_generation_profile')
    if value is None:return None
    if type(value) is not dict or value!={'version':STREAM_UNLIMITED_VERSION}:
        raise ValueError('generation_profile_invalid')
    return value


def native_text_profile(model):
    """Explicit plain-text reference requests retain the existing Grok low setting."""
    if '_native_text_profile' not in model:return None
    value=model['_native_text_profile']
    if type(value) is not dict or value!={'version':NATIVE_TEXT_VERSION}:
        raise ValueError('native_text_profile_invalid')
    if not is_grok47(model):raise ValueError('native_text_model_unsupported')
    if (model.get('_reply_mode') not in (None,'legacy_full') or
            any(key in model for key in ('_expression_snapshot','_character_selection',
                '_json_output','_disposition_compile','_dialogue_profile','_companion_profile'))):
        raise ValueError('native_text_profile_conflict')
    return value


def request_timeout(model):
    return None if generation_profile(model) else model['timeout']


def grok_interactive(model):
    profile = model.get('_companion_profile')
    if is_grok47(model) and isinstance(profile, dict):
        from .companion import NATIVE_VERSIONS, checked
        if profile.get('version') in NATIVE_VERSIONS:
            checked(profile)
            if model.get('_reply_mode') != 'context_full':
                raise ValueError('companion_native_wire_conflict')
            return True
    return is_grok47(model) and (model.get('_character_selection') is True or
        model.get('_reply_mode') in ('natural_structured','context_structured'))


def selector_format():
    properties={'schema_version':{'type':'integer','enum':[1]},
                'lore_ids':{'type':'array','maxItems':4,'items':{'type':'string'}},
                'example_ids':{'type':'array','maxItems':4,'items':{'type':'string'}}}
    return {'type':'json_schema','json_schema':{'name':'pcar_character_selection_v1','strict':True,
        'schema':{'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}}}


def is_deepseek(model):
    return (urlsplit(model.get('base_url','')).hostname=='api.deepseek.com' or
            model.get('name','').strip().lower() in CURRENT_DEEPSEEK|LEGACY_DEEPSEEK)


def endpoint(model,resource='chat/completions'):
    if resource not in ('chat/completions','models'):raise ValueError('invalid_provider_resource')
    base=model.get('base_url','').rstrip('/')
    if is_deepseek(model) or is_grok47(model):
        path=urlsplit(base).path.rstrip('/')
        if path.startswith('/anthropic') or path.endswith('/responses'):
            raise ValueError('provider_endpoint_incompatible')
        if path.endswith('/chat/completions'):base=base[:-len('/chat/completions')]
    return base+'/'+resource


def _format(model):
    schema=response_format(model.get('_reply_mode'),model.get('_expression_snapshot'))
    wants_json=bool(schema or model.get('_json_output') or model.get('_disposition_compile'))
    if is_deepseek(model):return {'type':'json_object'} if wants_json else None
    if is_grok47(model) and model.get('_character_selection') is True:return selector_format()
    return None if model.get('_disposition_compile') else schema


def describe_request(model):
    native=native_text_profile(model)
    generation=generation_profile(model)
    fmt=_format(model);deepseek=is_deepseek(model)
    # Legacy aliases may refer to older single-mode APIs at third-party gateways.
    # Do not pretend a thinking-only legacy model was made non-thinking.
    thinking='disabled' if deepseek and model.get('name','').strip().lower() not in LEGACY_DEEPSEEK else 'provider_default'
    result={'version':VERSION,'profile':'deepseek-chat-v1' if deepseek else 'openai-compatible-v1',
            'response_format':fmt['type'] if fmt else 'text','thinking':thinking,
            'format_hash':fingerprint(fmt) if fmt else None,'application_validation_required':bool(fmt)}
    if grok_interactive(model) or native:result.update(version='provider-wire-grok47-v1',profile='grok47-chat-v1',reasoning_effort='low')
    if native:result.update(version='provider-wire-native-text-low-v1',native_text_profile=NATIVE_TEXT_VERSION)
    if generation:result.update(generation_version=STREAM_UNLIMITED_VERSION,stream=True,
                                output_limit='omitted',timeout_policy='none')
    return result


def build_request(model,messages):
    contract=describe_request(model);fmt=_format(model)
    if contract['profile']=='deepseek-chat-v1' and fmt and not any(
            m.get('role') in ('system','user') and isinstance(m.get('content'),str) and 'json' in m['content'].lower() for m in messages):
        # Do not silently alter a frozen prompt or pay for an invalid JSON-mode
        # request. All normal structured PCAR purposes already supply a contract.
        raise ValueError('provider_json_instruction_missing')
    if generation_profile(model):
        payload={'model':model['name'],'messages':messages,'stream':True,'stream_options':{'include_usage':True}}
    else:payload={'model':model['name'],'messages':messages,'max_tokens':model['max_tokens'],'stream':False}
    if fmt:payload['response_format']=fmt
    if contract['thinking']=='disabled':payload['thinking']={'type':'disabled'}
    if contract.get('reasoning_effort')=='low':payload['reasoning_effort']='low'
    if model.get('_dialogue_profile') is not None:
        from .dialogue_voice import read
        profile=read({'model_snapshot':model})
        payload['temperature']=profile['temperature']
    return payload,contract


def usage_fields(value,*,deepseek=False,grok=False):
    if not isinstance(value,dict):return None
    names=('prompt_tokens','completion_tokens','total_tokens')
    if deepseek:names+=('prompt_cache_hit_tokens','prompt_cache_miss_tokens')
    result={k:v for k,v in value.items() if k in names and type(v) is int and v>=0}
    if deepseek or grok:
        for outer,inner in (('prompt_tokens_details','cached_tokens'),('completion_tokens_details','reasoning_tokens')):
            item=value.get(outer)
            if isinstance(item,dict) and type(item.get(inner)) is int and item[inner]>=0:result[outer]={inner:item[inner]}
    return result


def response_metadata(data,*,grok=False):
    """Public identifiers only. Never return reasoning_content or other text."""
    result={}
    if isinstance(data.get('model'),str) and data['model'] in CURRENT_DEEPSEEK|LEGACY_DEEPSEEK:result['response_model']=data['model']
    if grok and isinstance(data.get('model'),str) and re.fullmatch(r'grok-4\.7(?:[-._a-zA-Z0-9]{0,64})',data['model']):result['response_model']=data['model']
    value=data.get('system_fingerprint')
    if isinstance(value,str) and re.fullmatch(r'[a-fA-F0-9]{16,128}',value):result['system_fingerprint']=value
    elif grok and isinstance(value,str) and re.fullmatch(r'fp_[a-fA-F0-9]{8,128}',value):result['system_fingerprint']=value
    return result
