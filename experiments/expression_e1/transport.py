"""单次表达请求，沿用PCAR提供商／代理／TLS合同并保存完整首次返回。"""
import asyncio
import hashlib
import json
import time

import aiohttp

from persona_runtime.chat import model_tls_context
from persona_runtime.network_proxy import outbound_proxy
from persona_runtime.provider_compat import build_request, endpoint, response_metadata, usage_fields
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.security import unprotect


class ExpressionCallFailure(Exception):
    def __init__(self, code, diagnostics, *, usage=None):
        super().__init__(code)
        self.diagnostics = diagnostics
        self.usage = usage


class ExpressionHTTPClient:
    def __init__(self, *, allow_loopback_fixture=False):
        self.allow_loopback_fixture = allow_loopback_fixture

    async def complete(self, model, messages, *, save_raw):
        require_generation_provider(model, allow_loopback_fixture=self.allow_loopback_fixture)
        payload, contract = build_request(model, messages)
        url = endpoint(model)
        proxy = outbound_proxy(url)
        secret = unprotect(model['encrypted_key']) if model['encrypted_key'] else ''
        headers = {'Content-Type': 'application/json'}
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        started = time.monotonic()
        state = {'request_headers_sent': False, 'response_headers_received': False,
                 'network_route': 'system_proxy' if proxy else 'local',
                 'thinking': contract['thinking'], 'response_format': contract['response_format'],
                 'physical_dispatch_index': 1, 'response_bytes': 0}

        def diagnostics():
            return state | {'elapsed_ms': round((time.monotonic() - started) * 1000, 3)}

        trace = aiohttp.TraceConfig()

        async def sent(*_):
            state['request_headers_sent'] = True

        trace.on_request_headers_sent.append(sent)
        timeout = aiohttp.ClientTimeout(total=model['timeout'])
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False, trace_configs=[trace],
                                            connector=aiohttp.TCPConnector(ssl=model_tls_context())) as client:
                async with client.post(url, json=payload, headers=headers, proxy=proxy,
                                       allow_redirects=False) as response:
                    state.update(response_headers_received=True, http_status=response.status)
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        state['response_bytes'] = len(raw)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ExpressionCallFailure('response_capacity_exceeded', diagnostics())
                    state['response_sha256'] = hashlib.sha256(raw).hexdigest()
                    if response.status != 200:
                        raise ExpressionCallFailure(f'provider_http_{response.status}', diagnostics())
                    if secret and secret.encode('utf-8') in raw:
                        raise ExpressionCallFailure('credential_reflection_blocked', diagnostics())
                    save_raw(bytes(raw))
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise ExpressionCallFailure('provider_response_invalid', diagnostics()) from None
                    usage = usage_fields(data.get('usage') if isinstance(data, dict) else None, deepseek=True)
                    if not isinstance(data, dict) or data.get('error') is not None:
                        raise ExpressionCallFailure('provider_terminal_error', diagnostics(), usage=usage)
                    try:
                        choice = data['choices'][0]
                        text = choice['message']['content']
                        finish = choice.get('finish_reason')
                    except (KeyError, IndexError, TypeError):
                        raise ExpressionCallFailure('provider_response_invalid', diagnostics(), usage=usage) from None
                    state.update(response_metadata(data), finish_reason=finish if isinstance(finish, str) else 'unknown',
                                 tool_calls_present=bool(choice['message'].get('tool_calls')))
                    return {'content': text, 'usage': usage, 'diagnostics': diagnostics(),
                            'response_sha256': state['response_sha256']}
        except ExpressionCallFailure:
            raise
        except asyncio.CancelledError as error:
            error.diagnostics = diagnostics()
            raise
        except (TimeoutError, aiohttp.ClientError):
            raise ExpressionCallFailure('provider_transport_interrupted', diagnostics()) from None
