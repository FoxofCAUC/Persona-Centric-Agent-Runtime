"""Measure and reuse the HTTPS connection for approved scene decisions."""
import asyncio
from types import SimpleNamespace

import aiohttp

from .chat import ModelError, model_tls_context
from .network_proxy import outbound_proxy
from .security import unprotect

TIMING_PROFILE = 'jev-request-body-to-response-v1'


class JevTransport:
    def __init__(self, model):
        self.model = model
        self.url = model['base_url'].rstrip('/') + '/systemone'
        self.session = None

    async def close(self):
        if self.session is not None:
            await self.session.close()
        self.session = None

    @staticmethod
    def trace():
        trace = aiohttp.TraceConfig()

        async def connect_begin(_session, ctx, _params):
            ctx.trace_request_ctx.connection_start = asyncio.get_running_loop().time()

        async def connect_end(_session, ctx, _params):
            ctx.trace_request_ctx.connection_end = asyncio.get_running_loop().time()

        async def reuse(_session, ctx, _params):
            ctx.trace_request_ctx.reused = True

        async def body_sent(_session, ctx, _params):
            record = ctx.trace_request_ctx
            if record.dispatched is None:
                record.dispatched = asyncio.get_running_loop().time()
                record.on_dispatch(record.dispatched)

        async def headers_sent(_session, ctx, _params):
            ctx.trace_request_ctx.headers_sent = True

        trace.on_connection_create_start.append(connect_begin)
        trace.on_connection_create_end.append(connect_end)
        trace.on_connection_reuseconn.append(reuse)
        trace.on_request_headers_sent.append(headers_sent)
        trace.on_request_chunk_sent.append(body_sent)
        return trace

    async def complete(self, wire, *, on_dispatch, on_receipt, diagnostics):
        loop = asyncio.get_running_loop()
        start = loop.time()
        proxy = outbound_proxy(self.url)
        trace = SimpleNamespace(connection_start=None, connection_end=None, reused=False,
                                dispatched=None, on_dispatch=on_dispatch, headers_sent=False)
        headers_at = receipt_at = status = None
        response_headers = {}
        try:
            try:
                key = unprotect(self.model['encrypted_key'])
            except (OSError, ValueError):
                raise ModelError('jev_credential_unavailable') from None
            if self.session is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=min(self.model['timeout'],30)),
                    connector=aiohttp.TCPConnector(ssl=model_tls_context(),keepalive_timeout=25),trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),trace_configs=[self.trace()])
            async with self.session.post(self.url,json=wire,
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
                    allow_redirects=False,proxy=proxy,trace_request_ctx=trace) as response:
                headers_at = loop.time()
                status = response.status
                response_headers = {k:response.headers[k] for k in ('Server-Timing','X-Request-Id')
                                    if k in response.headers}
                raw = bytearray()
                async for part in response.content.iter_chunked(65536):
                    raw.extend(part)
                    if len(raw)>8*1024**2:
                        raise ModelError('jev_response_too_large')
                receipt_at = loop.time()
                on_receipt(receipt_at)
                return status,raw.decode('utf-8',errors='replace').replace(key,'[REDACTED]')
        except (TimeoutError,aiohttp.ClientError):
            sent = trace.headers_sent or trace.dispatched is not None or status is not None
            raise ModelError('jev_transport_unconfirmed' if sent else 'provider_network_error',
                             diagnostics={'request_headers_sent':sent,'http_status':status,
                                          'response_headers_received':status is not None,'effect_unknown':sent}) from None
        finally:
            diagnostics({
                'profile':TIMING_PROFILE,'http':status,'total_ms':ms(start,receipt_at or loop.time()),
                'before_send_ms':ms(start,trace.dispatched),
                'connection_ms':ms(trace.connection_start,trace.connection_end),
                'connection_reused':trace.reused,'time_to_headers_ms':ms(trace.dispatched,headers_at),
                'body_read_ms':ms(headers_at,receipt_at),'wire_ms':ms(trace.dispatched,receipt_at),
                'response_headers':response_headers,
            })


def ms(start, end):
    return round((end-start)*1000,3) if start is not None and end is not None else None
