"""One native POST, with final protocol receipts and no implicit replay."""
import asyncio
import json
import time

import aiohttp

from .deepseek_native_contract import official_endpoint
from .deepseek_native_response import NativeResponse
from .network_proxy import outbound_proxy
from .provider_compat import build_request, request_timeout
from .security import unprotect


class DeepSeekNativeClient:
    def __init__(self, *, ssl_context=None, progress=None):
        self.ssl_context = ssl_context
        self.progress = progress

    async def complete(self, model, messages):
        from .chat import ModelError, model_tls_context
        try:
            payload, contract = build_request(model, messages)
            url = official_endpoint(model)
        except ValueError as error:
            raise ModelError(str(error), diagnostics={'request_headers_sent': False}) from None
        proxy = outbound_proxy(url)
        try: key = unprotect(model['encrypted_key']) if model.get('encrypted_key') else ''
        except (ValueError, OSError):
            raise ModelError('provider_native_credential_unavailable', diagnostics={'request_headers_sent': False}) from None
        headers = {'Content-Type': 'application/json'}
        if key: headers['Authorization'] = 'Bearer ' + key
        started = time.monotonic()
        parser = NativeResponse(stream=payload['stream'], started=started, progress=self.progress)
        transport = {'phase': 'request_setup', 'request_headers_sent': False,
            'response_headers_received': False, 'network_route': 'system_proxy' if proxy else 'local',
            'transport_profile': contract['profile'], 'thinking': contract['thinking'],
            'response_format': 'text'}
        trace = aiohttp.TraceConfig()
        async def sent(*_):
            transport.update(request_headers_sent=True, phase='awaiting_headers')
        trace.on_request_headers_sent.append(sent)
        def diagnostic():
            return {**transport, 'elapsed_ms': round((time.monotonic()-started)*1000, 3)}
        try:
            budget = request_timeout(model)
            async with aiohttp.ClientSession(trust_env=False, trace_configs=[trace],
                    timeout=aiohttp.ClientTimeout(total=budget, connect=budget, sock_read=budget),
                    connector=aiohttp.TCPConnector(ssl=self.ssl_context or model_tls_context())) as client:
                async with client.post(url, json=payload, headers=headers, proxy=proxy, allow_redirects=False) as response:
                    transport.update(response_headers_received=True, http_status=response.status, phase='reading_body')
                    if response.status != 200:
                        # Do not log arbitrary provider error text or retry on a
                        # successful HTTP handshake. A returned status is known.
                        raw = await response.content.read(2*1024*1024)
                        try:
                            value = json.loads(raw)
                            from .provider_compat import usage_fields
                            parser.last_usage = usage_fields(value.get('usage'), deepseek=True)
                        except (ValueError, TypeError, AttributeError): pass
                        parser.fail('provider_native_http_' + str(response.status), known=True)
                    received = 0
                    if payload['stream']:
                        if response.content_type != 'text/event-stream':
                            parser.fail('provider_native_stream_expected')
                        async for chunk in response.content.iter_chunked(65536):
                            received += len(chunk)
                            if received > 16*1024*1024: parser.fail('provider_native_response_too_large')
                            parser.feed(chunk)
                            if parser.done: break
                    else:
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 16*1024*1024: parser.fail('provider_native_response_too_large')
                        try: value = json.loads(raw)
                        except (ValueError, UnicodeError): parser.fail('provider_native_invalid_response')
                        parser.consume(value)
                    value = parser.complete()
                    value.diagnostics = {**diagnostic(), **value.diagnostics}
                    return value
        except ModelError as error:
            error.diagnostics = {**diagnostic(), **(error.diagnostics or {})}
            raise
        except (aiohttp.ClientError, TimeoutError):
            if not transport['request_headers_sent'] and not transport['response_headers_received']:
                raise ModelError('provider_network_error',diagnostics=diagnostic()) from None
            try: parser.fail('provider_native_unknown')
            except ModelError as error:
                error.diagnostics = {**diagnostic(), **error.diagnostics}
                raise error from None
        except asyncio.CancelledError as error:
            error.usage = None
            error.native = parser.packet('unknown')
            error.diagnostics = {**diagnostic(), **parser.diagnostics('unknown')}
            raise
        finally:
            key = ''; headers.clear()
