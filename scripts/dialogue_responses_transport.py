"""Isolated, uncapped Responses wire adapter for the CR3-A.1 experiment.

The configured gateway/model are preserved. Text-only transcript Items stay in
their original order; no system prompt is moved into ``instructions``. Raw HTTP
``output`` Items, rather than the SDK convenience ``output_text`` property, are
the response source. This module neither retries nor changes formal settings.
"""
import asyncio
from copy import deepcopy
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion, ModelError, model_tls_context
from persona_runtime.security import unprotect
from scripts.dialogue_anthropic_transport import public_provider_error

VERSION = 'openai-responses-uncapped-v1'
PROFILE = 'openai-responses-v1'


def transport_contract():
    return {'version': VERSION, 'max_tokens_mode': 'omit',
            'max_output_tokens_mode': 'omit', 'timeout_policy': 'none',
            'auth_mode': 'bearer', 'store': False, 'stream': False}


def _contract(value):
    result = transport_contract() | deepcopy(value or {})
    if (result['version'] != VERSION or result['max_tokens_mode'] != 'omit' or
            result['max_output_tokens_mode'] != 'omit' or result['timeout_policy'] != 'none'
            or result.get('max_tokens') is not None or result.get('max_output_tokens') is not None):
        raise ValueError('responses_limit_contract_invalid')
    if result['auth_mode'] != 'bearer' or result['store'] is not False or result['stream'] is not False:
        raise ValueError('responses_wire_contract_invalid')
    return result


def responses_endpoint(model):
    url = urlsplit(model.get('base_url', ''))
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError('responses_endpoint_invalid')
    path = url.path.rstrip('/')
    if path.endswith('/v1/responses'):
        pass
    elif path.endswith('/v1'):
        path += '/responses'
    elif not path:
        path = '/v1/responses'
    else:
        raise ValueError('responses_endpoint_incompatible')
    return urlunsplit((url.scheme, url.netloc, path, '', ''))


def build_responses_request(model, messages, contract=None):
    _contract(contract)
    if not isinstance(model.get('name'), str) or not model['name']:
        raise ValueError('model_not_configured')
    items = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'} or
                message['role'] not in ('system', 'developer', 'user', 'assistant') or
                not isinstance(message['content'], str)):
            raise ValueError('responses_message_invalid')
        items.append(deepcopy(message))
    if not items:
        raise ValueError('responses_conversation_invalid')
    payload = {'model': model['name'], 'input': items, 'stream': False, 'store': False}
    if model.get('_dialogue_profile') is not None:
        from persona_runtime.dialogue_voice import read
        payload['temperature'] = read({'model_snapshot': model})['temperature']
    descriptor = transport_contract() | {
        'profile': PROFILE, 'response_format': 'text', 'thinking': 'provider_default',
        'application_validation_required': True, 'logical_input_hash': fingerprint(messages),
        'wire_hash': fingerprint(payload)}
    return payload, descriptor


def responses_usage(value):
    """Keep reported totals independently; cache/reasoning are subdivisions."""
    if not isinstance(value, dict): return None
    raw = {k: value[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')
           if type(value.get(k)) is int and value[k] >= 0}
    if not raw: return None
    result = {}
    for source, target in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                           ('total_tokens', 'total_tokens')):
        if source in raw: result[target] = raw[source]
    for source, target, fields in (
            ('input_tokens_details', 'prompt_tokens_details', ('cached_tokens', 'cache_write_tokens')),
            ('output_tokens_details', 'completion_tokens_details', ('reasoning_tokens',))):
        details = value.get(source)
        if isinstance(details, dict):
            clean = {k: details[k] for k in fields if type(details.get(k)) is int and details[k] >= 0}
            if clean:
                raw[source] = clean
                result[target] = deepcopy(clean)
    result['responses_usage'] = raw
    return result


def response_metadata(data, secret_values=()):
    result = {}
    if not isinstance(data, dict): return result
    for source, target in (('model', 'model'), ('system_fingerprint', 'system_fingerprint'),
                           ('status', 'status'), ('id', 'response_id')):
        value = data.get(source)
        if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', value)
                and not any(secret and secret in value for secret in secret_values)):
            result[target] = value
    return result


def parse_response(data, diagnostics=None, *, secret_values=()):
    diagnostics = deepcopy(diagnostics or {})
    metadata = response_metadata(data, secret_values)
    if 'model' in metadata: diagnostics['response_model'] = metadata['model']
    for name in ('system_fingerprint', 'response_id', 'status'):
        if name in metadata: diagnostics[name if name != 'status' else 'response_status'] = metadata[name]
    usage = responses_usage(data.get('usage')) if isinstance(data, dict) else None

    def fail(code):
        error = ModelError(code, usage=usage, diagnostics=diagnostics)
        error.provider_error = public_provider_error(data, secret_values)
        error.response_metadata = metadata
        error.provider_response_metadata = metadata
        raise error

    if not isinstance(data, dict) or data.get('object') != 'response': fail('provider_invalid_response')
    status = data.get('status')
    if isinstance(status, str) and re.fullmatch(r'[a-z_]{1,64}', status): diagnostics['finish_reason'] = status
    if status == 'incomplete':
        details = data.get('incomplete_details')
        reason = details.get('reason') if isinstance(details, dict) else None
        if reason == 'max_output_tokens':
            diagnostics['finish_reason'] = 'length'
            fail('provider_output_truncated')
        if reason == 'content_filter': fail('provider_content_filtered')
        fail('provider_invalid_response')
    if status == 'failed':
        error = data.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        # Classify an explicit structured code; never infer from error prose.
        failure_codes = {'invalid_api_key': 'provider_http_401',
                         'permission_denied': 'provider_http_403',
                         'model_not_found': 'provider_http_404',
                         'invalid_prompt': 'provider_http_400',
                         'invalid_request_error': 'provider_http_400',
                         'insufficient_quota': 'provider_quota_exhausted',
                         'rate_limit_exceeded': 'provider_http_429',
                         'server_error': 'provider_http_500'}
        fail(failure_codes.get(code, 'provider_aborted'))
    if status == 'cancelled': fail('provider_aborted')
    if status != 'completed' or data.get('error') is not None: fail('provider_invalid_response')
    items = data.get('output')
    if not isinstance(items, list): fail('provider_invalid_response')
    parts, reasoning = [], False
    for item in items:
        if not isinstance(item, dict): fail('provider_invalid_response')
        kind = item.get('type')
        if kind == 'reasoning':
            reasoning = True
            continue
        if kind != 'message': fail('provider_tool_call_not_supported')
        if item.get('role') != 'assistant' or item.get('status', 'completed') != 'completed':
            fail('provider_invalid_response')
        content = item.get('content')
        if not isinstance(content, list): fail('provider_invalid_response')
        for part in content:
            if not isinstance(part, dict): fail('provider_invalid_response')
            if part.get('type') == 'refusal': fail('provider_content_filtered')
            if part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                fail('provider_invalid_response')
            parts.append(part['text'])
    text = ''.join(parts)
    diagnostics.update(output_characters=len(text), reasoning_present=reasoning)
    if not text.strip(): fail('provider_empty_response')
    value = ModelCompletion(text, usage, diagnostics)
    value.response_metadata = metadata
    value.provider_response_metadata = metadata
    return value


class ResponsesClient:
    """One dispatch, no local timeout/output cap/retry or protocol fallback."""
    def __init__(self, contract=None, *, ssl_context=None, session_factory=None, connector_factory=None):
        self.contract = _contract(contract)
        self.ssl_context = ssl_context
        self.session_factory = session_factory or aiohttp.ClientSession
        self.connector_factory = connector_factory or aiohttp.TCPConnector

    def request_payload(self, model, messages):
        return build_responses_request(model, messages, self.contract)[0]

    async def complete(self, model, messages):
        try:
            payload, wire = build_responses_request(model, messages, self.contract)
            url = responses_endpoint(model)
        except ValueError as error:
            raise ModelError(str(error)) from None
        if not model.get('encrypted_key'): raise ModelError('model_not_configured')
        secret = unprotect(model['encrypted_key'])
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret}
        secret_values = (secret, headers['Authorization'])
        del secret
        timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        start = time.monotonic()
        state = {'phase': 'request_setup', 'request_headers_sent': False, 'response_headers_received': False}
        trace = aiohttp.TraceConfig()
        async def connecting(*_): state['phase'] = 'connecting'
        async def connected(*_): state['phase'] = 'awaiting_headers'
        async def sent(*_): state.update(phase='awaiting_headers', request_headers_sent=True)
        trace.on_connection_create_start.append(connecting)
        trace.on_connection_create_end.append(connected)
        trace.on_request_headers_sent.append(sent)

        def diagnostic():
            return {**state, 'timeout_seconds': None, 'local_timeout_enabled': False,
                    'elapsed_ms': round((time.monotonic() - start) * 1000, 3),
                    'transport_profile': wire['profile'], 'response_format': 'text', 'thinking': 'provider_default'}

        connector = self.connector_factory(ssl=self.ssl_context or model_tls_context())
        try:
            async with self.session_factory(timeout=timeout, trust_env=False, trace_configs=[trace], connector=connector) as session:
                async with session.post(url, headers=headers, json=payload, allow_redirects=False) as response:
                    state.update(phase='reading_body', response_headers_received=True)
                    raw = bytearray()
                    try:
                        async for chunk in response.content.iter_chunked(65536): raw.extend(chunk)
                    except aiohttp.ClientError:
                        if response.status == 200: raise
                        raw = None
                    if response.status != 200:
                        data = None
                        try: data = json.loads(raw)
                        except (ValueError, TypeError, UnicodeError): pass
                        error = ModelError(f'provider_http_{response.status}',
                            diagnostics=diagnostic() | {'http_status': response.status})
                        error.provider_error = public_provider_error(data, secret_values)
                        error.response_metadata = response_metadata(data, secret_values)
                        error.provider_response_metadata = error.response_metadata
                        raise error
                    state['phase'] = 'parsing_response'
                    return parse_response(json.loads(raw), diagnostic() | {'http_status': response.status},
                                          secret_values=secret_values)
        except asyncio.CancelledError: raise
        except ModelError: raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ModelError('provider_timeout', diagnostics=diagnostic()) from None
        except aiohttp.ClientError:
            raise ModelError('provider_network_error', diagnostics=diagnostic()) from None
        except (ValueError, TypeError, KeyError):
            raise ModelError('provider_invalid_response', diagnostics=diagnostic()) from None
