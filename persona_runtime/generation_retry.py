"""Bounded generation retries with durable, body-free per-dispatch evidence."""
import asyncio
import hashlib
import json
import re
import time
from .models import RuntimeErrorBase, identifier

MAX_RETRIES = 3
VERSION = 'generation-retry-v1'
RETRYABLE = frozenset(('provider_timeout','provider_connection_timeout','provider_network_error',
    'provider_empty_response','provider_invalid_response','provider_output_truncated',
    'provider_aborted','provider_insufficient_system_resource','selector_timeout',
    'selector_invalid_output','reply_schema_invalid','reply_output_truncated'))


async def retry_delay(seconds):
    await asyncio.sleep(seconds)


class GenerationRetriesExhausted(RuntimeErrorBase):
    def __init__(self, last_error, usage):
        super().__init__('provider_retries_exhausted')
        self.usage = usage
        row=usage['generation_retry']['attempts'][-1]
        self.diagnostics = {**row.get('diagnostics',{}),'last_error_code':last_error,'retry_limit':MAX_RETRIES,'retry_exhausted':True}
        self.output_validation=row.get('validation')
        self.output_fingerprint=row.get('output_fingerprint')


def safe_diagnostics(value):
    if not isinstance(value,dict):return {}
    result={}
    for key in ('phase','transport_profile','response_format','thinking','reasoning_effort','finish_reason','generation_version','timeout_policy','output_limit'):
        item=value.get(key)
        if isinstance(item,str) and re.fullmatch(r'[a-z0-9_-]{1,64}',item):result[key]=item
    for key in ('timeout_seconds','elapsed_ms','output_characters','http_status','stream_events','first_event_ms','first_content_delta_ms'):
        item=value.get(key)
        if type(item) in (int,float) and item>=0:result[key]=item
    for key in ('request_headers_sent','response_headers_received','reasoning_present','stream','stream_complete'):
        if type(value.get(key)) is bool:result[key]=value[key]
    from .provider_compat import response_metadata
    result.update(response_metadata({'model':value.get('response_model'),'system_fingerprint':value.get('system_fingerprint')},grok=True))
    return result


def retryable(error):
    code = str(error)
    return code in RETRYABLE or code in ('provider_http_408','provider_http_429') or bool(re.fullmatch(r'provider_http_5\d\d',code))


def aggregate(state):
    result = {}
    for row in state['attempts']:
        for key,value in (row.get('usage') or {}).items():
            if type(value) is int:result[key] = result.get(key,0) + value
            elif isinstance(value,dict):
                target=result.setdefault(key,{})
                for name,number in value.items():target[name]=target.get(name,0)+number
    result['generation_retry'] = state
    return result


def automatic_organization(task):
    return bool(task['automatic']) and json.loads(task['payload_json']).get('manual_quota_request') is not True


def call_count(conn, table, since, *, automatic_only=False):
    allowed=('attempts','character_selection_attempts','organization_attempts',
             'disposition_compile_attempts','situation_observation_attempts')
    if table not in allowed:raise ValueError('invalid_attempt_table')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():return 0
    condition=''
    if automatic_only:
        if table in ('attempts','disposition_compile_attempts'):return 0
        if table=='organization_attempts':condition=" WHERE EXISTS (SELECT 1 FROM organization_tasks t WHERE t.id=counted.task_id AND t.automatic!=0 AND coalesce(json_extract(t.payload_json,'$.manual_quota_request'),0)!=1)"
        elif table=='character_selection_attempts':condition=' WHERE counted.turn_id IS NOT NULL'
    # Count each dispatch at its own time, including retries across an hour/day
    # boundary. Old rows without retry evidence continue to count as one.
    return conn.execute('''SELECT coalesce(sum(CASE WHEN json_valid(usage_json)
        AND json_type(usage_json,'$.generation_retry.attempts')='array'
        AND json_array_length(usage_json,'$.generation_retry.attempts')>0
        THEN (SELECT count(*) FROM json_each(usage_json,'$.generation_retry.attempts') j
              WHERE json_extract(j.value,'$.started_epoch')>?)
        ELSE CASE WHEN started_epoch>? THEN 1 ELSE 0 END END),0) FROM '''+table+' AS counted'+condition,(since,since)).fetchone()[0]


async def _one(client, model, messages, timeout_code):
    from .maintenance import require_writable_storage
    from .paths import RuntimePaths
    try:require_writable_storage(RuntimePaths.from_environment())
    except (OSError,RuntimeErrorBase) as error:
        from .chat import ModelError
        raise ModelError(str(error) if isinstance(error,RuntimeErrorBase) else 'storage_unavailable',
            diagnostics={'request_headers_sent':False,'dispatch_phase':'preflight'}) from None
    from .provider_compat import request_timeout
    budget=request_timeout(model)
    request=model|{'_request_deadline':time.monotonic()+budget} if budget is not None else {k:v for k,v in model.items() if k!='_request_deadline'}
    task=asyncio.create_task(client.complete(request,messages))
    try:
        done,_=await asyncio.wait({task},timeout=budget)
        if done:return task.result()
        task.cancel('generation_attempt_timeout')
        settled,_=await asyncio.wait({task},timeout=1)
        if not settled:
            error=RuntimeErrorBase('provider_cancellation_unsettled');error.pending_task=task
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            raise error
        if not task.cancelled():task.exception()
        raise RuntimeErrorBase(timeout_code)
    except asyncio.CancelledError:
        task.cancel('generation_cancelled')
        # Transport close runs before the owning turn releases its execution slot.
        # This is cleanup, not a generation deadline or another dispatch.
        settled,_=await asyncio.wait({task},timeout=1)
        if not settled:
            error=RuntimeErrorBase('provider_cancellation_unsettled');error.pending_task=task
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            raise error
        if not task.cancelled():task.exception()
        raise


async def audited_complete(client, model, messages, *, validate, save, before_attempt,
                           prior_failures=0, timeout_code='provider_timeout', sleep=None):
    from .archive import fingerprint
    from .provider_compat import build_request,usage_fields,request_timeout
    from .chat import ModelCompletion
    sleep=sleep or retry_delay
    if type(prior_failures) is not int or not 0<=prior_failures<=MAX_RETRIES:
        raise RuntimeErrorBase('generation_retry_state_invalid')
    state={'version':VERSION,'max_retries':MAX_RETRIES,'prior_failures':prior_failures,'attempts':[]}
    request_hash=fingerprint(build_request(model,messages)[0])
    for index in range(prior_failures,MAX_RETRIES+1):
        before_attempt(state)
        row={'id':identifier(),'index':index,'status':'running','started_epoch':time.time(),
             'request_hash':request_hash,'timeout_seconds':request_timeout(model)}
        state['attempts'].append(row);save(aggregate(state))
        value=None
        try:
            value=await _one(client,model,messages,timeout_code)
            finish=getattr(value,'diagnostics',{}).get('finish_reason')
            if finish=='length':raise RuntimeErrorBase('provider_output_truncated')
            if finish=='content_filter':raise RuntimeErrorBase('provider_content_filtered')
            validate(value[0])
        except BaseException as error:
            interrupted=isinstance(error,asyncio.CancelledError)
            code='request_interrupted' if interrupted else str(error)
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code):code=type(error).__name__
            row.update(status='interrupted' if interrupted else 'failed',error_code=code,finished_epoch=time.time())
            row['usage']=usage_fields(value[1] if value else getattr(error,'usage',None),deepseek=True,grok=True)
            row['usage_known']=row['usage'] is not None
            row['diagnostics']=safe_diagnostics(getattr(value if value is not None else error,'diagnostics',None))
            validation=getattr(error,'output_validation',None)
            if code in ('provider_output_truncated','reply_output_truncated'):
                validation={'status':'rejected','reason':'provider_length_limit'}
            elif code=='provider_content_filtered':validation={'status':'rejected','reason':code}
            if validation:row['validation']=validation
            if value:row['output_fingerprint']=hashlib.sha256(value[0].encode('utf-8')).hexdigest()
            save(aggregate(state))
            if interrupted or not isinstance(error,Exception) or not retryable(error):
                if isinstance(error,Exception):
                    error.usage=aggregate(state);error.diagnostics=row['diagnostics']
                    error.output_validation=validation;error.output_fingerprint=row.get('output_fingerprint')
                raise
            if index==MAX_RETRIES:raise GenerationRetriesExhausted(code,aggregate(state)) from error
            await sleep((1,2,4)[index])
            continue
        row.update(status='completed',finished_epoch=time.time(),usage=usage_fields(value[1],deepseek=True,grok=True))
        row['usage_known']=row['usage'] is not None
        row['diagnostics']=safe_diagnostics(getattr(value,'diagnostics',None))
        row['output_fingerprint']=hashlib.sha256(value[0].encode('utf-8')).hexdigest()
        save(aggregate(state))
        diagnostics=dict(getattr(value,'diagnostics',{}))
        diagnostics.update(retry_count=index,provider_attempt_count=len(state['attempts']),retry_version=VERSION)
        return ModelCompletion(value[0],aggregate(state),diagnostics)
