"""Official read-only tool transport. Never receives a separate credential."""
import asyncio
from decimal import Decimal
import ipaddress
import json
import time
from urllib.parse import urlsplit
import aiohttp
from .chat import ModelError,model_tls_context,ModelCompletion
from .security import unprotect


async def request(stage,args,model):
    endpoint='https://api.deepseek.com/v1/tools/'+stage
    from .network_proxy import outbound_proxy
    proxy=outbound_proxy(endpoint)
    sent=False;status=None;body=None;started=time.monotonic()
    if stage=='fetch':
        from .knowledge_general import public_url
        args=dict(args,url=public_url(args['url']))
    # The client connects only to the fixed provider endpoint above. Public
    # hostname resolution and remote-fetch isolation are handled by that service.
    async def headers_sent(*_):
        nonlocal sent
        sent=True
    trace=aiohttp.TraceConfig();trace.on_request_headers_sent.append(headers_sent)
    try:
        timeout=aiohttp.ClientTimeout(total=max(.001,model['timeout']-(time.monotonic()-started)))
        async with aiohttp.ClientSession(timeout=timeout,connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False,trace_configs=[trace]) as client:
            async with client.post(endpoint,json=args,headers={'Authorization':'Bearer '+unprotect(model['encrypted_key']),
                    'Content-Type':'application/json'},allow_redirects=False,proxy=proxy) as response:
                status=response.status
                raw=bytearray()
                async for piece in response.content.iter_chunked(65536):
                    raw.extend(piece)
                    if len(raw)>8*1024**2:raise ModelError('web_response_too_large')
                body=raw.decode('utf-8')
                if status!=200:
                    error=ModelError('web_http_'+str(status),usage={'tool':stage,'http_status':status,'billable':False,'amount_usd':'0','network_route':'system_proxy'},
                        diagnostics={'request_headers_sent':sent,'http_status':status,'read_only_tool':True,'network_route':'system_proxy'})
                    error.tool_response=body
                    raise error
                data=json.loads(body)
                billable=bool(data.get('results')) if stage=='search' else bool(data.get('content'))
                usage={'tool':stage,'http_status':200,'billable':billable,
                    'amount_usd':str(Decimal('0.00075') if billable else Decimal('0')),'network_route':'system_proxy','pricing_source':'https://api-docs.deepseek.com/guides/search/','amount_is_estimate':True,'elapsed_ms':round((time.monotonic()-started)*1000,3)}
                return ModelCompletion(body,usage,{'request_headers_sent':sent,'http_status':200,'read_only_tool':True,'network_route':'system_proxy'})
    except ModelError:raise
    except (aiohttp.ClientError,TimeoutError,ValueError,UnicodeError):
        raise ModelError('web_transport_unconfirmed',diagnostics={'request_headers_sent':sent,'http_status':status,'read_only_tool':True,'network_route':'system_proxy'}) from None
