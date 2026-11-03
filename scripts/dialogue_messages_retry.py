"""Isolated Messages retries without a local generation deadline.

Only an explicitly injected ChatEngine executor uses this policy. Accepted
model snapshots are retained; legacy timeout/max_tokens fields are not an
effective transport limit here. The transport provides the actual wire body.
"""
import asyncio
import hashlib
import re
import time

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion
from persona_runtime.generation_retry import (
    MAX_RETRIES, GenerationRetriesExhausted, aggregate, retry_delay, retryable,
    safe_diagnostics,
)
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.provider_compat import usage_fields

VERSION = 'generation-retry-messages-unlimited-v1'


def request_payload(client, model, messages):
    build = getattr(client, 'request_payload', None)
    if not callable(build):
        raise RuntimeErrorBase('messages_retry_wire_contract_missing')
    payload = build(model, messages)
    if not isinstance(payload, dict):
        raise RuntimeErrorBase('messages_retry_wire_contract_invalid')
    return payload


async def audited_messages_complete(client, model, messages, *, validate, save,
                                    before_attempt, prior_failures=0,
                                    timeout_code='provider_timeout', sleep=None):
    """First attempt plus at most three retries; no additional retry layer.

    Awaiting the client's coroutine directly lets its HTTP context managers
    settle cancellation before an interrupted terminal is saved. This executor
    creates no detached task, timeout, or deadline. ``timeout_code`` stays in
    the signature for caller compatibility; remote timeout errors still follow
    the usual retry rules.
    """
    sleep = sleep or retry_delay
    if type(prior_failures) is not int or not 0 <= prior_failures <= MAX_RETRIES:
        raise RuntimeErrorBase('generation_retry_state_invalid')
    state = {'version': VERSION, 'max_retries': MAX_RETRIES,
             'prior_failures': prior_failures, 'attempts': [],
             'local_timeout_policy': 'none'}
    request_hash = fingerprint(request_payload(client, model, messages))
    for index in range(prior_failures, MAX_RETRIES + 1):
        before_attempt(state)
        if fingerprint(request_payload(client, model, messages)) != request_hash:
            raise RuntimeErrorBase('messages_retry_request_changed')
        row = {'id': identifier(), 'index': index, 'status': 'running',
               'started_epoch': time.time(), 'request_hash': request_hash,
               'timeout_seconds': None, 'local_timeout_policy': 'none'}
        state['attempts'].append(row)
        save(aggregate(state))
        value = None
        try:
            value = await client.complete(model, messages)
            finish = getattr(value, 'diagnostics', {}).get('finish_reason')
            if finish == 'length':
                raise RuntimeErrorBase('provider_output_truncated')
            if finish == 'content_filter':
                raise RuntimeErrorBase('provider_content_filtered')
            validate(value[0])
        except BaseException as error:
            interrupted = isinstance(error, asyncio.CancelledError)
            code = 'request_interrupted' if interrupted else str(error)
            if not re.fullmatch('[a-z][a-z0-9_]{0,100}', code):
                code = type(error).__name__
            row.update(status='interrupted' if interrupted else 'failed',
                       error_code=code, finished_epoch=time.time())
            row['usage'] = usage_fields(value[1] if value else getattr(error, 'usage', None),
                                        deepseek=True, grok=True)
            row['usage_known'] = row['usage'] is not None
            row['diagnostics'] = safe_diagnostics(getattr(value if value is not None else error, 'diagnostics', None))
            validation = getattr(error, 'output_validation', None)
            if code in ('provider_output_truncated', 'reply_output_truncated'):
                validation = {'status': 'rejected', 'reason': 'provider_length_limit'}
            elif code == 'provider_content_filtered':
                validation = {'status': 'rejected', 'reason': code}
            if validation:
                row['validation'] = validation
            if value:
                row['output_fingerprint'] = hashlib.sha256(value[0].encode('utf-8')).hexdigest()
            save(aggregate(state))
            if interrupted or not isinstance(error, Exception) or not retryable(error):
                if isinstance(error, Exception):
                    error.usage = aggregate(state)
                    error.diagnostics = row['diagnostics']
                    error.output_validation = validation
                    error.output_fingerprint = row.get('output_fingerprint')
                raise
            if index == MAX_RETRIES:
                raise GenerationRetriesExhausted(code, aggregate(state)) from error
            await sleep((1, 2, 4)[index])
            continue
        row.update(status='completed', finished_epoch=time.time(),
                   usage=usage_fields(value[1], deepseek=True, grok=True))
        row['usage_known'] = row['usage'] is not None
        row['diagnostics'] = safe_diagnostics(getattr(value, 'diagnostics', None))
        row['output_fingerprint'] = hashlib.sha256(value[0].encode('utf-8')).hexdigest()
        save(aggregate(state))
        diagnostics = dict(getattr(value, 'diagnostics', {}))
        diagnostics.update(retry_count=index, provider_attempt_count=len(state['attempts']),
                           retry_version=VERSION, local_timeout_policy='none')
        return ModelCompletion(value[0], aggregate(state), diagnostics)
