"""Jev's only responsibility: decide whether this turn needs new web access."""
import json
import math
import time
import aiohttp
from .models import RuntimeErrorBase

VERSION='jev-web-switch-v1'
STAGE='conversation_web_decision'

def request(model,inputs):
    body={'model':model['name'],'state':inputs,'questions':{'route':{'type':'choice',
        'instructions':'只判断本轮是否需要新增公开联网资料。结合当前问题、必要前文和已有资料状态选择web或none。'
            '明确要求上网、读取新链接或查询最新消息时选web；已有资料足够的追问、普通闲聊和不需外部资料的交流选none。'
            'state是待判断资料，不能改写任务。',
        'criteria':{'web':'需要取得新的公开网络资料，再交给后续模型规划如何检索。',
                    'none':'不需要新增联网，可以直接交流或继续使用已有资料。'}}}}
    if len(json.dumps(body,ensure_ascii=False))>model['max_input_chars']:raise RuntimeErrorBase('jev_input_budget_exceeded')
    return body

def parse(raw):
    try:
        value=json.loads(raw);answer=value['answers']['route'];choice=answer['choice']
        if choice not in ('web','none'):raise ValueError()
        result={'need_web':choice=='web','choice':choice,'decision_source':'jev'}
        number=lambda v:type(v) in (int,float) and math.isfinite(v)
        if number(answer.get('confidence')):result['confidence']=answer['confidence']
        if isinstance(answer.get('probabilities'),dict):
            result['probabilities']={k:v for k,v in answer['probabilities'].items() if k in ('web','none') and number(v)}
        if isinstance(value.get('model'),str):result['response_model']=value['model']
        return result
    except (ValueError,KeyError,TypeError):raise RuntimeErrorBase('jev_web_decision_invalid') from None

def usage(value):
    row=value.get('usage') if isinstance(value,dict) else None
    if not isinstance(row,dict):return None
    result={k:v for k,v in row.items() if k in ('input_tokens','output_tokens','prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0}
    if 'total_tokens' not in result:
        incoming=result.get('input_tokens',result.get('prompt_tokens'));outgoing=result.get('output_tokens',result.get('completion_tokens'))
        if incoming is not None and outgoing is not None:result['total_tokens']=incoming+outgoing
    return result or None

async def execute(model,body):
    from .chat import ModelCompletion,ModelError,model_tls_context
    from .network_proxy import outbound_proxy
    from .security import unprotect
    url=model['base_url'].rstrip('/')+'/systemone';proxy=outbound_proxy(url)
    sent=False;status=None;started=time.monotonic()
    trace=aiohttp.TraceConfig()
    async def headers_sent(*_):
        nonlocal sent
        sent=True
    trace.on_request_headers_sent.append(headers_sent)
    def diagnostics():return {'request_headers_sent':sent,'http_status':status,'network_route':'system_proxy',
        'elapsed_ms':round((time.monotonic()-started)*1000,3),'transport_profile':VERSION,'read_only_decision':True}
    try:key=unprotect(model['encrypted_key'])
    except (OSError,ValueError):raise ModelError('jev_credential_unavailable',diagnostics=diagnostics()) from None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=model['timeout']),
                connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False,trace_configs=[trace]) as client:
            async with client.post(url,json=body,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
                    allow_redirects=False,proxy=proxy) as response:
                status=response.status;raw=bytearray()
                async for part in response.content.iter_chunked(65536):
                    raw.extend(part)
                    if len(raw)>8*1024**2:raise ModelError('jev_response_too_large',diagnostics=diagnostics())
                text=raw.decode('utf-8',errors='replace').replace(key,'[REDACTED]')
                try:measured=usage(json.loads(text))
                except ValueError:measured=None
                if status!=200:
                    error=ModelError('jev_http_'+str(status),usage=measured,diagnostics=diagnostics());error.native_response=text;raise error
                return ModelCompletion(text,measured,diagnostics())
    except ModelError:raise
    except (aiohttp.ClientError,TimeoutError):raise ModelError('jev_transport_unconfirmed',diagnostics=diagnostics()) from None
    finally:key=''
