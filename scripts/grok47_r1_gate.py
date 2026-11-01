"""Six-call Grok compatibility gate. Any failed case blocks R1 continuation."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request,describe_request,endpoint
from persona_runtime.reply_batch import validate_reply
from persona_runtime.character_selector_protocol import parse_output
from scripts.expression_selection_r1 import HOME,OUT,read,sha,safe_model,fingerprint,timestamp
from scripts.retry_expression_selection_r1 import ReceiptLedger,write_once,StopRun
from scripts.verify_prompt_assembly_r0 import formal_hashes
from scripts.diagnose_r1_provider import http

EVIDENCE=HOME/'exports/acceptance/M7.6.3-R1-grok47'
GATE=EVIDENCE/'gate'


def source_hashes():
    return {str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in list((ROOT/'persona_runtime').rglob('*.py'))+[Path(__file__)]}


def model_for(approved,item):
    model=deepcopy(approved['character_selector' if item['kind']=='selector' else 'model'])
    if item['kind']=='selector':model.update(_json_output=True,_character_selection=True)
    else:model.update(item['flags'])
    return model


def guard():
    start=read(EVIDENCE/'start.json');seal=read(GATE/'freeze.json')
    if formal_hashes()!=start['formal_before']:raise StopRun('formal_or_review_files_changed')
    if sha(GATE/'plan.json')!=seal['plan_sha256'] or source_hashes()!=seal['sources']:raise StopRun('grok_gate_freeze_changed')
    if sha(OUT/'experiment/ledger.json')!=start['old_experiment_ledger_sha256']:raise StopRun('original_ledger_changed')
    for path,digest in seal['inputs'].items():
        if sha(Path(path))!=digest:raise StopRun('artificial_input_changed')


def prepare():
    if GATE.exists():raise StopRun('gate_exists_never_overwrite')
    approved=ConfigStore(RuntimePaths(HOME)).load()
    for kind,budget,timeout in (('model',8192,180),('character_selector',512,60)):
        m=approved[kind]
        if m['name']!='grok-4.7' or m['base_url'].rstrip('/')!='https://api.uniapi.io/v1' or m['max_tokens']!=budget or m['timeout']!=timeout:
            raise StopRun('configured_grok_contract_changed')
    if not approved['character_selector']['enabled']:raise StopRun('selector_disabled')
    frozen=read(OUT/'experiment/prepared.json');items=[];inputs={}
    for arm in ('control','experiment'):
        item=next(i for i in frozen['items'] if i['key']=='R07-detail-'+arm)
        items.append({'id':'selector-'+arm,'kind':'selector','messages':item['selector_messages'],
            'pool':read(Path(item['candidate_file'])),'flags':{},'reference_item':item['key']})
        inputs[item['candidate_file']]=sha(Path(item['candidate_file']))
    old=read(OUT/'experiment/ledger.json')['calls'];sim=read(OUT/'simulation/ledger.json')['calls']
    for label,key,collection in [('joke','R03-detail-control',old),('correction','R07-detail-control',old),
                                 ('fact','R05-detail-control',old),('reference','R08-detail-control',sim)]:
        row=next(c for c in collection if c['item']==key and c['kind']=='main')
        items.append({'id':'reply-'+label,'kind':'main','messages':row['request']['messages'],
            'flags':{k:row['model'][k] for k in ('_reply_mode','_expression_snapshot','_dialogue_profile')},'reference_item':key})
    for p in (OUT/'experiment/prepared.json',OUT/'experiment/ledger.json',OUT/'simulation/ledger.json'):
        inputs[str(p)]=sha(p)
    for item in items:
        model=model_for(approved,item);item['model']=safe_model(model);item['wire_request']=build_request(model,item['messages'])[0]
        item['wire_contract']=describe_request(model)
    GATE.mkdir()
    write_once(GATE/'plan.json',{'items':items,'generation_limit':6,'retries':0,'criteria':
        '6/6供应商及原应用合同通过；事实172、后续更正与合法引用人工机器阅读核对；任一失败即阻断，不补跑替换。',
        'provider_provenance':'user_configured_UniAPI_gateway_not_verified_official_xAI','at':timestamp()})
    write_once(GATE/'freeze.json',{'plan_sha256':sha(GATE/'plan.json'),'sources':source_hashes(),'inputs':inputs})
    return approved


async def run():
    if GATE.exists():raise StopRun('gate_no_automatic_resume')
    approved=prepare();guard();ledger=ReceiptLedger(GATE/'receipts',limit=6)
    write_once(GATE/'models.reserve.json',{'url':endpoint(approved['model'],'models'),'at':timestamp()})
    status,raw,meta=await http('GET',endpoint(approved['model'],'models'),approved['model']['encrypted_key'])
    try:ids=[r.get('id') for r in json.loads(raw).get('data',[])]
    except (ValueError,AttributeError):ids=[]
    write_once(GATE/'models.result.json',{'http_status':status,'configured_model_listed':'grok-4.7' in ids,
        'model_count':len(ids),'diagnostics':meta})
    if status in (401,402,403,429) or status>=500:raise StopRun('provider_catalog_http_'+str(status))
    client=ChatCompletionsClient()
    for item in read(GATE/'plan.json')['items']:
        guard();model=model_for(approved,item)
        if safe_model(model)!=item['model'] or build_request(model,item['messages'])[0]!=item['wire_request']:raise StopRun('gate_model_changed')
        row=ledger.reserve(item['id'],{'kind':item['kind'],'item':item['id'],'request':item['wire_request'],
            'input_hash':fingerprint(item['messages']),'real':True,'model':safe_model(model)})
        started=time.monotonic();value=None
        try:
            value=await client.complete(model,item['messages'])
            if item['kind']=='selector':parsed=parse_output(value[0],item['pool'],4)
            else:
                batch=validate_reply(value[0],model['_reply_mode'],model['_expression_snapshot']);parsed={'reply':batch.text}
                if item['id']=='reply-fact' and batch.text!='172':raise ValueError('fact_or_exact_format_failed')
        except Exception as exc:
            ledger.finish(row,'failed',seconds=time.monotonic()-started,error=type(exc).__name__+':'+str(exc),
                output=value[0] if value else None,usage=value[1] if value else getattr(exc,'usage',None),
                diagnostics=getattr(value or exc,'diagnostics',None))
            raise StopRun('gate_case_failed:'+item['id']+':'+type(exc).__name__+':'+str(exc)) from exc
        ledger.finish(row,'completed',seconds=time.monotonic()-started,output=value[0],parsed=parsed,
            usage=value[1],diagnostics=getattr(value,'diagnostics',None))
        print(json.dumps({'case':item['id'],'passed':True,'calls':len(ledger.rows())}),flush=True)
    guard();write_once(GATE/'result.json',{'protocol_passed':True,'content_review_pending':True,'generation_calls':6,'at':timestamp()})


def main():
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'r1-execution.lock')
    try:asyncio.run(run())
    except (StopRun,Exception) as exc:
        rows=ReceiptLedger(GATE/'receipts',limit=6).rows() if (GATE/'receipts').exists() else []
        write_once(EVIDENCE/'blocked.json',{'status':'blocked','reason':str(exc),'calls':len(rows),
            'continue_r1':False,'user_instruction':'测试通过后继续任务，不通过则标记阻断','at':timestamp()})
        print(json.dumps({'blocked':str(exc),'calls':len(rows)},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
