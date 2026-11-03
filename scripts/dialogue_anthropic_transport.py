"""Isolated Anthropic Messages transport for the CR3-A.1 recovery experiment.

This is not a production configuration switch. Logical messages remain the
Manifest input; the mapped Messages body has its own wire fingerprint. Ordered
leading system messages become distinct top-level text blocks. Interleaved
system messages are rejected rather than moved. No output limit or local
timeout is supplied, and a gateway rejection is never repaired by adding one.
"""
from copy import deepcopy
import asyncio
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion, ModelError, model_tls_context
from persona_runtime.security import unprotect

VERSION = 'anthropic-messages-uncapped-v1'


def transport_contract():
    return {'version': VERSION, 'max_tokens_mode': 'omit', 'timeout_policy': 'none',
            'auth_mode': 'bearer', 'anthropic_version': '2023-06-01'}


def _contract(value):
    result = transport_contract() | deepcopy(value or {})
    if (result['version'] != VERSION or result['max_tokens_mode'] != 'omit' or
            result['timeout_policy'] != 'none' or result.get('max_tokens') is not None):
        raise ValueError('anthropic_limit_contract_invalid')
    if result['auth_mode'] not in ('bearer', 'x-api-key'):
        raise ValueError('anthropic_auth_contract_invalid')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', result['anthropic_version']):
        raise ValueError('anthropic_version_invalid')
    return result


def messages_endpoint(model):
    url = urlsplit(model.get('base_url', ''))
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError('anthropic_endpoint_invalid')
    path = url.path.rstrip('/')
    if path.endswith('/v1/messages'):
        pass
    elif path.endswith('/v1'):
        path += '/messages'
    elif not path:
        path = '/v1/messages'
    else:
        raise ValueError('anthropic_endpoint_incompatible')
    return urlunsplit((url.scheme, url.netloc, path, '', ''))


def build_messages_request(model, messages, contract=None):
    contract = _contract(contract)
    if not isinstance(model.get('name'), str) or not model['name']:
        raise ValueError('model_not_configured')
    system, conversation = [], []
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get('content'), str)
                or message.get('role') not in ('system', 'user', 'assistant')):
            raise ValueError('anthropic_message_invalid')
        if message['role'] == 'system':
            if conversation:
                raise ValueError('anthropic_interleaved_system')
            system.append({'type': 'text', 'text': message['content']})
        else:
            conversation.append({'role': message['role'], 'content': message['content']})
    if not conversation or conversation[0]['role'] != 'user':
        raise ValueError('anthropic_conversation_invalid')
    payload = {'model': model['name'], 'messages': conversation, 'stream': False}
    if system:
        payload['system'] = system
    if model.get('_dialogue_profile') is not None:
        from persona_runtime.dialogue_voice import read
        payload['temperature'] = read({'model_snapshot': model})['temperature']
    wire = {'version': VERSION, 'profile': 'anthropic-messages-v1',
            'max_tokens_mode': 'omit', 'timeout_policy': 'none',
            'anthropic_version': contract['anthropic_version'],
            'auth_mode': contract['auth_mode'], 'response_format': 'text',
            'thinking': 'provider_default', 'application_validation_required': True,
            'logical_input_hash': fingerprint(messages), 'wire_hash': fingerprint(payload)}
    return payload, wire


def messages_usage(value):
    """Anthropic input excludes cache reads/writes; sum these once, not twice."""
    if not isinstance(value, dict):
        return None
    keys = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
    raw = {key: value[key] for key in keys if type(value.get(key)) is int and value[key] >= 0}
    if 'input_tokens' not in raw or 'output_tokens' not in raw:
        return None
    prompt = raw['input_tokens'] + raw.get('cache_creation_input_tokens', 0) + raw.get('cache_read_input_tokens', 0)
    return {'prompt_tokens': prompt, 'completion_tokens': raw['output_tokens'],
            'total_tokens': prompt + raw['output_tokens'], 'anthropic_usage': raw}


def public_provider_error(data, secret_values=()):
    """A small structured supplier rejection, not its headers or full body."""
    error = data.get('error') if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    result = {}
    for name in ('type', 'code'):
        value = error.get(name)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value):
            result[name] = value
    message = error.get('message')
    if isinstance(message, str):
        for secret in sorted((v for v in secret_values if isinstance(v, str) and v), key=len, reverse=True):
            message = message.replace(secret, '[REDACTED]')
        message = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', message)
        message = re.sub(r'(?i)((?:x-api-key|api_key|authorization)\s*[:=]\s*)[^\s,;"\']+',
                         r'\1[REDACTED]', message)
        result['message'] = message[:512]
        result['message_truncated'] = len(message) > 512
    return result or None


def parse_message(data, diagnostics=None):
    diagnostics = deepcopy(diagnostics or {})
    if not isinstance(data, dict) or data.get('type') != 'message' or data.get('role') != 'assistant':
        raise ModelError('provider_invalid_response', diagnostics=diagnostics)
    usage = messages_usage(data.get('usage'))
    reason = data.get('stop_reason')
    if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,64}', reason):
        diagnostics['finish_reason'] = reason
    for source, target in (('model', 'response_model'), ('system_fingerprint', 'system_fingerprint')):
        value = data.get(source)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', value):
            diagnostics[target] = value
    if reason == 'max_tokens':
        raise ModelError('provider_output_truncated', usage=usage, diagnostics=diagnostics)
    if reason == 'refusal':
        raise ModelError('provider_content_filtered', usage=usage, diagnostics=diagnostics)
    if reason == 'tool_use':
        raise ModelError('provider_tool_call_not_supported', usage=usage, diagnostics=diagnostics)
    if reason not in ('end_turn', 'stop_sequence'):
        raise ModelError('provider_invalid_response', usage=usage, diagnostics=diagnostics)
    blocks = data.get('content')
    if not isinstance(blocks, list):
        raise ModelError('provider_invalid_response', usage=usage, diagnostics=diagnostics)
    parts, thinking = [], False
    for block in blocks:
        if not isinstance(block, dict):
            raise ModelError('provider_invalid_response', usage=usage, diagnostics=diagnostics)
        kind = block.get('type')
        if kind == 'text' and isinstance(block.get('text'), str):
            parts.append(block['text'])
        elif kind in ('thinking', 'redacted_thinking'):
            thinking = True
        elif kind == 'tool_use':
            raise ModelError('provider_tool_call_not_supported', usage=usage, diagnostics=diagnostics)
        else:
            raise ModelError('provider_invalid_response', usage=usage, diagnostics=diagnostics)
    text = ''.join(parts)
    diagnostics.update(output_characters=len(text), reasoning_present=thinking)
    if not text.strip():
        raise ModelError('provider_empty_response', usage=usage, diagnostics=diagnostics)
    return ModelCompletion(text, usage, diagnostics)


class AnthropicMessagesClient:
    """One physical dispatch; the caller owns native retry and durable audit."""
    def __init__(self, contract=None, *, ssl_context=None, session_factory=None, connector_factory=None):
        self.contract = _contract(contract)
        self.ssl_context = ssl_context
        self.session_factory = session_factory or aiohttp.ClientSession
        self.connector_factory = connector_factory or aiohttp.TCPConnector

    def request_payload(self, model, messages):
        return build_messages_request(model, messages, self.contract)[0]

    async def complete(self, model, messages):
        try:
            payload, wire = build_messages_request(model, messages, self.contract)
            url = messages_endpoint(model)
        except ValueError as error:
            raise ModelError(str(error)) from None
        headers = {'Content-Type': 'application/json', 'anthropic-version': self.contract['anthropic_version']}
        if not model.get('encrypted_key'):
            raise ModelError('model_not_configured')
        secret = unprotect(model['encrypted_key'])
        if self.contract['auth_mode'] == 'bearer':
            headers['Authorization'] = 'Bearer ' + secret
        else:
            headers['x-api-key'] = secret
        secret_values = (secret, headers.get('Authorization', ''))
        del secret
        timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        start = time.monotonic()
        transport = {'phase': 'request_setup', 'request_headers_sent': False,
                     'response_headers_received': False}
        trace = aiohttp.TraceConfig()
        async def connecting(*_): transport['phase'] = 'connecting'
        async def connected(*_): transport['phase'] = 'awaiting_headers'
        async def sent(*_):
            transport.update(phase='awaiting_headers', request_headers_sent=True)
        trace.on_connection_create_start.append(connecting)
        trace.on_connection_create_end.append(connected)
        trace.on_request_headers_sent.append(sent)
        def diagnostic():
            return {**transport, 'timeout_seconds': None, 'local_timeout_enabled': False,
                    'elapsed_ms': round((time.monotonic() - start) * 1000, 3),
                    'transport_profile': wire['profile'], 'response_format': 'text',
                    'thinking': 'provider_default'}
        connector = self.connector_factory(ssl=self.ssl_context or model_tls_context())
        try:
            async with self.session_factory(timeout=timeout, trust_env=False,
                                            trace_configs=[trace], connector=connector) as session:
                async with session.post(url, headers=headers, json=payload, allow_redirects=False) as response:
                    transport.update(phase='reading_body', response_headers_received=True)
                    if response.status != 200:
                        provider_error = None
                        try:
                            raw_error = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                raw_error.extend(chunk)
                            provider_error = public_provider_error(json.loads(raw_error), secret_values)
                        except (ValueError, TypeError, UnicodeError, aiohttp.ClientError):
                            pass
                        error = ModelError(f'provider_http_{response.status}', diagnostics=diagnostic() | {'http_status': response.status})
                        error.provider_error = provider_error
                        raise error
                    # No inherited two-MiB cap or token/character truncation.
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                    transport['phase'] = 'parsing_response'
                    return parse_message(json.loads(chunks), diagnostic() | {'http_status': response.status})
        except asyncio.CancelledError:
            # Context managers close the response/session before propagating.
            raise
        except ModelError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ModelError('provider_timeout', diagnostics=diagnostic()) from None
        except aiohttp.ClientError:
            raise ModelError('provider_network_error', diagnostics=diagnostic()) from None
        except (ValueError, TypeError, KeyError):
            raise ModelError('provider_invalid_response', diagnostics=diagnostic()) from None
