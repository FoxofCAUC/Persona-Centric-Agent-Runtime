"""Native SystemOne transport with protected route credentials and raw receipts."""
import asyncio
import json
import time
import aiohttp
from .chat import ModelCompletion,ModelError,model_tls_context
from .knowledge_routing_contract import usage
from .security import unprotect

async def execute(model,body):
    sent=False;status=None;started=time.monotonic()
    trace=aiohttp.TraceConfig()
    async def headers_sent(*_):
        nonlocal sent
        sent=True
    trace.on_request_headers_sent.append(headers_sent)
    def diagnostics():return {'request_headers_sent':sent,'http_status':status,'read_only_decision':True,
        'elapsed_ms':round((time.monotonic()-started)*1000,3),'transport_profile':'jev-systemone-v1'}
    key=unprotect(model['encrypted_key'])
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=model['timeout']),connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False,trace_configs=[trace]) as client:
            async with client.post(model['base_url'].rstrip('/')+'/systemone',json=body,
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},allow_redirects=False) as response:
                status=response.status;raw=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw)>8*1024**2:raise ModelError('jev_response_too_large',diagnostics=diagnostics())
                text=raw.decode('utf-8',errors='replace').replace(key,'[REDACTED]')
                if status!=200:
                    error=ModelError('jev_http_'+str(status),diagnostics=diagnostics());error.native_response=text;raise error
                try:measured=usage(json.loads(text))
                except (ValueError,TypeError,AttributeError):measured=None
                return ModelCompletion(text,measured,diagnostics())
    except (aiohttp.ClientError,TimeoutError):raise ModelError('jev_transport_unconfirmed',diagnostics=diagnostics()) from None
    finally:key=''
