"""Bounded generation retries with durable, body-free per-dispatch evidence."""
import asyncio
import hashlib
import json
import re
import time
from .models import RuntimeErrorBase, identifier

MAX_RETRIES = 3


class ConnectionPending(RuntimeErrorBase):
    """The transport proved it sent no headers and received no response."""


def pause_before_send(code,diagnostics,usage):
    if (diagnostics.get('request_headers_sent') is not False or diagnostics.get('response_headers_received')
            or diagnostics.get('effect_unknown') or diagnostics.get('http_status') or usage is not None):return None
    if code in ('storage_write_fault','storage_low_pause_dispatch','local_repair_active'):return 'storage'
    if code in ('provider_network_error','provider_connection_timeout','provider_timeout','system_proxy_required'):return 'network'
    return None

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
    for key in ('phase','transport_profile','response_format','thinking','reasoning_effort','finish_reason','generation_version','timeout_policy','output_limit','network_route','native_terminal','native_protocol','upstream_error_code'):
        item=value.get(key)
        if isinstance(item,str) and re.fullmatch(r'[a-z0-9_-]{1,64}',item):result[key]=item
    for key in ('timeout_seconds','elapsed_ms','output_characters','http_status','stream_events','first_event_ms','first_content_delta_ms'):
        item=value.get(key)
        if type(item) in (int,float) and item>=0:result[key]=item
    for key in ('request_headers_sent','response_headers_received','reasoning_present','stream','stream_complete','effect_unknown','provider_terminal_error'):
        if type(value.get(key)) is bool:result[key]=value[key]
    from .provider_compat import response_metadata
    result.update(response_metadata({'model':value.get('response_model'),'system_fingerprint':value.get('system_fingerprint')},grok=True))
    return result


def retryable(error):
    code = str(error)
    return code in RETRYABLE or code in ('provider_http_408','provider_http_429') or bool(re.fullmatch(r'provider_http_5\d\d',code))


def effect_unconfirmed(error):
    diagnostics=getattr(error,'diagnostics',None) or {}
    if diagnostics.get('effect_unknown') is True:return True
    if diagnostics.get('native_terminal') in ('completed','failed') or diagnostics.get('provider_terminal_error') is True:return False
    if diagnostics.get('request_headers_sent') is not True:return False
    if diagnostics.get('stream') is True and diagnostics.get('stream_complete') is not True:return True
    return str(error) in ('provider_timeout','provider_connection_timeout','provider_network_error',
        'provider_invalid_response','provider_stream_expected','provider_native_unknown')


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
        AND (json_array_length(usage_json,'$.generation_retry.attempts')>0 OR json_type(usage_json,'$.generation_retry.connection_wait')='object')
        THEN (SELECT count(*) FROM json_each(usage_json,'$.generation_retry.attempts') j
              WHERE json_extract(j.value,'$.started_epoch')>?)
        ELSE CASE WHEN started_epoch>? THEN 1 ELSE 0 END END),0) FROM '''+table+' AS counted'+condition+
        ((' AND ' if condition else ' WHERE ')+"coalesce(counted.error_code,'') NOT IN ('organization_network_wait','organization_storage_wait')" if table=='organization_attempts' else ''),(since,since)).fetchone()[0]


async def _one(client, model, messages, timeout_code, *, paths=None, received=None):
    from .maintenance import require_writable_storage
    from .paths import RuntimePaths
    try:
        paths=paths or RuntimePaths.from_environment()
        from .migration import ensure_cloud_ready
        ensure_cloud_ready(paths)
        require_writable_storage(paths)
        from .local_repair import require_open
        require_open(paths)
    except (OSError,RuntimeErrorBase) as error:
        from .chat import ModelError
        raise ModelError(str(error) if isinstance(error,RuntimeErrorBase) else 'storage_unavailable',
            diagnostics={'request_headers_sent':False,'dispatch_phase':'preflight'}) from None
    from .provider_compat import request_timeout
    budget=request_timeout(model)
    request=model|{'_request_deadline':time.monotonic()+budget} if budget is not None else {k:v for k,v in model.items() if k!='_request_deadline'}
    async def receive():
        result=await client.complete(request,messages)
        if received is not None:received(result)
        return result
    task=asyncio.create_task(receive() if received is not None else client.complete(request,messages))
    try:
        done,_=await asyncio.wait({task},timeout=budget)
        if done:return task.result()
        task.cancel('generation_attempt_timeout')
        settled,_=await asyncio.wait({task},timeout=1)
        if not settled:
            error=RuntimeErrorBase('provider_cancellation_unsettled');error.pending_task=task
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            raise error
        try:
            settled_value=task.result()
        except BaseException as child_error:
            from .chat import ModelError
            diagnostics=getattr(child_error,'diagnostics',None) or {}
            diagnostics={**diagnostics,'effect_unknown':diagnostics.get('request_headers_sent') is not False}
            error=ModelError(timeout_code,usage=getattr(child_error,'usage',None),diagnostics=diagnostics)
            error.native=getattr(child_error,'native',None)
            raise error from None
        else:
            # A terminal receipt won the cancellation race; consume it instead
            # of discarding a completed response and paying for it again.
            return settled_value
    except asyncio.CancelledError as cancelled:
        task.cancel('generation_cancelled')
        # Transport close runs before the owning turn releases its execution slot.
        # This is cleanup, not a generation deadline or another dispatch.
        settled,_=await asyncio.wait({task},timeout=1)
        if not settled:
            error=RuntimeErrorBase('provider_cancellation_unsettled');error.pending_task=task
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            raise error
        # Transport cancellation may carry a partial native protocol receipt,
        # or may race with an already received terminal response. Preserve that
        # evidence on the owning cancellation without issuing another request.
        try:
            settled_value=task.result()
        except BaseException as child_error:
            for name in ('usage','diagnostics','native'):
                if hasattr(child_error,name):setattr(cancelled,name,getattr(child_error,name))
        else:
            if isinstance(settled_value,tuple) and len(settled_value)==2:
                cancelled.usage=settled_value[1]
                cancelled.diagnostics=getattr(settled_value,'diagnostics',None)
                cancelled.native=getattr(settled_value,'native',None)
        raise


async def audited_complete(client, model, messages, *, validate, save, before_attempt,
                           prior_failures=0, timeout_code='provider_timeout', sleep=None, paths=None, save_completion=None,
                           save_valid_completion=None, resume_audit=None, meter_first=False, park_offline=False, first_meter=None):
    from .archive import fingerprint
    from .provider_compat import build_request,usage_fields,request_timeout
    from .chat import ModelCompletion
    sleep=sleep or retry_delay
    if type(prior_failures) is not int or not 0<=prior_failures<=MAX_RETRIES:
        raise RuntimeErrorBase('generation_retry_state_invalid')
    state={'version':VERSION,'max_retries':MAX_RETRIES,'prior_failures':prior_failures,'attempts':[]}
    request_hash=fingerprint(build_request(model,messages)[0])
    if resume_audit is not None:
        previous = resume_audit["attempts"]
        offline=resume_audit.get('connection_wait',{})
        if (resume_audit.get("version") != VERSION or not previous and not (park_offline and offline.get('wire')==request_hash)
                or previous and previous[-1]["index"]>=MAX_RETRIES
                or any(row["request_hash"] != request_hash or row["status"] != "failed"
                       for row in previous)):
            raise RuntimeErrorBase("generation_retry_state_invalid")
        state = dict(resume_audit, attempts=list(previous))
        prior_failures = previous[-1]["index"] + 1 if previous else prior_failures
    for index in range(prior_failures,MAX_RETRIES+1):
        from .call_limits import CallLimitPending,wait_for_budget
        from .local_repair import RepairInProgress,wait_until_open
        from .storage import Store
        metered=paths is not None and (meter_first or index!=prior_failures)
        meter=first_meter if index==prior_failures else None
        while True:
            try:
                if paths is not None:await wait_until_open(Store(paths))
                before_attempt(state)
                if metered:
                    from .call_limits import gate
                    store=Store(paths)
                    with store.connection(write=True) as db:meter=gate(db,store,'generation','retry/'+request_hash)
                break
            except CallLimitPending as wait:
                await wait_for_budget(Store(paths),wait)
            except RepairInProgress:
                await wait_until_open(Store(paths))
        row={'id':identifier(),'index':index,'status':'running','started_epoch':time.time(),
             'request_hash':request_hash,'timeout_seconds':request_timeout(model)}
        state['attempts'].append(row);save(aggregate(state))
        value=None
        terminal_seen=False
        receipt_saved=False
        def received(result):
            nonlocal terminal_seen,receipt_saved
            terminal_seen=True
            validation_error=None
            try:
                finish=getattr(result,'diagnostics',{}).get('finish_reason')
                if finish=='length':raise RuntimeErrorBase('provider_output_truncated')
                if finish=='content_filter':raise RuntimeErrorBase('provider_content_filtered')
                validate(result[0])
            except Exception as error:validation_error=error
            row.update(status='completed',finished_epoch=time.time(),usage=usage_fields(result[1],deepseek=True,grok=True))
            row['usage_known']=row['usage'] is not None
            row['diagnostics']=safe_diagnostics(getattr(result,'diagnostics',None))
            row['output_fingerprint']=hashlib.sha256(result[0].encode('utf-8')).hexdigest()
            if validation_error is not None:row['validation']=getattr(validation_error,'output_validation',None) or {'status':'rejected','reason':str(validation_error)}
            if validation_error is not None and save_completion is None:
                terminal_seen=False
                raise validation_error
            callback = save_completion or save_valid_completion
            callback(result,aggregate(state),row,validation_error)
            receipt_saved=True
            if validation_error is not None:raise validation_error
        try:
            callback_enabled = save_completion is not None or save_valid_completion is not None
            value=await _one(client,model,messages,timeout_code,paths=paths,**({'received':received} if callback_enabled else {}))
            finish=getattr(value,'diagnostics',{}).get('finish_reason')
            if finish=='length':raise RuntimeErrorBase('provider_output_truncated')
            if finish=='content_filter':raise RuntimeErrorBase('provider_content_filtered')
            if not callback_enabled:validate(value[0])
        except BaseException as error:
            if callback_enabled and terminal_seen:
                # 外部完整结果已收到。本地持久化、校验或取消失败不成为第二次模型请求。
                if not receipt_saved:
                    try:save(aggregate(state))
                    except Exception:pass
                error.usage=aggregate(state);error.diagnostics=row.get('diagnostics',{})
                error.output_fingerprint=row.get('output_fingerprint')
                raise
            interrupted=isinstance(error,asyncio.CancelledError)
            code='request_interrupted' if interrupted else str(error)
            if not interrupted and effect_unconfirmed(error):
                error.diagnostics={**(getattr(error,'diagnostics',None) or {}),
                    'effect_unknown':True,'upstream_error_code':code}
                code='provider_effect_unknown';error.args=(code,)
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
            pause=pause_before_send(code,row['diagnostics'],row['usage'])
            if park_offline and pause:
                state['attempts'].pop()
                count=state.get('connection_wait',{}).get('count',0)+1
                state['connection_wait']={'count':count,'wire':request_hash,'code':code,'headers_sent':False}
                wait=ConnectionPending('desktop_'+pause+'_wait')
                wait.audit=aggregate(state);wait.meter=meter
                raise wait from None
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
        if not callback_enabled:save(aggregate(state))
        diagnostics=dict(getattr(value,'diagnostics',{}))
        diagnostics.update(retry_count=index,provider_attempt_count=len(state['attempts']),retry_version=VERSION)
        return ModelCompletion(value[0],aggregate(state),diagnostics,native=getattr(value,'native',None))


def assert_manual_retry_safe(conn,turn_id):
    """Keep paid-response recovery protection, independent of retrieval content."""
    for row in conn.execute('SELECT usage_json FROM attempts WHERE turn_id=?',(turn_id,)):
        state=json.loads(row[0] or '{}').get('generation_retry',{})
        for attempt in state.get('attempts',[]):
            if attempt.get('status')=='completed':raise RuntimeErrorBase('generation_already_received')
            code=attempt.get('error_code') or '';diag=attempt.get('diagnostics') or {}
            if (attempt.get('status') in ('running','interrupted') or any(k in code for k in ('unknown','unconfirmed','unsettled'))
                or diag.get('effect_unknown') is True
                or diag.get('request_headers_sent') is True and not diag.get('response_headers_received') and not diag.get('http_status')):
                raise RuntimeErrorBase('generation_effect_unconfirmed')
