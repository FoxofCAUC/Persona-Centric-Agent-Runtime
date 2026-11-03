"""Isolated Anthropic Messages transport for the CR3-A.1 recovery experiment.

This is not a production configuration switch. Logical messages remain the
Manifest input; the mapped Messages body has its own wire fingerprint. Ordered
leading system messages become distinct top-level text blocks. Interleaved
system messages are rejected rather than moved. No output limit or local
timeout is supplied, and a gateway rejection is never repaired by adding one.

The original ``anthropic-messages-uncapped-v1`` contract is non-streaming and
kept byte-for-byte for sealed packages. ``anthropic-messages-stream-v1`` must
be selected explicitly: it sends ``stream: true`` so the gateway forwards
thinking/text deltas while the model is still reasoning, instead of holding a
silent connection until its ~120 s upstream/Cloudflare limit returns 500/524.
Server-sent events are folded back into the same message object a
non-streaming call returns and validated by the same ``parse_message``; the
only local limit is an optional idle-read timeout (no data for N seconds),
never a total deadline or an output cap.
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
STREAM_VERSION = 'anthropic-messages-stream-v1'
DEFAULT_IDLE_TIMEOUT_SECONDS = 90


def transport_contract():
    return {'version': VERSION, 'max_tokens_mode': 'omit', 'timeout_policy': 'none',
            'auth_mode': 'bearer', 'anthropic_version': '2023-06-01'}


def stream_transport_contract(idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS):
    """Streaming variant; ``idle_timeout_seconds=None`` disables the idle limit."""
    return {'version': STREAM_VERSION, 'max_tokens_mode': 'omit', 'timeout_policy': 'idle',
            'idle_timeout_seconds': idle_timeout_seconds, 'stream': True,
            'auth_mode': 'bearer', 'anthropic_version': '2023-06-01'}


def _valid_idle(value):
    return value is None or (type(value) in (int, float) and 10 <= value <= 600)


def _contract(value):
    value = deepcopy(value or {})
    if value.get('version') == STREAM_VERSION:
        result = stream_transport_contract() | value
        if (result['max_tokens_mode'] != 'omit' or result.get('max_tokens') is not None
                or result['timeout_policy'] != 'idle' or result['stream'] is not True
                or not _valid_idle(result['idle_timeout_seconds'])):
            raise ValueError('anthropic_limit_contract_invalid')
    else:
        result = transport_contract() | value
        if (result['version'] != VERSION or result['max_tokens_mode'] != 'omit' or
                result['timeout_policy'] != 'none' or result.get('max_tokens') is not None
                or result.get('stream') not in (None, False)):
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
    streaming = contract['version'] == STREAM_VERSION
    payload = {'model': model['name'], 'messages': conversation, 'stream': streaming}
    if system:
        payload['system'] = system
    if model.get('_dialogue_profile') is not None:
        from persona_runtime.dialogue_voice import read
        payload['temperature'] = read({'model_snapshot': model})['temperature']
    wire = {'version': contract['version'], 'profile': 'anthropic-messages-v1',
            'max_tokens_mode': 'omit', 'timeout_policy': contract['timeout_policy'],
            'anthropic_version': contract['anthropic_version'],
            'auth_mode': contract['auth_mode'], 'response_format': 'text',
            'thinking': 'provider_default', 'application_validation_required': True,
            'logical_input_hash': fingerprint(messages), 'wire_hash': fingerprint(payload)}
    if streaming:
        wire.update(stream=True, idle_timeout_seconds=contract['idle_timeout_seconds'])
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


class StreamIncomplete(ValueError):
    """The event stream ended before ``message_stop``; final usage is unknown."""


class StreamProviderError(ValueError):
    """The provider sent an in-band ``error`` event after HTTP 200."""
    def __init__(self, event):
        super().__init__('stream_provider_error')
        self.event = event


class SSEDecoder:
    """Minimal text/event-stream framing: ``data:`` lines joined until a blank line.

    ``event:``/``id:``/``retry:`` fields and ``:`` comments are ignored; the JSON
    ``type`` field is authoritative. Chunk boundaries may split lines or UTF-8.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.data = []

    def _dispatch(self):
        if not self.data:
            return None
        event = json.loads(b'\n'.join(self.data).decode('utf-8'))
        self.data = []
        return event

    def feed(self, chunk):
        self.buffer.extend(chunk)
        events = []
        while True:
            end = self.buffer.find(b'\n')
            if end < 0:
                return events
            line = bytes(self.buffer[:end])
            del self.buffer[:end + 1]
            if line.endswith(b'\r'):
                line = line[:-1]
            if not line:
                event = self._dispatch()
                if event is not None:
                    events.append(event)
            elif not line.startswith(b':'):
                field, _, value = line.partition(b':')
                if field == b'data':
                    self.data.append(value[1:] if value.startswith(b' ') else value)

    def close(self):
        events = self.feed(b'\n') if self.buffer else []
        event = self._dispatch()
        return events + ([event] if event is not None else [])


class MessageStream:
    """Fold Messages SSE events into the object a non-streaming call returns.

    Thinking text is counted, never retained. Unknown event types are ignored
    (Anthropic may add new ones); malformed ordering is rejected.
    """
    def __init__(self, clock=time.monotonic):
        self.clock, self.start = clock, clock()
        self.message, self.usage = None, {}
        self.blocks, self.open, self.stopped = {}, set(), False
        self.events = 0
        self.first_event_ms = self.first_thinking_ms = self.first_text_ms = None

    def _ms(self):
        return round((self.clock() - self.start) * 1000, 3)

    def push(self, event):
        if not isinstance(event, dict):
            raise ValueError('stream_event_invalid')
        self.events += 1
        if self.first_event_ms is None:
            self.first_event_ms = self._ms()
        kind = event.get('type')
        if kind == 'ping':
            return
        if kind == 'error':
            raise StreamProviderError(event)
        if self.stopped:
            raise ValueError('stream_event_after_stop')
        if kind == 'message_start':
            message = event.get('message')
            if self.message is not None or not isinstance(message, dict):
                raise ValueError('stream_message_start_invalid')
            self.message = {k: v for k, v in message.items() if k not in ('content', 'usage')}
            if isinstance(message.get('usage'), dict):
                self.usage.update(message['usage'])
            return
        if self.message is None:
            raise ValueError('stream_event_before_start')
        index = event.get('index')
        if kind == 'content_block_start':
            block = event.get('content_block')
            if type(index) is not int or index in self.blocks or not isinstance(block, dict):
                raise ValueError('stream_block_start_invalid')
            block = deepcopy(block)
            if block.get('type') == 'thinking':
                self._thinking(block.pop('thinking', ''))
                block.pop('signature', None)
            elif block.get('type') == 'text':
                self._text(block.get('text'))
            self.blocks[index] = block
            self.open.add(index)
        elif kind == 'content_block_delta':
            delta = event.get('delta')
            if index not in self.open or not isinstance(delta, dict):
                raise ValueError('stream_delta_invalid')
            block, dtype = self.blocks[index], delta.get('type')
            if dtype == 'text_delta' and block.get('type') == 'text' and isinstance(delta.get('text'), str):
                block['text'] = (block.get('text') or '') + delta['text']
                self._text(delta['text'])
            elif dtype == 'thinking_delta' and block.get('type') == 'thinking':
                self._thinking(delta.get('thinking'))
            elif dtype == 'signature_delta' and block.get('type') == 'thinking':
                pass
            elif dtype == 'input_json_delta' and block.get('type') == 'tool_use':
                pass  # parse_message rejects tool_use blocks
            else:
                raise ValueError('stream_delta_invalid')
        elif kind == 'content_block_stop':
            if index not in self.open:
                raise ValueError('stream_block_stop_invalid')
            self.open.discard(index)
        elif kind == 'message_delta':
            delta = event.get('delta')
            if not isinstance(delta, dict):
                raise ValueError('stream_message_delta_invalid')
            for key in ('stop_reason', 'stop_sequence'):
                if key in delta:
                    self.message[key] = delta[key]
            if isinstance(event.get('usage'), dict):
                self.usage.update(event['usage'])
        elif kind == 'message_stop':
            self.stopped = True

    def _text(self, value):
        if isinstance(value, str) and value and self.first_text_ms is None:
            self.first_text_ms = self._ms()

    def _thinking(self, value):
        if isinstance(value, str) and value and self.first_thinking_ms is None:
            self.first_thinking_ms = self._ms()

    def result(self):
        if self.message is None or not self.stopped:
            raise StreamIncomplete('stream_incomplete')
        if self.open:
            raise ValueError('stream_unclosed_block')
        return self.message | {'content': [self.blocks[i] for i in sorted(self.blocks)],
                               'usage': dict(self.usage)}

    def diagnostics(self):
        return {'stream': True, 'stream_events': self.events,
                'first_event_ms': self.first_event_ms, 'first_thinking_ms': self.first_thinking_ms,
                'first_text_ms': self.first_text_ms}


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
        streaming = self.contract['version'] == STREAM_VERSION
        idle = self.contract['idle_timeout_seconds'] if streaming else None
        # Streaming bounds only silence between reads (and connect/headers);
        # there is still no total deadline. The legacy contract has no limit.
        timeout = aiohttp.ClientTimeout(total=None, connect=idle, sock_connect=idle, sock_read=idle)
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
        stream = MessageStream() if streaming else None
        def diagnostic():
            value = {**transport, 'timeout_seconds': idle, 'local_timeout_enabled': idle is not None,
                     'elapsed_ms': round((time.monotonic() - start) * 1000, 3),
                     'transport_profile': wire['profile'], 'response_format': 'text',
                     'thinking': 'provider_default'}
            if stream is not None:
                value.update(stream.diagnostics())
            return value
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
                    if streaming:
                        return await self._read_stream(response, stream, idle, transport, diagnostic, secret_values)
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

    async def _read_stream(self, response, stream, idle, transport, diagnostic, secret_values):
        """Read SSE until ``message_stop``; map stream failures onto existing retryable codes."""
        ctype = (getattr(response, 'headers', None) or {}).get('Content-Type', '') or ''
        if ctype.split(';')[0].strip().lower() == 'application/json':
            # Gateway ignored stream=true and answered in one JSON body.
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                chunks.extend(chunk)
            transport['phase'] = 'parsing_response'
            return parse_message(json.loads(chunks), diagnostic() | {'http_status': 200, 'stream_downgraded': True})
        transport['phase'] = 'streaming'
        decoder = SSEDecoder()
        iterator = response.content.iter_chunked(65536).__aiter__()
        try:
            while not stream.stopped:
                try:
                    if idle is None:
                        chunk = await iterator.__anext__()
                    else:
                        chunk = await asyncio.wait_for(iterator.__anext__(), idle)
                except StopAsyncIteration:
                    for event in decoder.close():
                        stream.push(event)
                    break
                for event in decoder.feed(chunk):
                    stream.push(event)
                    if stream.stopped:
                        break
            data = stream.result()  # raises while phase still reads 'streaming'
            transport['phase'] = 'parsing_response'
        except StreamProviderError as error:
            failure = ModelError('provider_aborted', diagnostics=diagnostic() | {'http_status': 200})
            failure.provider_error = public_provider_error(error.event, secret_values)
            raise failure from None
        except StreamIncomplete:
            raise ModelError('provider_network_error', diagnostics=diagnostic() | {'http_status': 200}) from None
        return parse_message(data, diagnostic() | {'http_status': 200})
