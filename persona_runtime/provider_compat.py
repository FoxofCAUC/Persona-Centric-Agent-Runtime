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
CURRENT_DEEPSEEK=frozenset(('deepseek-flash','deepseek-v4-pro','deepseek-v4-flash','deepseek-v4-flash-vision-exp'))
LEGACY_DEEPSEEK=frozenset(('deepseek-chat','deepseek-reasoner'))


def is_deepseek(model):
    return (urlsplit(model.get('base_url','')).hostname=='api.deepseek.com' or
            model.get('name','').strip().lower() in CURRENT_DEEPSEEK|LEGACY_DEEPSEEK)


def endpoint(model,resource='chat/completions'):
    if resource not in ('chat/completions','models'):raise ValueError('invalid_provider_resource')
    base=model.get('base_url','').rstrip('/')
    if is_deepseek(model):
        path=urlsplit(base).path.rstrip('/')
        if path.startswith('/anthropic') or path.endswith('/responses'):
            raise ValueError('provider_endpoint_incompatible')
        if path.endswith('/chat/completions'):base=base[:-len('/chat/completions')]
    return base+'/'+resource


def _format(model):
    schema=response_format(model.get('_reply_mode'),model.get('_expression_snapshot'))
    wants_json=bool(schema or model.get('_json_output') or model.get('_disposition_compile'))
    if is_deepseek(model):return {'type':'json_object'} if wants_json else None
    return None if model.get('_disposition_compile') else schema


def describe_request(model):
    fmt=_format(model);deepseek=is_deepseek(model)
    # Legacy aliases may refer to older single-mode APIs at third-party gateways.
    # Do not pretend a thinking-only legacy model was made non-thinking.
    thinking='disabled' if deepseek and model.get('name','').strip().lower() not in LEGACY_DEEPSEEK else 'provider_default'
    return {'version':VERSION,'profile':'deepseek-chat-v1' if deepseek else 'openai-compatible-v1',
            'response_format':fmt['type'] if fmt else 'text','thinking':thinking,
            'format_hash':fingerprint(fmt) if fmt else None,'application_validation_required':bool(fmt)}


def build_request(model,messages):
    contract=describe_request(model);fmt=_format(model)
    if contract['profile']=='deepseek-chat-v1' and fmt and not any(
            m.get('role') in ('system','user') and isinstance(m.get('content'),str) and 'json' in m['content'].lower() for m in messages):
        # Do not silently alter a frozen prompt or pay for an invalid JSON-mode
        # request. All normal structured PCAR purposes already supply a contract.
        raise ValueError('provider_json_instruction_missing')
    payload={'model':model['name'],'messages':messages,'max_tokens':model['max_tokens'],'stream':False}
    if fmt:payload['response_format']=fmt
    if contract['thinking']=='disabled':payload['thinking']={'type':'disabled'}
    if model.get('_dialogue_profile') is not None:
        from .dialogue_voice import read
        profile=read({'model_snapshot':model})
        payload['temperature']=profile['temperature']
    return payload,contract


def usage_fields(value,*,deepseek=False):
    if not isinstance(value,dict):return None
    names=('prompt_tokens','completion_tokens','total_tokens')
    if deepseek:names+=('prompt_cache_hit_tokens','prompt_cache_miss_tokens')
    result={k:v for k,v in value.items() if k in names and type(v) is int and v>=0}
    if deepseek:
        for outer,inner in (('prompt_tokens_details','cached_tokens'),('completion_tokens_details','reasoning_tokens')):
            item=value.get(outer)
            if isinstance(item,dict) and type(item.get(inner)) is int and item[inner]>=0:result[outer]={inner:item[inner]}
    return result


def response_metadata(data):
    """Public identifiers only. Never return reasoning_content or other text."""
    result={}
    if isinstance(data.get('model'),str) and data['model'] in CURRENT_DEEPSEEK|LEGACY_DEEPSEEK:result['response_model']=data['model']
    value=data.get('system_fingerprint')
    if isinstance(value,str) and re.fullmatch(r'[a-fA-F0-9]{16,128}',value):result['system_fingerprint']=value
    return result
