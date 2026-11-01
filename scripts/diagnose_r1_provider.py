"""Bounded artificial-only supplier diagnosis. Never resumes the R1 experiment.

One model-list GET and at most five generation requests; each is sent once.
Raw artificial replies stay in the D-drive evidence package, never normal logs.
"""
import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import aiohttp
from persona_runtime.archive import fingerprint
from persona_runtime.chat import model_tls_context
from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import endpoint,usage_fields
from persona_runtime.reply_batch import validate_reply
from persona_runtime.security import unprotect
from scripts.expression_selection_r1 import OUT,HOME,read,sha,timestamp
from scripts.retry_expression_selection_r1 import write_once,ReceiptLedger,StopRun,SUPPLEMENT
from scripts.verify_prompt_assembly_r0 import formal_hashes

DIAG=HOME/'exports/acceptance/M7.6.3-R1-provider-diagnosis'
CLARIFY=('\n本轮输出须是完整JSON对象，不要直接输出聊天正文。前面提到的自然纯文本只约束'
    'messages中每个content的字符串内容，不改变整个响应的JSON外层。'
    '先遵守已给出的schema_version、behavior_intent、messages结构，再把自然接话写进content；对象外不写文字。')


def content_stats(value):
    """Small body-free diagnosis; no arbitrary response strings in this summary."""
    if not isinstance(value,str):return {'kind':'null' if value is None else 'non_string','characters':None}
    kind='empty_string' if not value else 'ascii_spaces_only' if value==(' '*len(value)) else 'whitespace_only' if not value.strip() else 'text'
    result={'kind':kind,'characters':len(value),'non_whitespace_characters':len(''.join(value.split())),
        'sha256_utf8':hashlib.sha256(value.encode()).hexdigest(),'json_parseable':False}
    if kind not in ('text','empty_string'):
        result['whitespace_codepoints']={f'U+{ord(c):04X}':n for c,n in Counter(value).items()}
    try:
        parsed=json.loads(value);result.update(json_parseable=True,json_type=type(parsed).__name__)
    except (ValueError,TypeError):pass
    return result


def plans():
    prior=read(OUT/'experiment/ledger.json')['calls']
    original=next(r for r in prior if r['item']=='R07-detail-control' and r['kind']=='main')
    assert original['status']=='failed' and original['error']=='ModelError:provider_empty_response'
    base=deepcopy(original['request'])
    mini={'model':base['model'],'messages':[{'role':'user','content':'这是人工连接检查。请只回复 PCAR-PROBE-OK。'}],
          'max_tokens':128,'stream':False,'thinking':{'type':'disabled'}}
    json_mini=deepcopy(mini);json_mini['messages']=[{'role':'system','content':'只返回JSON对象，例如 {"ok":true}。不要其他内容。'},
        {'role':'user','content':'连接检查完成，请输出 {"ok":true}。'}];json_mini['response_format']={'type':'json_object'}
    without=deepcopy(base);without.pop('response_format')
    reminder=deepcopy(base);reminder['messages'][0]['content']+=CLARIFY
    return [
        {'id':'D01-plain','purpose':'plain_connection','request':mini,'timeout':60},
        {'id':'D02-json-minimal','purpose':'minimal_json_contract','request':json_mini,'timeout':60},
        {'id':'D03-original-json','purpose':'exact_failed_request','request':base,'timeout':180},
        {'id':'D04-no-json-mode','purpose':'only_remove_response_format','request':without,'timeout':180},
        {'id':'D05-clarify-wrapper','purpose':'only_append_wrapper_clarification','request':reminder,'timeout':180},
    ],original


def prepare():
    if DIAG.exists():raise StopRun('diagnosis_exists_never_overwrite')
    prior_hashes=read(SUPPLEMENT/'source-hashes.json')
    assert all(sha(ROOT/p)==digest for p,digest in prior_hashes.items()),'source_baseline_changed'
    approved=ConfigStore(RuntimePaths(HOME)).load()['model']
    assert approved['base_url'].rstrip('/')=='https://api.deepseek.com' and approved['name']=='deepseek-flash'
    items,original=plans();DIAG.mkdir(parents=True)
    frozen={'items':items,'original_item':original['item'],'original_call':original['ordinal'],
        'expression_snapshot':original['model']['_expression_snapshot'],'base_url':approved['base_url'],
        'model':approved['name'],'generation_limit':5,'models_get_limit':1,'automatic_retries':0,
        'authorized_purpose':'用户要求排查供应商连接和空响应原因；诊断输出不替换任何评测结果。',
        'timestamp':timestamp(),'sources':{str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in list((ROOT/'persona_runtime').rglob('*.py'))+[Path(__file__)]}}
    write_once(DIAG/'plan.json',frozen)
    write_once(DIAG/'baseline.json',{'plan_sha256':sha(DIAG/'plan.json'),'previous_closure_sha256':sha(SUPPLEMENT/'closure.json'),
        'previous_original_ledger_sha256':sha(OUT/'experiment/ledger.json'),
        'previous_stop_sha256':sha(SUPPLEMENT/'real/stop.json'),'formal_hashes':formal_hashes(),
        'previous_source_files_verified':len(prior_hashes)})
    return frozen


def guard():
    seal=read(DIAG/'baseline.json');plan=read(DIAG/'plan.json')
    assert sha(DIAG/'plan.json')==seal['plan_sha256']
    assert sha(SUPPLEMENT/'closure.json')==seal['previous_closure_sha256']
    assert sha(OUT/'experiment/ledger.json')==seal['previous_original_ledger_sha256']
    assert sha(SUPPLEMENT/'real/stop.json')==seal['previous_stop_sha256']
    assert all(sha(ROOT/p)==digest for p,digest in plan['sources'].items())
    assert formal_hashes()==seal['formal_hashes']
    return plan


def tls_probe():
    started=time.monotonic();host='api.deepseek.com';resolved=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    with socket.create_connection((host,443),timeout=10) as conn:
        with model_tls_context().wrap_socket(conn,server_hostname=host) as tls:
            cert=tls.getpeercert()
            return {'dns_addresses':sorted({r[4][0] for r in resolved}), 'dns_tls_seconds':time.monotonic()-started,
                'verified_hostname':host,'tls_version':tls.version(),'cipher':tls.cipher()[0],
                'peer_ip':tls.getpeername()[0],'certificate_sha256':hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest(),
                'certificate_issuer':cert.get('issuer'),'certificate_not_after':cert.get('notAfter'),
                'proxy_environment_keys_present':[k for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY') if os.environ.get(k)],
                'runtime_trust_env':False}


async def http(method,url,key,*,payload=None,timeout=60):
    times={};start=time.monotonic();trace=aiohttp.TraceConfig()
    async def dns_start(*_):times['dns_start_ms']=round((time.monotonic()-start)*1000,3)
    async def dns_end(*_):times['dns_end_ms']=round((time.monotonic()-start)*1000,3)
    async def conn_start(*_):times['connect_start_ms']=round((time.monotonic()-start)*1000,3)
    async def conn_end(*_):times['connect_end_ms']=round((time.monotonic()-start)*1000,3)
    async def sent(*_):times['headers_sent_ms']=round((time.monotonic()-start)*1000,3)
    trace.on_dns_resolvehost_start.append(dns_start);trace.on_dns_resolvehost_end.append(dns_end)
    trace.on_connection_create_start.append(conn_start);trace.on_connection_create_end.append(conn_end);trace.on_request_headers_sent.append(sent)
    headers={'Content-Type':'application/json','Authorization':'Bearer '+unprotect(key)}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=model_tls_context()),
        timeout=aiohttp.ClientTimeout(total=timeout,connect=timeout),trust_env=False,trace_configs=[trace]) as client:
        async with client.request(method,url,headers=headers,json=payload,allow_redirects=False) as response:
            times['response_headers_ms']=round((time.monotonic()-start)*1000,3)
            raw=bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw)>2*1024*1024:raise RuntimeError('response_too_large')
            times['body_complete_ms']=round((time.monotonic()-start)*1000,3)
            safe_headers={k:v for k,v in response.headers.items() if k.lower() in ('content-type','content-length','date','server','x-request-id','x-ds-trace-id') and re.fullmatch(r'[\x20-\x7e]{1,200}',v)}
            return response.status,bytes(raw),{'timings':times,'http_status':response.status,'response_headers':safe_headers,
                'response_bytes':len(raw),'body_sha256':hashlib.sha256(raw).hexdigest()}


def classify(item,body,expression):
    choice=body['choices'][0];message=choice['message'];content=message.get('content');stats=content_stats(content)
    result={'content':stats,'finish_reason':choice.get('finish_reason'),'response_model':body.get('model'),
            'reasoning_present':bool(message.get('reasoning_content')),'application_valid':False}
    if stats['kind']!='text':return False,result
    if item['id']=='D01-plain':result['application_valid']=content.strip()=='PCAR-PROBE-OK'
    elif item['id']=='D02-json-minimal':
        try:result['application_valid']=json.loads(content)=={'ok':True}
        except ValueError:pass
    else:
        try:
            batch=validate_reply(content,'natural_structured',expression)
            result.update(application_valid=True,application_reply=batch.text)
        except Exception as exc:result['application_error']=type(exc).__name__+':'+str(exc)
    # D04 remains a diagnostic failure when non-JSON; never treated as a reply.
    return result['application_valid'],result


async def run():
    plan=guard()
    if (DIAG/'stop.json').exists():raise StopRun('diagnosis_stopped')
    if (DIAG/'receipts').exists():raise StopRun('diagnosis_never_automatically_resume')
    approved=ConfigStore(RuntimePaths(HOME)).load()['model'];key=approved['encrypted_key']
    assert approved['name']==plan['model'] and approved['base_url']==plan['base_url']
    write_once(DIAG/'connection.json',tls_probe())
    write_once(DIAG/'models-get.reserve.json',{'method':'GET','url':endpoint(approved,'models'),'at':timestamp()})
    status,raw,meta=await http('GET',endpoint(approved,'models'),key)
    body=json.loads(raw);ids=[v.get('id') for v in body.get('data',[])]
    write_once(DIAG/'models-get.result.json',{**meta,'model_ids':ids,'configured_model_listed':plan['model'] in ids})
    if status!=200 or plan['model'] not in ids:raise StopRun('model_connection_probe_failed')
    ledger=ReceiptLedger(DIAG/'receipts',limit=5)
    for item in plan['items']:
        guard();ledger.check()
        row=ledger.reserve(item['id'],{'request':item['request'],'kind':'main','item':item['id'],'real':True,
            'input_hash':fingerprint(item['request']['messages']),'purpose':item['purpose'],'timeout':item['timeout']})
        start=time.monotonic()
        try:
            status,raw,meta=await http('POST',endpoint(approved),key,payload=item['request'],timeout=item['timeout'])
            body=json.loads(raw)
        except Exception as exc:
            ledger.finish(row,'failed',error=type(exc).__name__,seconds=time.monotonic()-start)
            raise StopRun('probe_transport_or_parse_failure:'+type(exc).__name__) from exc
        # Never persist an unexpected reasoning transcript. The configured mode
        # is disabled; retain only counts if the supplier violates that mode.
        redacted=False
        for choice in body.get('choices',[]):
            message=choice.get('message',{})
            if message.get('reasoning_content'):
                message['reasoning_content']={'redacted':True,'characters':len(message['reasoning_content'])};redacted=True
        if redacted:write_once(DIAG/'responses'/(item['id']+'.json'),body)
        else:
            path=DIAG/'responses'/(item['id']+'.json');path.parent.mkdir(exist_ok=True)
            with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if status==200:
            ok,analysis=classify(item,body,plan['expression_snapshot'])
        else:ok,analysis=False,{'application_valid':False,'http_error':status}
        ledger.finish(row,'completed' if ok else 'failed',seconds=time.monotonic()-start,
            usage=usage_fields(body.get('usage'),deepseek=True),diagnostics=meta,analysis=analysis,
            raw_artificial_response=str(path if not redacted else DIAG/'responses'/(item['id']+'.json')),
            raw_reasoning_redacted=redacted)
        print(json.dumps({'probe':item['id'],'http':status,'content_kind':analysis.get('content',{}).get('kind'),
            'application_valid':ok},ensure_ascii=False),flush=True)
        ledger.check()
        if status!=200:raise StopRun('probe_http_failure')
    guard();write_once(DIAG/'run-status.json',{'complete':True,'generation_calls':5,'model_list_gets':1,
        'original_results_replaced':0,'formal_files_unchanged':True,'timestamp':timestamp()})


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));a=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:
        if a.action=='prepare':prepare();print('diagnosis_prepared_zero_calls')
        else:asyncio.run(run())
    except (StopRun,Exception) as exc:
        if DIAG.exists() and not (DIAG/'stop.json').exists():write_once(DIAG/'stop.json',{'reason':str(exc),'at':timestamp()})
        print(json.dumps({'stopped':type(exc).__name__+':'+str(exc)}),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
