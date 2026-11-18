"""Single approved request, preserving complete HTTP error or success receipts.

Adapted from PCAR expression_transport.ExpressionHTTPClient; provider, proxy,
TLS and credential policies remain shared. No retry or redirect is performed.
"""
import asyncio
import hashlib
import json
import time

import aiohttp

from .chat import model_tls_context
from .network_proxy import outbound_proxy
from .provider_compat import build_request, endpoint
from .provider_policy import require_generation_provider
from .security import unprotect


class CorpusCallFailure(Exception):
    def __init__(self,code,diagnostics):
        super().__init__(code)
        self.diagnostics=diagnostics


class CorpusLearningHTTPClient:
    @staticmethod
    def preflight(model,request,*,wire_builder=build_request,allow_reply_decision_tool=False):
        require_generation_provider(model,allow_reply_decision_tool=allow_reply_decision_tool)
        if wire_builder(model,request['messages'])[0] != request:
            raise ValueError('corpus_learning_wire_changed')
        url=endpoint(model);proxy=outbound_proxy(url);tls=model_tls_context()
        secret=unprotect(model['encrypted_key']) if model.get('encrypted_key') else ''
        if not secret:raise ValueError('corpus_learning_credential_missing')
        return url,proxy,tls,secret

    async def complete(self,model,request,*,save_raw):
        try:url,proxy,tls,secret=self.preflight(model,request)
        except (ValueError,OSError,KeyError):
            raise CorpusCallFailure('corpus_learning_transport_preflight_failed',
                 {'request_headers_sent':False,'response_headers_received':False,'phase':'preflight'}) from None
        headers={'Content-Type':'application/json','Authorization':'Bearer '+secret}
        started=time.monotonic()
        state={'request_headers_sent':False,'response_headers_received':False,'network_route':'system_proxy',
               'physical_dispatch_index':1,'response_bytes':0,'raw_complete':False}
        def diagnostics():return state|{'elapsed_ms':round((time.monotonic()-started)*1000,3)}
        trace=aiohttp.TraceConfig()
        async def sent(*_):state['request_headers_sent']=True
        trace.on_request_headers_sent.append(sent)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=model['timeout']),trust_env=False,
                    trace_configs=[trace],connector=aiohttp.TCPConnector(ssl=tls)) as client:
                async with client.post(url,json=request,headers=headers,proxy=proxy,allow_redirects=False) as response:
                    state.update(response_headers_received=True,http_status=response.status)
                    raw=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk);state['response_bytes']=len(raw)
                        if len(raw)>2*1024*1024:raise CorpusCallFailure('corpus_learning_response_capacity',diagnostics())
                    state.update(raw_complete=True,response_sha256=hashlib.sha256(raw).hexdigest())
                    if secret.encode('utf-8') in raw:
                        raise CorpusCallFailure('corpus_learning_credential_reflection_blocked',diagnostics())
                    save_raw(bytes(raw),http_status=response.status,diagnostics=diagnostics())
                    return diagnostics()
        except CorpusCallFailure:raise
        except asyncio.CancelledError as error:
            error.diagnostics=diagnostics();raise
        except (TimeoutError,aiohttp.ClientError):
            raise CorpusCallFailure('corpus_learning_transport_interrupted',diagnostics()) from None
