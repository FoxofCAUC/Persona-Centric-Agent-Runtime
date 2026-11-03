"""Artificial wire/HTTP fixtures; no supplier connection or secret access."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelError
from scripts import dialogue_anthropic_transport as transport


MODEL = {'base_url': 'https://gateway.invalid/v1', 'name': 'grok-4.7',
         'encrypted_key': 'fake-dpapi', 'timeout': 1, 'max_tokens': 1,
         '_request_deadline': 0}
MESSAGES = [{'role': 'system', 'content': '第一段\n'},
            {'role': 'system', 'content': '\n第二段'},
            {'role': 'user', 'content': '输入\n原样'}]


def response(text='回答'):
    return {'type': 'message', 'role': 'assistant', 'model': 'grok-4.7-build',
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}],
            'usage': {'input_tokens': 10, 'output_tokens': 4,
                      'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 7}}


class WireTests(unittest.TestCase):
    def test_system_blocks_text_order_and_logical_vs_wire_hash(self):
        original = deepcopy(MESSAGES)
        body, wire = transport.build_messages_request(MODEL, MESSAGES)
        self.assertEqual(body['system'], [{'type': 'text', 'text': '第一段\n'},
                                         {'type': 'text', 'text': '\n第二段'}])
        self.assertEqual(body['messages'], [MESSAGES[2]])
        self.assertEqual(MESSAGES, original)
        self.assertEqual(wire['logical_input_hash'], fingerprint(MESSAGES))
        self.assertEqual(wire['wire_hash'], fingerprint(body))
        self.assertNotEqual(wire['logical_input_hash'], wire['wire_hash'])

    def test_legacy_caps_deadline_and_json_flags_not_sent(self):
        body, _ = transport.build_messages_request(MODEL | {'_json_output': True, '_disposition_compile': True}, MESSAGES)
        self.assertEqual(set(body), {'model', 'messages', 'system', 'stream'})
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('timeout', body)
        self.assertNotIn('_request_deadline', body)
        self.assertNotIn('encrypted_key', body)

    def test_explicit_zero_positive_and_unknown_limit_mode_rejected(self):
        for value in ({'max_tokens': 0}, {'max_tokens': 999999}, {'max_tokens_mode': 'explicit'}, {'timeout_policy': '240'}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'limit_contract_invalid'):
                transport.build_messages_request(MODEL, MESSAGES, value)

    def test_endpoint_no_duplicate_v1_and_incompatible_paths_rejected(self):
        for base in ('https://gateway.invalid', 'https://gateway.invalid/v1/', 'https://gateway.invalid/v1/messages'):
            self.assertEqual(transport.messages_endpoint(MODEL | {'base_url': base}), 'https://gateway.invalid/v1/messages')
        for base in ('http://gateway.invalid/v1', 'https://gateway.invalid/v1/chat/completions', 'https://key@gateway.invalid/v1'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                transport.messages_endpoint(MODEL | {'base_url': base})

    def test_interleaved_system_not_silently_moved(self):
        with self.assertRaisesRegex(ValueError, 'interleaved_system'):
            transport.build_messages_request(MODEL, [MESSAGES[2], MESSAGES[0]])

    def test_temperature_preserved_and_consecutive_turns_not_rewritten(self):
        model = MODEL | {'_dialogue_profile': {'version': 'natural-dialogue-v2', 'seed': 'artificial', 'temperature': 1.1}}
        messages = [MESSAGES[2], MESSAGES[2], {'role': 'assistant', 'content': '原答案'}, MESSAGES[2]]
        body, _ = transport.build_messages_request(model, messages)
        self.assertEqual(body['messages'], messages)
        self.assertEqual(body['temperature'], 1.1)

    def test_text_blocks_concatenated_without_reasoning_or_added_separator(self):
        value = response()
        value['content'] = [{'type': 'thinking', 'thinking': 'DO_NOT_EXPORT'},
                            {'type': 'text', 'text': '甲\n'}, {'type': 'text', 'text': '乙'}]
        parsed = transport.parse_message(value)
        self.assertEqual(parsed[0], '甲\n乙')
        self.assertTrue(parsed.diagnostics['reasoning_present'])
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(parsed.diagnostics))

    def test_usage_cache_components_added_once_and_original_counts_retained(self):
        parsed = transport.parse_message(response())
        self.assertEqual(parsed[1]['prompt_tokens'], 20)
        self.assertEqual(parsed[1]['completion_tokens'], 4)
        self.assertEqual(parsed[1]['total_tokens'], 24)
        self.assertEqual(parsed[1]['anthropic_usage']['input_tokens'], 10)
        self.assertIsNone(transport.messages_usage({'output_tokens': 4}))

    def test_truncation_refusal_tool_and_empty_not_success(self):
        for reason, code in (('max_tokens', 'provider_output_truncated'), ('refusal', 'provider_content_filtered'),
                             ('tool_use', 'provider_tool_call_not_supported'), ('pause_turn', 'provider_invalid_response')):
            value = response(); value['stop_reason'] = reason
            with self.subTest(reason=reason), self.assertRaisesRegex(ModelError, code):
                transport.parse_message(value)
        with self.assertRaisesRegex(ModelError, 'empty_response'):
            transport.parse_message(response('  '))

    def test_missing_backend_is_not_fabricated(self):
        parsed = transport.parse_message(response())
        self.assertEqual(parsed.diagnostics['response_model'], 'grok-4.7-build')
        self.assertNotIn('system_fingerprint', parsed.diagnostics)


class FakeContent:
    def __init__(self, body, wait=False):
        self.body = body
        self.wait = wait
        self.entered = asyncio.Event()

    async def iter_chunked(self, size):
        self.entered.set()
        if self.wait:
            await asyncio.Future()
        yield self.body[:7]
        yield self.body[7:]


class FakeResponse:
    def __init__(self, status=200, value=None, wait=False):
        self.status = status
        self.content = FakeContent(json.dumps(value or response(), ensure_ascii=False).encode(), wait)
        self.closed = False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True


class FakeSession:
    def __init__(self, response): self.response, self.closed, self.calls = response, False, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return self.response


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result, contract=None):
        session = FakeSession(result)
        self.settings = []
        def factory(**kwargs): self.settings.append(kwargs); return session
        client = transport.AnthropicMessagesClient(contract, session_factory=factory,
                    connector_factory=lambda **_: object(), ssl_context=object())
        return client, session

    async def test_all_timeouts_disabled_headers_private_and_redirects_disabled(self):
        client, session = self.client(FakeResponse())
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], '回答')
        timeout = self.settings[0]['timeout']
        self.assertTrue(all(getattr(timeout, name) is None for name in ('total', 'connect', 'sock_connect', 'sock_read')))
        self.assertFalse(self.settings[0]['trust_env'])
        self.assertFalse(session.calls[0]['allow_redirects'])
        self.assertEqual(session.calls[0]['headers']['Authorization'], 'Bearer ARTIFICIAL_SECRET')
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(value.diagnostics))
        self.assertTrue(session.closed and session.response.closed)

    async def test_cancellation_closes_session_and_response(self):
        result = FakeResponse(wait=True)
        client, session = self.client(result)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            task = asyncio.create_task(client.complete(MODEL, MESSAGES))
            await result.content.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(session.closed and result.closed)

    async def test_http400_no_retry_or_added_limit_or_fallback(self):
        rejection = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens: Field required'}}
        client, session = self.client(FakeResponse(status=400, value=rejection))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_400') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn('max_tokens', session.calls[0]['json'])
        self.assertEqual(session.calls[0]['url'], 'https://gateway.invalid/v1/messages')
        self.assertEqual(raised.exception.diagnostics['http_status'], 400)
        self.assertEqual(raised.exception.provider_error,
                         {'type': 'invalid_request_error', 'message': 'max_tokens: Field required', 'message_truncated': False})

    async def test_rejection_message_secret_redacted_and_diagnostic_length_only(self):
        rejection = {'error': {'type': 'invalid_request_error', 'code': 'missing_field',
            'message': 'Authorization: Bearer ARTIFICIAL_SECRET; key ARTIFICIAL_SECRET\n' + 'detail' * 200}}
        client, session = self.client(FakeResponse(status=400, value=rejection))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_400') as raised:
                await client.complete(MODEL, MESSAGES)
        public = raised.exception.provider_error
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(public))
        self.assertTrue(public['message_truncated'])
        self.assertLessEqual(len(public['message']), 512)
        self.assertEqual(public['code'], 'missing_field')
        self.assertEqual(len(session.calls), 1)

    async def test_html_rejection_keeps_http_status_without_inventing_cause(self):
        result = FakeResponse(status=524)
        result.content = FakeContent(b'<html>upstream failure</html>')
        client, _ = self.client(result)
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            with self.assertRaisesRegex(ModelError, 'provider_http_524') as raised:
                await client.complete(MODEL, MESSAGES)
        self.assertIsNone(raised.exception.provider_error)

    async def test_no_inherited_two_mib_output_cap(self):
        text = 'x' * (2 * 1024 * 1024 + 1)
        client, _ = self.client(FakeResponse(value=response(text)))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertEqual(value[0], text)

    async def test_x_api_key_header_is_explicit_contract(self):
        client, session = self.client(FakeResponse(), {'auth_mode': 'x-api-key'})
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            await client.complete(MODEL, MESSAGES)
        self.assertEqual(session.calls[0]['headers']['x-api-key'], 'ARTIFICIAL_SECRET')
        self.assertNotIn('Authorization', session.calls[0]['headers'])

    async def test_legacy_contract_still_sends_non_streaming_body(self):
        client, session = self.client(FakeResponse())
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            value = await client.complete(MODEL, MESSAGES)
        self.assertIs(session.calls[0]['json']['stream'], False)
        self.assertNotIn('stream', value.diagnostics)


# --- Streaming (anthropic-messages-stream-v1); artificial SSE only ---------

STREAM = transport.stream_transport_contract()


def events(text_parts=('回', '答'), stop_reason='end_turn', thinking=('SECRET_THOUGHT',), final_output=4):
    value = [{'type': 'message_start', 'message': {
        'id': 'msg_artificial', 'type': 'message', 'role': 'assistant', 'model': 'grok-4.7-build',
        'content': [], 'stop_reason': None, 'stop_sequence': None,
        'usage': {'input_tokens': 10, 'output_tokens': 1,
                  'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 7}}}]
    index = 0
    if thinking:
        value.append({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'thinking', 'thinking': ''}})
        value += [{'type': 'content_block_delta', 'index': index, 'delta': {'type': 'thinking_delta', 'thinking': t}} for t in thinking]
        value.append({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'signature_delta', 'signature': 'sig'}})
        value.append({'type': 'content_block_stop', 'index': index})
        index += 1
    value.append({'type': 'ping'})
    value.append({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}})
    value += [{'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': t}} for t in text_parts]
    value.append({'type': 'content_block_stop', 'index': index})
    value.append({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                  'usage': {'output_tokens': final_output}})
    value.append({'type': 'message_stop'})
    return value


def sse(items):
    return b''.join(b'event: ' + e['type'].encode() + b'\ndata: ' +
                    json.dumps(e, ensure_ascii=False).encode() + b'\n\n' for e in items)


def fold(items):
    stream = transport.MessageStream()
    for event in items:
        stream.push(event)
    return stream


class FakeStreamContent:
    def __init__(self, chunks, hang_at=None):
        self.chunks, self.hang_at = list(chunks), hang_at
        self.entered = asyncio.Event()

    async def iter_chunked(self, size):
        self.entered.set()
        for i, chunk in enumerate(self.chunks):
            if i == self.hang_at:
                await asyncio.Future()
            yield chunk
        if self.hang_at is not None and self.hang_at >= len(self.chunks):
            await asyncio.Future()


class FakeStreamResponse(FakeResponse):
    def __init__(self, chunks, status=200, content_type='text/event-stream; charset=utf-8', hang_at=None):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.content = FakeStreamContent(chunks, hang_at)
        self.closed = False


def split(body, size):
    return [body[i:i + size] for i in range(0, len(body), size)]


class StreamWireTests(unittest.TestCase):
    def test_stream_contract_sends_stream_true_and_records_idle_policy(self):
        body, wire = transport.build_messages_request(MODEL, MESSAGES, STREAM)
        legacy_body, legacy_wire = transport.build_messages_request(MODEL, MESSAGES)
        self.assertIs(body['stream'], True)
        self.assertEqual(set(body), {'model', 'messages', 'system', 'stream'})
        self.assertNotIn('max_tokens', body)
        self.assertEqual({k: v for k, v in body.items() if k != 'stream'},
                         {k: v for k, v in legacy_body.items() if k != 'stream'})
        self.assertEqual(wire['version'], transport.STREAM_VERSION)
        self.assertEqual((wire['stream'], wire['timeout_policy'], wire['idle_timeout_seconds']), (True, 'idle', 90))
        self.assertEqual(wire['logical_input_hash'], legacy_wire['logical_input_hash'])
        self.assertNotEqual(wire['wire_hash'], legacy_wire['wire_hash'])
        self.assertNotIn('stream', legacy_wire)

    def test_stream_contract_rejects_caps_deadlines_and_bad_idle(self):
        for change in ({'max_tokens': 2048}, {'max_tokens_mode': 'explicit'}, {'timeout_policy': 'none'},
                       {'timeout_policy': 'total'}, {'stream': False}, {'idle_timeout_seconds': 5},
                       {'idle_timeout_seconds': 601}, {'idle_timeout_seconds': '90'}, {'idle_timeout_seconds': True}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'limit_contract_invalid'):
                transport.build_messages_request(MODEL, MESSAGES, STREAM | change)
        body, wire = transport.build_messages_request(MODEL, MESSAGES, transport.stream_transport_contract(None))
        self.assertIs(body['stream'], True)
        self.assertIsNone(wire['idle_timeout_seconds'])

    def test_legacy_contract_cannot_be_switched_to_stream_without_new_version(self):
        with self.assertRaisesRegex(ValueError, 'limit_contract_invalid'):
            transport.build_messages_request(MODEL, MESSAGES, {'stream': True})


class SSEDecoderTests(unittest.TestCase):
    def test_every_chunk_boundary_including_split_utf8_and_crlf(self):
        items = events(text_parts=('雨', '后\n', '晴'))
        body = sse(items).replace(b'\n', b'\r\n')
        for size in (1, 2, 3, 7, 64, len(body)):
            decoder, decoded = transport.SSEDecoder(), []
            for chunk in split(body, size):
                decoded += decoder.feed(chunk)
            decoded += decoder.close()
            with self.subTest(size=size):
                self.assertEqual(decoded, items)

    def test_comments_event_fields_and_multiline_data(self):
        decoder = transport.SSEDecoder()
        body = b': keepalive\n\nevent: ping\nid: 7\nretry: 100\ndata: {"type":\ndata: "ping"}\n\n'
        self.assertEqual(decoder.feed(body), [{'type': 'ping'}])

    def test_final_event_without_trailing_blank_line_is_flushed_on_close(self):
        decoder = transport.SSEDecoder()
        self.assertEqual(decoder.feed(b'data: {"type": "message_stop"}'), [])
        self.assertEqual(decoder.close(), [{'type': 'message_stop'}])


class MessageStreamTests(unittest.TestCase):
    def test_folded_stream_parses_identically_to_non_streaming_message(self):
        streamed = transport.parse_message(fold(events()).result())
        whole = transport.parse_message(response())
        self.assertEqual(streamed[0], whole[0])
        self.assertEqual(streamed[1], whole[1])
        self.assertEqual(streamed.diagnostics['response_model'], whole.diagnostics['response_model'])
        self.assertEqual(streamed.diagnostics['finish_reason'], 'end_turn')
        self.assertTrue(streamed.diagnostics['reasoning_present'])

    def test_thinking_text_is_counted_never_retained(self):
        stream = fold(events(thinking=('SECRET_THOUGHT',) * 50))
        self.assertNotIn('SECRET_THOUGHT', json.dumps(stream.result(), ensure_ascii=False))
        self.assertNotIn('SECRET_THOUGHT', json.dumps(stream.diagnostics()))
        self.assertIsNotNone(stream.diagnostics()['first_thinking_ms'])

    def test_final_usage_comes_from_message_delta_not_initial_snapshot(self):
        usage = transport.parse_message(fold(events(final_output=16127)).result())[1]
        self.assertEqual(usage['completion_tokens'], 16127)
        self.assertEqual(usage['anthropic_usage']['input_tokens'], 10)

    def test_same_stop_reason_rules_as_non_streaming(self):
        for reason, code in (('max_tokens', 'provider_output_truncated'), ('refusal', 'provider_content_filtered'),
                             ('pause_turn', 'provider_invalid_response')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ModelError, code):
                transport.parse_message(fold(events(stop_reason=reason)).result())
        with self.assertRaisesRegex(ModelError, 'empty_response'):
            transport.parse_message(fold(events(text_parts=(' ',))).result())

    def test_tool_use_block_rejected_like_non_streaming(self):
        items = events()
        items.insert(-2, {'type': 'content_block_start', 'index': 9, 'content_block': {'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {}}})
        items.insert(-2, {'type': 'content_block_delta', 'index': 9, 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}})
        items.insert(-2, {'type': 'content_block_stop', 'index': 9})
        with self.assertRaisesRegex(ModelError, 'tool_call_not_supported'):
            transport.parse_message(fold(items).result())

    def test_malformed_ordering_rejected(self):
        good = events()
        cases = {'before_start': good[1:], 'duplicate_start': [good[0]] + good,
                 'delta_unopened': good[:1] + [{'type': 'content_block_delta', 'index': 5, 'delta': {'type': 'text_delta', 'text': 'x'}}],
                 'text_delta_into_thinking': good[:2] + [{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'x'}}],
                 'after_stop': good + [{'type': 'ping'}, {'type': 'message_delta', 'delta': {}}]}
        for name, items in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                fold(items).result()
        unclosed = [e for e in good if not (e['type'] == 'content_block_stop' and e['index'] == 1)]
        with self.assertRaisesRegex(ValueError, 'unclosed'):
            fold(unclosed).result()

    def test_incomplete_and_unknown_events(self):
        with self.assertRaises(transport.StreamIncomplete):
            fold(events()[:-1]).result()
        items = events()
        items.insert(3, {'type': 'future_event_type', 'payload': 1})
        self.assertEqual(transport.parse_message(fold(items).result())[0], '回答')


class StreamClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result, contract=STREAM):
        session = FakeSession(result)
        self.settings = []
        def factory(**kwargs): self.settings.append(kwargs); return session
        client = transport.AnthropicMessagesClient(contract, session_factory=factory,
                    connector_factory=lambda **_: object(), ssl_context=object())
        return client, session

    async def complete(self, client):
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            return await client.complete(MODEL, MESSAGES)

    async def test_streams_with_idle_limits_only_and_matches_non_streaming_result(self):
        client, session = self.client(FakeStreamResponse(split(sse(events()), 5)))
        value = await self.complete(client)
        whole = transport.parse_message(response())
        self.assertEqual((value[0], value[1]), (whole[0], whole[1]))
        self.assertIs(session.calls[0]['json']['stream'], True)
        self.assertNotIn('max_tokens', session.calls[0]['json'])
        timeout = self.settings[0]['timeout']
        self.assertIsNone(timeout.total)
        self.assertEqual((timeout.connect, timeout.sock_connect, timeout.sock_read), (90, 90, 90))
        self.assertTrue(value.diagnostics['stream'] and value.diagnostics['local_timeout_enabled'])
        self.assertEqual(value.diagnostics['timeout_seconds'], 90)
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(value.diagnostics))
        self.assertNotIn('SECRET_THOUGHT', json.dumps(value.diagnostics))
        self.assertTrue(session.closed and session.response.closed)

    async def test_long_reasoning_before_late_text_has_no_total_or_output_cap(self):
        items = events(text_parts=('x' * 4096,) * 8, thinking=('思考' * 64,) * 3000, final_output=16000)
        client, _ = self.client(FakeStreamResponse(split(sse(items), 65536)))
        value = await self.complete(client)
        self.assertEqual(len(value[0]), 4096 * 8)
        self.assertEqual(value[1]['completion_tokens'], 16000)

    async def test_disconnect_before_message_stop_is_retryable_network_error_with_unknown_usage(self):
        from persona_runtime.generation_retry import retryable
        body = sse(events()[:-2])
        client, _ = self.client(FakeStreamResponse(split(body, 11)))
        with self.assertRaisesRegex(ModelError, 'provider_network_error') as raised:
            await self.complete(client)
        self.assertIsNone(raised.exception.usage)
        self.assertEqual(raised.exception.diagnostics['phase'], 'streaming')
        self.assertTrue(retryable(raised.exception))

    async def test_in_band_error_event_is_retryable_abort_with_redacted_provider_error(self):
        from persona_runtime.generation_retry import retryable
        items = events()[:3] + [{'type': 'error', 'error': {'type': 'overloaded_error',
                                 'message': 'upstream busy; Bearer ARTIFICIAL_SECRET'}}]
        client, _ = self.client(FakeStreamResponse([sse(items)]))
        with self.assertRaisesRegex(ModelError, 'provider_aborted') as raised:
            await self.complete(client)
        self.assertEqual(raised.exception.provider_error['type'], 'overloaded_error')
        self.assertNotIn('ARTIFICIAL_SECRET', json.dumps(raised.exception.provider_error))
        self.assertEqual(raised.exception.diagnostics['http_status'], 200)
        self.assertTrue(retryable(raised.exception))

    async def test_silence_longer_than_idle_limit_is_retryable_timeout_and_closes(self):
        from persona_runtime.generation_retry import retryable, safe_diagnostics
        result = FakeStreamResponse([sse(events()[:3])], hang_at=1)
        # Artificial: lower the 10 s contract floor to 50 ms for test speed only.
        with patch.object(transport, '_valid_idle', lambda value: True):
            client, session = self.client(result, transport.stream_transport_contract(0.05))
            with self.assertRaisesRegex(ModelError, 'provider_timeout') as raised:
                # Guard: if the idle limit regresses, fail in 5 s instead of hanging the suite.
                try:
                    await asyncio.wait_for(self.complete(client), 5)
                except asyncio.TimeoutError:
                    self.fail('idle timeout not enforced: stream hung past the 50 ms limit')
        self.assertEqual(raised.exception.diagnostics['phase'], 'streaming')
        self.assertEqual(raised.exception.diagnostics['stream_events'], 3)
        self.assertTrue(retryable(raised.exception))
        self.assertEqual(safe_diagnostics(raised.exception.diagnostics)['phase'], 'streaming')
        self.assertTrue(session.closed and result.closed)

    async def test_idle_none_never_times_out_locally(self):
        result = FakeStreamResponse([sse(events())])
        client, _ = self.client(result, transport.stream_transport_contract(None))
        value = await self.complete(client)
        timeout = self.settings[0]['timeout']
        self.assertTrue(all(getattr(timeout, n) is None for n in ('total', 'connect', 'sock_connect', 'sock_read')))
        self.assertFalse(value.diagnostics['local_timeout_enabled'])

    async def test_gateway_ignoring_stream_flag_is_parsed_and_flagged(self):
        body = json.dumps(response(), ensure_ascii=False).encode()
        client, _ = self.client(FakeStreamResponse(split(body, 9), content_type='application/json'))
        value = await self.complete(client)
        self.assertEqual(value[0], '回答')
        self.assertTrue(value.diagnostics['stream_downgraded'])

    async def test_http_error_status_unchanged_in_stream_mode(self):
        result = FakeStreamResponse([b'<html>upstream failure</html>'], status=524, content_type='text/html')
        client, session = self.client(result)
        with self.assertRaisesRegex(ModelError, 'provider_http_524') as raised:
            await self.complete(client)
        self.assertIsNone(raised.exception.provider_error)
        self.assertEqual(len(session.calls), 1)

    async def test_cancellation_mid_stream_closes_session_and_response(self):
        result = FakeStreamResponse([sse(events()[:3])], hang_at=1)
        client, session = self.client(result, transport.stream_transport_contract(None))
        with patch.object(transport, 'unprotect', return_value='ARTIFICIAL_SECRET'):
            task = asyncio.create_task(client.complete(MODEL, MESSAGES))
            await result.content.entered.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(session.closed and result.closed)

    async def test_malformed_sse_json_is_invalid_response(self):
        client, _ = self.client(FakeStreamResponse([b'data: {not json}\n\n']))
        with self.assertRaisesRegex(ModelError, 'provider_invalid_response'):
            await self.complete(client)


if __name__ == '__main__':
    unittest.main()
