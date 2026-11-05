"""One private, same-prefix comparison through the real upstream text chain.

This is a local review tool, not a production route or a generic paid probe.
Only an explicit ``run`` can generate. All text stays in the comparison's
temporary directory; upstream runtime data is registered for the same cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
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

from experiments.companion_reference import run_reference, source_status
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ModelCompletion
from persona_runtime.companion import GENERATION_PROFILE, MINIMAL_INSTRUCTION, choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.generation_retry import audited_complete
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import prompt_contract, validate_reply
from persona_runtime.storage import Store

VERSION = 'companion-private-reference-v1'


def error_code(error):
    value = str(error)
    return value if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', value) else type(error).__name__


def split_minimal_request(request):
    """Remove precisely the registered PCAR instruction and format block.

    Material text, including selected character facts/examples and memory,
    remains byte-for-byte in the persona supplied to the reference. This is a
    compiled-material comparison, not an equivalence claim about all prompts.
    """
    messages, model = request.get('messages'), request.get('model')
    if not isinstance(model, dict) or model.get('_companion_profile') != choose('minimal'):
        raise RuntimeErrorBase('reference_minimal_profile_required')
    if model.get('_generation_profile') != GENERATION_PROFILE or model.get('_reply_mode') != 'natural_structured':
        raise RuntimeErrorBase('reference_generation_contract_mismatch')
    if (not isinstance(messages, list) or len(messages) < 2
            or any(type(m) is not dict or set(m) != {'role', 'content'}
                   or not isinstance(m['content'], str) for m in messages)
            or messages[0]['role'] != 'system' or messages[-1]['role'] != 'user'
            or any(m['role'] not in ('user', 'assistant') for m in messages[1:])):
        raise RuntimeErrorBase('reference_source_messages_invalid')
    if request.get('messages_hash') != fingerprint(messages):
        raise RuntimeErrorBase('reference_source_messages_changed')
    wire, _ = build_request(model, messages)
    if request.get('wire_hash') != fingerprint(wire):
        raise RuntimeErrorBase('reference_source_wire_changed')
    expression = model.get('_expression_snapshot')
    protocol = prompt_contract('natural_structured', expression)
    if model.get('_dialogue_profile') is not None:
        from persona_runtime.dialogue_voice import bubble_contract, read
        read({'model_snapshot': model})
        protocol = bubble_contract()
    text = messages[0]['content']
    if not text.startswith(MINIMAL_INSTRUCTION) or not protocol or not text.endswith(protocol):
        raise RuntimeErrorBase('reference_minimal_boundaries_mismatch')
    material = text[len(MINIMAL_INSTRUCTION):-len(protocol)]
    if not material:
        raise RuntimeErrorBase('reference_material_empty')
    return {'persona': '人物资料：' + material, 'material_text': material,
            'history': deepcopy(messages[1:-1]), 'user': messages[-1]['content'],
            'output_contract': protocol, 'shared_dialogue_hash': fingerprint(messages[1:]),
            'material_hash': fingerprint(material)}


def review_display(reference, model):
    """Decode the common output protocol after preserving actual upstream IO.

    The upstream text stages treat JSON as text. Their native segmentation is
    therefore not what this controlled common-format comparison measures.
    """
    captured = reference.get('captured_messages')
    if not isinstance(captured, list) or not captured:
        raise RuntimeErrorBase('reference_captured_output_missing')
    texts = []
    for message in captured:
        if (not isinstance(message, list) or not message
                or any(type(c) is not dict or c.get('type') != 'Plain'
                       or not isinstance(c.get('text'), str) for c in message)):
            raise RuntimeErrorBase('reference_captured_output_not_text')
        texts.append(''.join(c['text'] for c in message))
    captured_text = '\n'.join(texts)
    batch = validate_reply(captured_text, model['_reply_mode'], model['_expression_snapshot'])
    return {'display': batch.text, 'reply': batch.text,
            'bubbles': [segment.text for segment in batch.segments],
            'captured_text': captured_text,
            'display_adapter': 'shared-schema3-decoder-after-upstream',
            'native_bubble_segmentation_not_compared': True}


def restore_model(public, original, scrub):
    """Recover only the approved encrypted credential, never save it."""
    if any(k in public for k in ('api_key', 'encrypted_key', 'admin_token', 'bridge_token')):
        raise RuntimeErrorBase('reference_public_model_contains_credential')
    base = {k: v for k, v in public.items() if not k.startswith('_')}
    if base != scrub(original):
        raise RuntimeErrorBase('reference_public_model_mismatch')
    result = deepcopy(public)
    result['encrypted_key'] = original.get('encrypted_key', '')
    return result


def register_runtime(comparison_root, reference_root):
    """Register the exact owned path before upstream can write private data."""
    name = 'runtime-private-comparison-' + identifier()
    path = safe(reference_root / name, reference_root)
    journal = comparison_root / 'reference-cleanup.json'
    data = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {
        'version': 1, 'paths': [], 'cleanup_after': 'this_short_chat_round_and_necessary_review'}
    data['paths'].append(str(path))
    atomic_json(journal, data)
    return name, path


class FakeClient:
    async def complete(self, model, messages):
        body = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': '独立人工参考夹具', 'content_kind': 'chat', 'cancel_if_new_input': False}
        ]}, ensure_ascii=False)
        return ModelCompletion(body, None, {'finish_reason': 'stop'})


async def run_reference_position(comparison_root, position_id, *, fake=False,
                                 reference_root=None, client=None):
    from persona_runtime.instance import InstanceLock
    lease = InstanceLock(Path(comparison_root).resolve() / 'comparison-execution.lock')
    try:
        return await _run_reference_locked(comparison_root, position_id, fake=fake,
                                            reference_root=reference_root, client=client)
    finally:
        lease.close()


async def _run_reference_locked(comparison_root, position_id, *, fake=False,
                                reference_root=None, client=None):
    from experiments import companion_comparison as comparison
    comparison_root = Path(comparison_root).resolve()
    plan = comparison.load_plan(comparison_root)
    row = comparison.position(plan, position_id)
    comparison.verify_frozen(comparison_root, plan)
    if bool(fake) != bool(plan.get('development_material')):
        raise RuntimeErrorBase('comparison_simulation_scope_mismatch')
    batch_state = json.loads((comparison_root / 'state.json').read_text(encoding='utf-8'))
    if batch_state.get('status') == 'stopped':
        raise RuntimeErrorBase('comparison_batch_stopped')
    comparison.ensure_no_unfinished_position(comparison_root)
    if row.get('mode') != 'reference' or not row.get('paired_pcar_position'):
        raise RuntimeErrorBase('reference_position_required')
    paired_id = row['paired_pcar_position']
    paired_position = comparison.position(plan, paired_id)
    if paired_position.get('mode') != 'minimal' or paired_position.get('pair_id') != row.get('pair_id'):
        raise RuntimeErrorBase('reference_paired_position_mismatch')
    paired = comparison_root / 'positions' / paired_id
    paired_result = json.loads((paired / 'result.json').read_text(encoding='utf-8'))
    kind = 'simulated' if fake else 'real'
    if paired_result.get('status') != 'completed' or paired_result.get('kind') != kind:
        raise RuntimeErrorBase('reference_paired_completion_required')
    request_path = paired / 'request.json'
    request = json.loads(request_path.read_text(encoding='utf-8'))
    common = split_minimal_request(request)
    paths = RuntimePaths(Path(row['home']))
    store, config = Store(paths), ConfigStore(paths)
    original_config = config.load()
    model = restore_model(request['model'], original_config['model'], comparison.public_model)
    guard_cloud_dispatch(store, row['persona_id'], 'main', model)
    reference_root = Path(reference_root or Path(plan['source']['home']).parents[2] / 'reference/dafeiyu').resolve()
    source = source_status(reference_root)
    if not source['source_ready'] or not source['text_patches_applied']:
        raise RuntimeErrorBase('reference_source_not_ready')
    output = comparison_root / 'positions' / position_id
    output.mkdir(parents=True, exist_ok=True)
    private_policy = paths.home / 'runtime/private-copy-policy.json'
    policy_hash = hashlib.sha256(private_policy.read_bytes()).hexdigest()
    source_request_hash = hashlib.sha256(request_path.read_bytes()).hexdigest()
    start = {'version': VERSION, 'position_id': position_id, 'kind': kind,
             'private': True, 'started_at': timestamp(), 'automatic_outer_retry': False}
    with (output / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump(start, stream, ensure_ascii=False, indent=2)
    audit, completion = {}, None
    started = time.perf_counter()
    result = {**start, 'status': 'reserved', 'paired_pcar_position': paired_id,
              'material_hash': common['material_hash'],
              'shared_dialogue_hash': common['shared_dialogue_hash'], 'source': source,
              'actual_qq': False, 'semantic_quality': 'not_evaluated',
              'comparison_scope': 'shared compiled materials; distinct PCAR/AstrBot prompt organization',
              'display_adapter': 'shared-schema3-decoder-after-upstream',
              'native_bubble_segmentation_not_compared': True}

    def before_attempt(state):
        comparison.verify_frozen(comparison_root, plan)
        if config.load() != original_config:
            raise RuntimeErrorBase('reference_comparison_config_changed')
        if hashlib.sha256(private_policy.read_bytes()).hexdigest() != policy_hash:
            raise RuntimeErrorBase('reference_comparison_permission_changed')
        if hashlib.sha256(request_path.read_bytes()).hexdigest() != source_request_hash:
            raise RuntimeErrorBase('reference_paired_request_changed')
        guard_cloud_dispatch(store, row['persona_id'], 'main', model)
        if source_status(reference_root) != source:
            raise RuntimeErrorBase('reference_upstream_source_changed')
        if any(a['status'] == 'completed' for a in state['attempts']):
            raise RuntimeErrorBase('reference_success_replay')

    def save(value):
        nonlocal audit
        audit = deepcopy(value)
        for attempt in audit.get('generation_retry', {}).get('attempts', []):
            reserve = output / (attempt['id'] + '.reserve.json')
            if not reserve.exists():
                if attempt['status'] != 'running':
                    raise RuntimeErrorBase('reference_reservation_missing')
                with reserve.open('x', encoding='utf-8') as stream:
                    json.dump({'kind': kind, 'position_id': position_id, 'attempt': attempt}, stream,
                              ensure_ascii=False, indent=2)
            atomic_json(output / (attempt['id'] + '.attempt.json'), attempt)
        atomic_json(output / 'audit.json', audit)

    async def complete(messages):
        nonlocal completion
        if (not isinstance(messages, list) or len(messages) < 2 or messages[0]['role'] != 'system'
                or messages[1:] != request['messages'][1:]
                or common['persona'] not in messages[0]['content']):
            raise RuntimeErrorBase('reference_shared_materials_changed')
        if sum(len(message['content']) for message in messages) > model['max_input_chars']:
            raise RuntimeErrorBase('reference_context_limit_exceeded')
        wire, transport = build_request(model, messages)
        if fingerprint({k:v for k,v in wire.items() if k != 'messages'}) != fingerprint({
                k:v for k,v in build_request(request['model'], request['messages'])[0].items() if k != 'messages'}):
            raise RuntimeErrorBase('reference_wire_parameters_changed')
        atomic_json(output / 'request.json', {'messages': messages, 'model': comparison.public_model(model),
                    'wire': wire, 'wire_hash': fingerprint(wire), 'messages_hash': fingerprint(messages),
                    'transport': transport, 'material_hash': common['material_hash'],
                    'shared_dialogue_hash': common['shared_dialogue_hash']})
        completion = await audited_complete(client or (FakeClient() if fake else ChatCompletionsClient()),
            model, messages, validate=lambda body: validate_reply(body, model['_reply_mode'], model['_expression_snapshot']),
            save=save, before_attempt=before_attempt)
        atomic_json(output / 'generation-result.json', {'kind': kind, 'raw_text': completion[0],
            'usage': completion[1], 'diagnostics': completion.diagnostics})
        return completion[0]

    try:
        runtime_name, runtime_path = register_runtime(comparison_root, reference_root)
        result['reference_runtime'] = str(runtime_path)
        atomic_json(output / 'result.json', result)
        # Upstream diagnostic output is also a private derivative, not stdout.
        with (output / 'upstream-console.txt').open('w', encoding='utf-8') as console:
            with redirect_stdout(console), redirect_stderr(console):
                value = await run_reference(reference_root, persona=common['persona'], user=common['user'],
                    history=common['history'], output_contract=common['output_contract'], complete=complete,
                    runtime_name=runtime_name)
        atomic_json(output / 'upstream-result.json', value)
        display = review_display(value, model)
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        if len(attempts) == 0 or completion is None:
            raise RuntimeErrorBase('reference_generation_receipt_missing')
        result.update(status='completed', **display, reference=value, usage=audit,
            messages_hash=fingerprint(value['messages']), physical_calls=0 if fake else len(attempts),
            simulated_calls=len(attempts) if fake else 0, generation_attempts=len(attempts),
            diagnostics=completion.diagnostics)
    except BaseException as error:
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        result.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
            error_code=error_code(error), usage=audit, physical_calls=0 if fake else len(attempts),
            simulated_calls=len(attempts) if fake else 0, generation_attempts=len(attempts))
        result['elapsed_seconds'] = time.perf_counter() - started
        atomic_json(comparison_root / 'state.json', {'status': 'stopped', 'position_id': position_id,
            'error_code': result['error_code'], 'automatic_resume': False})
        atomic_json(output / 'result.json', result)
        atomic_json(output / 'terminal.json', {k:result[k] for k in
            ('kind','position_id','status','physical_calls','elapsed_seconds','error_code')})
        raise
    result['elapsed_seconds'] = time.perf_counter() - started
    atomic_json(output / 'result.json', result)
    atomic_json(output / 'terminal.json', {k:result[k] for k in
        ('kind','position_id','status','physical_calls','elapsed_seconds')})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run',))
    parser.add_argument('comparison', type=Path)
    parser.add_argument('position_id')
    parser.add_argument('--reference-root', type=Path,
                        default=Path('D:/Persona-Centric Agent Runtime/reference/dafeiyu'))
    parser.add_argument('--fake', action='store_true')
    args = parser.parse_args()
    async def invoke():
        if args.fake:
            from unittest.mock import patch
            with patch('socket.socket.connect', side_effect=RuntimeError('reference_fake_network_blocked')):
                return await run_reference_position(args.comparison,args.position_id,fake=True,reference_root=args.reference_root)
        return await run_reference_position(args.comparison,args.position_id,reference_root=args.reference_root)
    try:
        value=asyncio.run(invoke())
        print(json.dumps({k:value[k] for k in ('kind','position_id','status','physical_calls','elapsed_seconds')}))
    except BaseException as error:
        print(json.dumps({'status':'stopped','error_code':error_code(error)}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
