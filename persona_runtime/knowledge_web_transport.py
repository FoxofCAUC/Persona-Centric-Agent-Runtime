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
    sent=False;status=None;body=None;started=time.monotonic()
    dns=None
    if stage=='fetch':
        from .knowledge_dns import public_target
        target=urlsplit(args['url'])
        dns=await public_target(target.hostname,target.port or (443 if target.scheme=='https' else 80),model['timeout'])
    async def headers_sent(*_):
        nonlocal sent
        sent=True
    trace=aiohttp.TraceConfig();trace.on_request_headers_sent.append(headers_sent)
    try:
        timeout=aiohttp.ClientTimeout(total=max(.001,model['timeout']-(time.monotonic()-started)))
        async with aiohttp.ClientSession(timeout=timeout,connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False,trace_configs=[trace]) as client:
            async with client.post(endpoint,json=args,headers={'Authorization':'Bearer '+unprotect(model['encrypted_key']),
                    'Content-Type':'application/json'},allow_redirects=False) as response:
                status=response.status
                raw=bytearray()
                async for piece in response.content.iter_chunked(65536):
                    raw.extend(piece)
                    if len(raw)>8*1024**2:raise ModelError('web_response_too_large')
                body=raw.decode('utf-8')
                if status!=200:
                    error=ModelError('web_http_'+str(status),usage={'tool':stage,'http_status':status,'billable':False,'amount_usd':'0'},
                        diagnostics={'request_headers_sent':sent,'http_status':status,'read_only_tool':True})
                    error.tool_response=body
                    raise error
                data=json.loads(body)
                billable=bool(data.get('results')) if stage=='search' else bool(data.get('content'))
                usage={'tool':stage,'http_status':200,'billable':billable,
                    'amount_usd':str(Decimal('0.00075') if billable else Decimal('0')),'pricing_source':'https://api-docs.deepseek.com/guides/search/','amount_is_estimate':True,'elapsed_ms':round((time.monotonic()-started)*1000,3)}
                if dns:usage['target_dns']=dns
                return ModelCompletion(body,usage,{'request_headers_sent':sent,'http_status':200,'read_only_tool':True})
    except ModelError:raise
    except (aiohttp.ClientError,TimeoutError,ValueError,UnicodeError):
        raise ModelError('web_transport_unconfirmed',diagnostics={'request_headers_sent':sent,'http_status':status,'read_only_tool':True}) from None


def parse(stage,response):
    try:data=json.loads(response)
    except (ValueError,TypeError):raise ModelError('web_response_invalid') from None
    if not isinstance(data,dict):raise ModelError('web_response_invalid')
    if stage=='fetch':
        if not isinstance(data.get('title'),str) or not isinstance(data.get('content'),str) or not data['content'].strip():
            raise ModelError('web_no_page_content')
        return {'title':data['title'],'content':data['content']}
    if not isinstance(data.get('results'),list) or len(data['results'])>50:raise ModelError('web_response_invalid')
    for row in data['results']:
        if not isinstance(row,dict) or any(not isinstance(row.get(k),str) for k in ('title','url','summary','snippet','content')):
            raise ModelError('web_response_invalid')
        from datetime import datetime,timezone
        date=row.get('date')
        try:
            parsed=datetime.fromisoformat(date.replace('Z','+00:00')) if isinstance(date,str) else None
            row['date']=parsed.astimezone(timezone.utc).isoformat() if parsed and parsed.tzinfo else None
        except ValueError:row['date']=None
    return data
