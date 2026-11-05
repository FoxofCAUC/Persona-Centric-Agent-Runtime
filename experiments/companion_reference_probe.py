"""One declared artificial case through the isolated real AstrBot text adapter.

Only an explicit invocation without --fake can use the configured provider.
The adapter owns no retry loop: the existing PCAR audited_complete owns all
physical attempts. Credentials stay in process memory and never enter evidence.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_probe import CASES
from experiments.companion_reference import run_reference, source_status
from experiments.companion_trial import load_trial
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.generation_retry import audited_complete
from persona_runtime.models import RuntimeErrorBase, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import snapshot, source_blocked
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import expression_snapshot, prompt_contract, validate_reply
from persona_runtime.storage import Store


def public_model(value):
    return {key: deepcopy(item) for key, item in value.items()
            if key not in {'encrypted_key', 'api_key', 'admin_token', 'bridge_token'}}


def error_code(error):
    value = str(error)
    return value if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', value) else type(error).__name__


def visible_persona(store, persona_id):
    with store.connection() as conn:
        profile = snapshot(conn, persona_id)
        if any(source_blocked(conn, row['source_ids']) for row in profile.values()):
            raise RuntimeErrorBase('reference_probe_source_blocked')
    core = deepcopy(profile['core']['data'])
    core['expression_style'] = profile['expression']['data']['style']
    relation = profile['relationship']['data']['description']
    text = '人物资料：' + json.dumps(core, ensure_ascii=False)
    if relation:
        text += '\n关系说明：' + relation
    return text, fingerprint(profile)


class FakeClient:
    async def complete(self, model, messages):
        # Deliberately only a protocol/data-flow fixture, not answers to CASES.
        body = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': '这是独立人工参考通路夹具', 'content_kind': 'chat', 'cancel_if_new_input': False}
        ]}, ensure_ascii=False)
        return ModelCompletion(body, None, {'finish_reason': 'stop'})


async def run(trial_path, case, output, reference_root, *, fake=False):
    trial = load_trial(trial_path)
    if trial.get('private') is not False:
        raise RuntimeErrorBase('artificial_probe_only')
    arm = next((value for value in trial['arms'].values() if value['mode'] == 'minimal'), None)
    if arm is None:
        raise RuntimeErrorBase('minimal_trial_arm_missing')
    text = dict(CASES)[case]
    paths = RuntimePaths(Path(arm['home']))
    config = ConfigStore(paths)
    store = Store(paths)
    persona, profile_hash = visible_persona(store, arm['persona_id'])
    original_model = config.load()['model']
    model = deepcopy(original_model)
    expression = expression_snapshot('natural_structured', version=3)
    model.update(_generation_profile={'version': 'chat-stream-unlimited-v1'},
                 _reply_mode='natural_structured', _expression_snapshot=expression)
    reference_root = Path(reference_root).resolve()
    source = source_status(reference_root)
    if not source['source_ready'] or not source['text_patches_applied']:
        raise RuntimeErrorBase('reference_source_not_ready')
    output = Path(output).resolve()
    if output == paths.home.resolve() or paths.home.resolve() in output.parents:
        raise RuntimeErrorBase('probe_output_inside_trial_arm')
    output.mkdir(parents=True, exist_ok=True)
    kind = 'simulated' if fake else 'real'
    start = {'version': 'companion-reference-probe-v1', 'kind': kind, 'case': case,
             'started_at': timestamp(), 'automatic_outer_retry': False, 'private': False}
    with (output / 'run.started.json').open('x', encoding='utf-8') as stream:
        json.dump(start, stream, ensure_ascii=False, indent=2)
    row = {**start, 'status': 'reserved', 'model': public_model(model),
           'persona_hash': profile_hash, 'source': source,
           'actual_qq': False, 'semantic_quality': 'not_evaluated'}
    atomic_json(output / 'result.json', row)
    audit = {}
    result = None
    started = time.perf_counter()

    def before_attempt(state):
        if config.load()['model'] != original_model:
            raise RuntimeErrorBase('reference_probe_model_changed')
        if visible_persona(store, arm['persona_id'])[1] != profile_hash:
            raise RuntimeErrorBase('reference_probe_source_changed')
        if state['attempts'] and state['attempts'][-1]['status'] == 'completed':
            raise RuntimeErrorBase('reference_probe_success_replay')

    def save(value):
        nonlocal audit
        audit = deepcopy(value)
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        # audited_complete saves the running row before calling the HTTP client.
        for attempt in attempts:
            reserve = output / (attempt['id'] + '.reserve.json')
            if not reserve.exists():
                if attempt['status'] != 'running':
                    raise RuntimeErrorBase('reference_probe_reservation_missing')
                with reserve.open('x', encoding='utf-8') as stream:
                    json.dump({'kind': kind, 'case': case, 'attempt': attempt}, stream,
                              ensure_ascii=False, indent=2)
            atomic_json(output / (attempt['id'] + '.attempt.json'), attempt)
        atomic_json(output / 'audit.json', audit)

    async def complete(messages):
        nonlocal result
        wire, transport = build_request(model, messages)
        atomic_json(output / 'request.json', {'messages': messages, 'wire': wire,
                    'transport': transport, 'wire_hash': fingerprint(wire),
                    'messages_hash': fingerprint(messages), 'model': public_model(model)})
        result = await audited_complete(FakeClient() if fake else ChatCompletionsClient(),
            model, messages, validate=lambda body: validate_reply(body, 'natural_structured', expression),
            save=save, before_attempt=before_attempt)
        # Preserve success even if an upstream decoration/send stage later fails.
        atomic_json(output / 'generation-result.json', {'kind': kind, 'raw_text': result[0],
            'usage': result[1], 'diagnostics': result.diagnostics,
            'raw_hash': hashlib.sha256(result[0].encode('utf-8')).hexdigest()})
        return result[0]

    try:
        value = await run_reference(reference_root, persona=persona, user=text, history=[],
            output_contract=prompt_contract('natural_structured', expression), complete=complete,
            runtime_name='runtime-probe-' + case + ('-fake' if fake else '-real'))
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        row.update(status='completed', elapsed_seconds=time.perf_counter()-started,
            reference=value, usage=audit, generation_attempts=len(attempts),
            real_model_calls=0 if fake else len(attempts),
            diagnostics=getattr(result, 'diagnostics', {}),
            usage_unknown_attempts=sum(not attempt.get('usage_known', False) for attempt in attempts))
    except BaseException as error:
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        row.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
            error_code=error_code(error), elapsed_seconds=time.perf_counter()-started,
            usage=audit, generation_attempts=len(attempts), real_model_calls=0 if fake else len(attempts),
            usage_unknown_attempts=sum(not attempt.get('usage_known', False) for attempt in attempts))
        atomic_json(output / 'result.json', row)
        atomic_json(output / 'terminal.json', {k:row[k] for k in
            ('kind','case','status','elapsed_seconds','generation_attempts','real_model_calls','error_code')})
        raise
    atomic_json(output / 'result.json', row)
    atomic_json(output / 'terminal.json', {k:row[k] for k in
        ('kind','case','status','elapsed_seconds','generation_attempts','real_model_calls')})
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--case', choices=[value[0] for value in CASES], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, default=Path('D:/Persona-Centric Agent Runtime/reference/dafeiyu'))
    parser.add_argument('--fake', action='store_true')
    args = parser.parse_args()
    async def invoke():
        if args.fake:
            from unittest.mock import patch
            with patch('socket.socket.connect', side_effect=RuntimeError('reference_fake_network_blocked')):
                return await run(args.trial, args.case, args.output, args.reference_root, fake=True)
        return await run(args.trial, args.case, args.output, args.reference_root)
    try:
        row = asyncio.run(invoke())
        print(json.dumps({k:row[k] for k in ('kind','case','status','elapsed_seconds','generation_attempts','real_model_calls')}, ensure_ascii=False))
    except BaseException as error:
        print(json.dumps({'status':'stopped', 'error_code': error_code(error)}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
