"""One isolated native-text reference turn; production never imports this tool.

The UI launches this module in the prepared reference Python environment once
per user message. All bodies remain in the disposable trial directory. Only
the existing audited generation helper owns physical retries.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout, suppress
from copy import deepcopy
from datetime import datetime, timezone
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
from persona_runtime.character import FIELDS, head as character_head
from persona_runtime.chat import ChatCompletionsClient, ModelCompletion, ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.generation_retry import audited_complete
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, safe
from persona_runtime.profile import snapshot, source_blocked
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store

VERSION = 'native-reference-trial-v1'
TURN_VERSION = 'native-reference-turn-v1'
REASONING_PROFILE = {'version': 'native-text-low-v1'}
SOURCE_FILES = (Path(__file__), ROOT / 'experiments/companion_reference.py',
    ROOT / 'experiments/companion_native_trial.py',
    ROOT / 'persona_runtime/chat.py', ROOT / 'persona_runtime/chat_stream.py',
    ROOT / 'persona_runtime/provider_compat.py', ROOT / 'persona_runtime/generation_retry.py')
RECOVERY_VERSION = 'native-interval-postprocess-recovery-v1'
# Only the first native-window incident is eligible for this source migration.
RECOVERY_OLD_HASHES = {
    'experiments/companion_reference.py': 'd409fd07a91e1be6247fe8c35f2da3aae60792165f47f2b4c5080ae9990b6439',
    'experiments/companion_native_reference.py': '238f4de19064735b56bafe672299e73747f44710b13a583e081daddd4845d423',
    'experiments/companion_native_trial.py': '4c11e157bb50d1df030ef646ed4f904310014fa409153fe4134886e065444e19',
}
RECOVERY_HELPER_HASH = '2caeee229cd19a1e55507d0191d9386a412b8f625e220633e4d50db10d9003af'


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def recovery_source_changes(old, current):
    if ({k: v for k, v in old.items() if k != 'source_hashes'}
            != {k: v for k, v in current.items() if k != 'source_hashes'}
            or set(old['source_hashes']) != set(current['source_hashes'])):
        raise RuntimeErrorBase('native_recovery_non_source_conditions_changed')
    changes = {key: {'before': value, 'after': current['source_hashes'][key]}
               for key, value in old['source_hashes'].items() if value != current['source_hashes'][key]}
    if (set(changes) != set(RECOVERY_OLD_HASHES)
            or any(row['before'] != RECOVERY_OLD_HASHES[key] for key, row in changes.items())
            or changes['experiments/companion_reference.py']['after'] != RECOVERY_HELPER_HASH):
        raise RuntimeErrorBase('native_recovery_source_migration_not_allowed')
    return changes


def recovery_originals(folder):
    """Require one durably completed generation followed by this known failure."""
    if (folder / 'cancel.json').exists():
        raise RuntimeErrorBase('native_recovery_cancelled')
    value, end = read_json(folder / 'result.json'), read_json(folder / 'terminal.json')
    audit, generation = read_json(folder / 'audit.json'), read_json(folder / 'generation-result.json')
    request, source_input = read_json(folder / 'request.json'), read_json(folder / 'input.json')
    attempts = audit.get('generation_retry', {}).get('attempts', [])
    started = read_json(folder / 'started.json')
    if (value.get('status') != 'failed' or end.get('status') != 'failed'
            or value.get('error_code') != 'IndexError' or end.get('error_code') != 'IndexError'
            or len(attempts) != 1 or attempts[0].get('status') != 'completed'
            or value.get('generation_attempts') != 1 or value.get('usage') != audit
            or generation.get('usage') != audit or generation.get('kind') != value.get('kind')
            or value.get('kind') not in ('real', 'simulated') or value.get('turn_id') != folder.name
            or any(started.get(k) != value.get(k) for k in ('kind', 'turn_id'))
            or any(end.get(k) != value.get(k) for k in ('kind', 'turn_id', 'physical_calls', 'simulated_calls'))
            or (value.get('physical_calls'), value.get('simulated_calls'))
               != ((1, 0) if value.get('kind') == 'real' else (0, 1))):
        raise RuntimeErrorBase('native_recovery_generation_not_unique_complete')
    attempt = attempts[0]
    raw = generation.get('raw_text')
    if (not isinstance(raw, str) or not raw.strip()
            or hashlib.sha256(raw.encode('utf-8')).hexdigest() != attempt.get('output_fingerprint')
            or fingerprint(request['messages']) != request.get('messages_hash')
            or fingerprint(request['wire']) != request.get('wire_hash')
            or request['wire'].get('messages') != request['messages']
            or attempt.get('request_hash') != request['wire_hash']
            or request['messages'][1:] != source_input['history'] + [{'role': 'user', 'content': source_input['text']}]):
        raise RuntimeErrorBase('native_recovery_original_binding_mismatch')
    key = attempt['id']
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key):
        raise RuntimeErrorBase('native_recovery_attempt_id_invalid')
    reserve = read_json(folder / (key + '.reserve.json'))
    initial = reserve.get('attempt', {})
    if (read_json(folder / (key + '.attempt.json')) != attempt
            or reserve.get('kind') != value['kind'] or reserve.get('turn_id') != value['turn_id']
            or initial.get('status') != 'running'
            or any(initial.get(k) != attempt.get(k) for k in ('id', 'index', 'request_hash', 'started_epoch', 'timeout_seconds'))
            or len(list(folder.glob('*.reserve.json'))) != 1 or len(list(folder.glob('*.attempt.json'))) != 1):
        raise RuntimeErrorBase('native_recovery_attempt_receipt_mismatch')
    names = ['input.json', 'request.json', 'generation-result.json', 'result.json', 'terminal.json',
             'audit.json', 'started.json', key + '.reserve.json', key + '.attempt.json']
    if (folder / 'runner-exit.json').exists(): names.append('runner-exit.json')
    return {name: file_hash(folder / name) for name in names}, value, request, generation


def effective_result(folder):
    """Both history consumers use the same checked, separately saved recovery."""
    value, end = read_json(folder / 'result.json'), read_json(folder / 'terminal.json')
    if value.get('status') != end.get('status'):
        raise RuntimeErrorBase('native_trial_terminal_mismatch')
    recovery = folder / 'postprocess-recovery'
    if not recovery.exists(): return value
    if not (recovery / 'terminal.json').is_file() or not (recovery / 'result.json').is_file():
        raise RuntimeErrorBase('native_recovery_incomplete_no_resume')
    record, terminal = read_json(recovery / 'result.json'), read_json(recovery / 'terminal.json')
    if (record.get('version') != RECOVERY_VERSION or record.get('status') != 'completed'
            or terminal.get('status') != 'completed'
            or terminal.get('result_hash') != file_hash(recovery / 'result.json')
            or terminal.get('new_generation_calls') != 0):
        raise RuntimeErrorBase('native_recovery_receipt_invalid')
    root = Path(record['trial_root'])
    plan = verify_trial(root)
    if safe(data_root(plan) / 'turns' / folder.name, data_root(plan)) != folder.resolve():
        raise RuntimeErrorBase('native_recovery_scope_mismatch')
    bindings, original, _, _ = recovery_originals(folder)
    if record.get('bindings') != bindings or record.get('trial_hash') != file_hash(root / 'trial.json'):
        raise RuntimeErrorBase('native_recovery_original_changed')
    result = record['result']
    if (result.get('status') != 'completed' or any(result.get(k) != original.get(k)
            for k in ('physical_calls', 'simulated_calls', 'usage', 'generation_attempts', 'kind', 'turn_id'))):
        raise RuntimeErrorBase('native_recovery_generation_accounting_changed')
    return result


def error_code(error):
    text = str(error)
    return text if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', text) else type(error).__name__


def public_model(model):
    return {k: deepcopy(v) for k, v in model.items()
            if k not in ('encrypted_key', 'api_key', 'admin_token', 'bridge_token')}


def visible_persona(store, persona_id):
    """Read approved profile/character objects directly, never a PCAR prompt."""
    with store.connection() as db:
        if store.current(db) != persona_id:
            raise RuntimeErrorBase('native_reference_persona_changed')
        profile = snapshot(db, persona_id)
        if any(source_blocked(db, row['source_ids']) for row in profile.values()):
            raise RuntimeErrorBase('native_reference_source_blocked')
        character = character_head(db, persona_id)
    core = deepcopy(profile['core']['data'])
    core['expression_style'] = profile['expression']['data']['style']
    data = character['data']
    lore = {row['id']: row for row in data['lorebook']}

    def eligible(key, trail=frozenset()):
        row = lore.get(key)
        return bool(row and key not in trail and row['enabled']
            and row['visibility'] == 'conversation'
            and all(eligible(dep, trail | {key}) for dep in row['dependencies']))

    included_lore = [deepcopy(row) for row in data['lorebook'] if eligible(row['id'])]
    included_examples = [deepcopy(row) for row in data['examples'] if row['enabled']]
    facts = {key: deepcopy(data[key]) for key in FIELDS}
    material = {'persona': core,
        'relationship': profile['relationship']['data']['description'],
        'character_facts': facts, 'lorebook': included_lore, 'expression_examples': included_examples}
    # Literal author content is preserved. These labels describe data, not a
    # fixed conversational order or a new instruction to repeat every field.
    text = ('人物资料：' + json.dumps(core, ensure_ascii=False)
        + '\n关系说明：' + material['relationship']
        + '\n人物补充资料：' + json.dumps(facts, ensure_ascii=False)
        + '\n可见背景资料：' + json.dumps(included_lore, ensure_ascii=False)
        + '\n表达样例：' + json.dumps(included_examples, ensure_ascii=False))
    return text, {'binding_hash': fingerprint({'profile': profile, 'character': character}),
        'material_hash': fingerprint(material), 'characters': len(text),
        'visible_lore_count': len(included_lore), 'visible_example_count': len(included_examples),
        'excluded_lore_count': len(data['lorebook']) - len(included_lore),
        'excluded_example_count': len(data['examples']) - len(included_examples),
        'selection': 'all_enabled_visible_material; no_pcar_retrieval_or_budget_trimming'}


def native_model(original):
    model = {k: deepcopy(v) for k, v in original.items() if not k.startswith('_')}
    model.update(_generation_profile={'version': 'chat-stream-unlimited-v1'},
                 _native_text_profile=dict(REASONING_PROFILE))
    return model


def freeze_trial(home, persona_id, reference_root):
    paths = RuntimePaths(Path(home).resolve())
    store, config = Store(paths), ConfigStore(paths)
    original = config.load()['model']
    model = native_model(original)
    guard_cloud_dispatch(store, persona_id, 'main', model)
    text, materials = visible_persona(store, persona_id)
    if len(text) > model['max_input_chars']:
        raise RuntimeErrorBase('native_reference_material_exceeds_input_limit')
    source = source_status(Path(reference_root))
    if not source.get('source_ready') or not source.get('text_patches_applied'):
        raise RuntimeErrorBase('reference_source_not_ready')
    wire, transport = build_request(model, [{'role': 'system', 'content': '人工公开参数核查'}])
    if ('response_format' in wire or wire.get('reasoning_effort') != 'low'
            or 'max_tokens' in wire or wire.get('stream') is not True):
        raise RuntimeErrorBase('native_reference_wire_contract_mismatch')
    policy = paths.home / 'runtime/private-copy-policy.json'
    return {'materials': materials, 'model_public_hash': fingerprint(public_model(original)),
        'model_private_binding_hash': fingerprint(original),
        'permission_hash': hashlib.sha256(policy.read_bytes()).hexdigest() if policy.is_file() else None,
        'wire_parameters': {k: v for k, v in wire.items() if k != 'messages'},
        'transport': transport, 'source': source,
        'source_hashes': {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in SOURCE_FILES}}


def load_trial(root):
    root = safe(root)
    trial = json.loads((root / 'trial.json').read_text(encoding='utf-8'))
    if trial.get('version') != VERSION or trial.get('status') != 'prepared':
        raise RuntimeErrorBase('native_reference_trial_not_prepared')
    source_home = Path(trial['source_home']).resolve()
    safe(root, source_home / 'tmp/companion-native-reference')
    home = safe(trial['home'], source_home / 'tmp')
    reference = safe(trial['reference_root'])
    if reference != source_home / 'reference/dafeiyu':
        raise RuntimeErrorBase('native_reference_root_mismatch')
    if trial.get('reasoning_profile') != REASONING_PROFILE:
        raise RuntimeErrorBase('native_reference_reasoning_profile_mismatch')
    if type(trial.get('development_material')) is not bool:
        raise RuntimeErrorBase('native_reference_material_scope_missing')
    if not trial['development_material']:
        safe(home, source_home / 'tmp/private-acceptance')
        marker = json.loads((home / 'private-sandbox.json').read_text(encoding='utf-8'))
        if marker.get('formal_home') != str(source_home) or marker.get('persona_id') != trial['persona_id']:
            raise RuntimeErrorBase('native_reference_private_scope_mismatch')
    deadline = trial.get('review_deadline')
    if deadline and datetime.now(timezone.utc) > datetime.fromisoformat(deadline):
        raise RuntimeErrorBase('native_reference_review_expired')
    return trial


def verify_trial(root, plan=None):
    current = load_trial(root)
    if plan is not None and current != plan:
        raise RuntimeErrorBase('native_reference_trial_changed')
    frozen = freeze_trial(current['home'], current['persona_id'], current['reference_root'])
    if frozen != current['frozen']:
        migration_file = data_root(current) / 'source-migration.json'
        if not migration_file.is_file():
            raise RuntimeErrorBase('native_reference_frozen_conditions_changed')
        migration = read_json(migration_file)
        recovery = safe(data_root(current) / 'turns' / migration['turn_id'] / 'postprocess-recovery', data_root(current))
        if (migration.get('version') != RECOVERY_VERSION
                or migration.get('turn_id') != '0001'
                or migration.get('from_frozen_hash') != fingerprint(current['frozen'])
                or migration.get('to_frozen') != frozen
                or migration.get('changes') != recovery_source_changes(current['frozen'], frozen)
                or migration.get('recovery_result_hash') != file_hash(recovery / 'result.json')
                or read_json(recovery / 'terminal.json').get('result_hash') != migration['recovery_result_hash']):
            raise RuntimeErrorBase('native_recovery_source_migration_invalid')
    return current


def data_root(plan):
    return safe(Path(plan['home']) / 'runtime/native-reference', plan['home'])


def checked_input(root, turn_id, plan=None):
    plan = plan or load_trial(root)
    body_root = data_root(plan)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', turn_id):
        raise RuntimeErrorBase('native_reference_turn_id_invalid')
    output = safe(body_root / 'turns' / turn_id, body_root / 'turns')
    value = json.loads((output / 'input.json').read_text(encoding='utf-8'))
    if (set(value) - {'text', 'history', 'seq'} or not isinstance(value.get('text'), str)
            or not value['text'].strip() or not isinstance(value.get('history'), list)):
        raise RuntimeErrorBase('native_reference_input_invalid')
    for m in value['history']:
        if (type(m) is not dict or set(m) != {'role', 'content'}
                or m['role'] not in ('user', 'assistant') or not isinstance(m['content'], str)):
            raise RuntimeErrorBase('native_reference_history_invalid')
    session = json.loads((body_root / 'session.json').read_text(encoding='utf-8'))
    order = session.get('order')
    if not isinstance(order, list) or len(order) != len(set(order)) or not order or order[-1] != turn_id:
        raise RuntimeErrorBase('native_reference_turn_order_changed')
    expected = []
    for prior in order[:-1]:
        if not isinstance(prior, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', prior):
            raise RuntimeErrorBase('native_reference_turn_id_invalid')
        folder = safe(body_root / 'turns' / prior, body_root / 'turns')
        if not (folder / 'terminal.json').is_file():
            raise RuntimeErrorBase('native_reference_previous_turn_unfinished')
        if (folder / 'cancel.json').exists():
            raise RuntimeErrorBase('native_reference_previous_turn_cancelled')
        result = effective_result(folder)
        if result.get('status') != 'completed':
            raise RuntimeErrorBase('native_reference_previous_turn_failed')
        expected = result['native_history']
    if value['history'] != expected:
        raise RuntimeErrorBase('native_reference_history_changed')
    return output, value


def native_display(value, reference_root):
    captured = value.get('captured_messages')
    if (captured == [] and value.get('suppressed') is True
            and value.get('suppression_reason') == 'upstream_no_reply_marker'
            and not (value.get('processed_text') or '').strip()
            and isinstance(value.get('after_response_hook'), str)):
        return {'display': '', 'bubbles': [], 'captured_messages': [],
            'history_reply': value['after_response_hook'], 'suppressed': True,
            'suppression_reason': 'upstream_no_reply_marker',
            'display_adapter': 'upstream_capture_plain_and_named_face; no_pcar_json_decoder'}
    if not isinstance(captured, list) or not captured:
        raise RuntimeErrorBase('native_reference_capture_missing')
    names = {}
    face_map = Path(reference_root) / 'upstream/plugin/astrbot_plugin_face_convert/face_map.json'
    if face_map.is_file():
        for name, face_id in json.loads(face_map.read_text(encoding='utf-8')).items():
            names.setdefault(str(face_id), name)
    bubbles = []
    for chain in captured:
        if not isinstance(chain, list) or not chain:
            raise RuntimeErrorBase('native_reference_capture_invalid')
        parts = []
        for item in chain:
            if isinstance(item, dict) and item.get('type') == 'Plain' and isinstance(item.get('text'), str):
                parts.append(item['text'])
            elif isinstance(item, dict) and item.get('type') == 'Face' and isinstance(item.get('id'), (int, str)):
                key = str(item['id'])
                parts.append('[' + names.get(key, 'QQ表情:' + key) + ']')
            else:
                raise RuntimeErrorBase('native_reference_component_unsupported')
        bubbles.append(''.join(parts))
    if not any(text.strip() for text in bubbles):
        raise RuntimeErrorBase('native_reference_display_empty')
    history_reply = value.get('after_response_hook')
    if not isinstance(history_reply, str) or not history_reply.strip():
        raise RuntimeErrorBase('native_reference_history_reply_missing')
    return {'display': '\n\n'.join(bubbles), 'bubbles': bubbles,
        'captured_messages': captured, 'history_reply': history_reply,
        'suppressed': False,
        'display_adapter': 'upstream_capture_plain_and_named_face; no_pcar_json_decoder'}


class FakeClient:
    async def complete(self, model, messages):
        return ModelCompletion('这是独立人工原生文本夹具。', None, {'finish_reason': 'stop'})


async def recover_turn(root, turn_id, *, upstream=None):
    """Explicit offline repair of the first interval IndexError, never generation."""
    from unittest.mock import patch
    root = safe(root)
    plan = load_trial(root)
    locks = []
    try:
        for path in (root / 'window.lock', Path(plan['home']) / 'runtime/service.lock', root / 'native-execution.lock'):
            locks.append(InstanceLock(path))
        folder, source_input = checked_input(root, turn_id, plan)
        if turn_id != '0001' or read_json(data_root(plan) / 'session.json')['order'] != ['0001']:
            raise RuntimeErrorBase('native_recovery_not_first_incident')
        recovery = folder / 'postprocess-recovery'
        if recovery.exists():
            # A partial recovery is a checkpoint, never an instruction to replay.
            return effective_result(folder)
        bindings, original, request, generation = recovery_originals(folder)
        frozen = freeze_trial(plan['home'], plan['persona_id'], plan['reference_root'])
        changes = recovery_source_changes(plan['frozen'], frozen)
        if ((original['kind'] == 'simulated') != plan['development_material']
                or {k: v for k, v in request['wire'].items() if k != 'messages'} != frozen['wire_parameters']):
            raise RuntimeErrorBase('native_recovery_request_conditions_changed')
        trial_hash = file_hash(root / 'trial.json')
        recovery.mkdir()
        atomic_json(recovery / 'started.json', {'version': RECOVERY_VERSION, 'started_at': timestamp(),
            'bindings': bindings, 'trial_hash': trial_hash, 'new_generation_calls': 0})
        record = {'version': RECOVERY_VERSION, 'status': 'running', 'trial_root': str(root),
            'bindings': bindings, 'trial_hash': trial_hash, 'new_generation_calls': 0}
        replay_count = 0
        try:
            paths = RuntimePaths(Path(plan['home']))
            persona, _ = visible_persona(Store(paths), plan['persona_id'])
            model = native_model(ConfigStore(paths).load()['model'])
            runtime_name = 'runtime-native-recovery-' + identifier()
            runtime_path = safe(data_root(plan) / 'upstream' / runtime_name, data_root(plan))
            journal = data_root(plan) / 'reference-cleanup.json'
            cleanup = read_json(journal) if journal.exists() else {'version': 1, 'paths': []}
            cleanup['paths'].append(str(runtime_path)); atomic_json(journal, cleanup)

            async def replay(messages):
                nonlocal replay_count
                replay_count += 1
                wire, transport = build_request(model, messages)
                if (replay_count != 1 or messages != request['messages'] or wire != request['wire']
                        or transport != request['transport'] or recovery_originals(folder)[0] != bindings):
                    raise RuntimeErrorBase('native_recovery_replay_request_changed')
                return generation['raw_text']

            # These guards apply even to an injected test upstream. No client is
            # constructed and audited_complete is deliberately not called.
            with patch('socket.socket.connect', side_effect=RuntimeError('offline_network_blocked')), \
                    patch('socket.create_connection', side_effect=RuntimeError('offline_network_blocked')), \
                    patch('asyncio.BaseEventLoop.create_connection', side_effect=RuntimeError('offline_network_blocked')), \
                    patch.object(ChatCompletionsClient, 'complete', side_effect=RuntimeError('offline_real_client_blocked')):
                with (recovery / 'upstream-console.txt').open('w', encoding='utf-8') as console:
                    with redirect_stdout(console), redirect_stderr(console):
                        value = await (upstream or run_reference)(Path(plan['reference_root']), persona=persona,
                            user=source_input['text'], history=source_input['history'], output_contract='',
                            complete=replay, runtime_name=runtime_name, owned_home=paths.home)
            if (replay_count != 1 or value.get('messages') != request['messages']
                    or recovery_originals(folder)[0] != bindings or file_hash(root / 'trial.json') != trial_hash
                    or freeze_trial(plan['home'], plan['persona_id'], plan['reference_root']) != frozen):
                raise RuntimeErrorBase('native_recovery_conditions_changed')
            history = value.get('native_history')
            if (not isinstance(history, list) or not history or any(type(item) is not dict
                    or set(item) != {'role', 'content'} or item['role'] not in ('user', 'assistant')
                    or not isinstance(item['content'], str) for item in history)):
                raise RuntimeErrorBase('native_recovery_history_missing')
            atomic_json(recovery / 'upstream-result.json', value)
            result = deepcopy(original)
            result.pop('error_code', None)
            result.update(status='completed', **native_display(value, plan['reference_root']),
                native_history=history, messages_hash=request['messages_hash'],
                recovered_reference_runtime=str(runtime_path), postprocess_recovery=RECOVERY_VERSION)
            record.update(status='completed', result=result, stored_response_replays=replay_count)
            atomic_json(recovery / 'result.json', record)
            digest = file_hash(recovery / 'result.json')
            atomic_json(recovery / 'terminal.json', {'version': RECOVERY_VERSION, 'status': 'completed',
                'result_hash': digest, 'new_generation_calls': 0, 'stored_response_replays': 1})
            atomic_json(data_root(plan) / 'source-migration.json', {'version': RECOVERY_VERSION,
                'turn_id': turn_id, 'from_frozen_hash': fingerprint(plan['frozen']), 'to_frozen': frozen,
                'changes': changes, 'recovery_result_hash': digest, 'reason': 'native_multi_bubble_interval_IndexError'})
            return effective_result(folder)
        except BaseException as error:
            # Once a recovery receipt exists, never overwrite it after a later
            # migration/persistence failure. It remains a visible checkpoint.
            if not (recovery / 'result.json').exists():
                record.update(status='failed', error_code=error_code(error), stored_response_replays=replay_count)
                atomic_json(recovery / 'result.json', record)
                atomic_json(recovery / 'terminal.json', {'version': RECOVERY_VERSION, 'status': 'failed',
                    'result_hash': file_hash(recovery / 'result.json'), 'new_generation_calls': 0})
            raise
    finally:
        for lock in reversed(locks): lock.close()


async def run_turn(root, turn_id, *, fake=False, client=None, upstream=None):
    root = safe(root)
    load_trial(root)  # Validate ownership before InstanceLock can create a file.
    lease = InstanceLock(root / 'native-execution.lock')
    try:
        return await _run_turn(root, turn_id, fake=fake, client=client, upstream=upstream)
    finally:
        lease.close()


async def _run_turn(root, turn_id, *, fake, client, upstream):
    plan = verify_trial(root)
    if bool(fake) != plan['development_material']:
        raise RuntimeErrorBase('native_reference_simulation_scope_mismatch')
    output, source_input = checked_input(root, turn_id, plan)
    if (output / 'started.json').exists() or (output / 'terminal.json').exists():
        raise RuntimeErrorBase('native_reference_turn_already_started')
    kind = 'simulated' if fake else 'real'
    start = {'version': TURN_VERSION, 'turn_id': turn_id, 'kind': kind, 'started_at': timestamp()}
    with (output / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump(start, stream, ensure_ascii=False, indent=2)
    paths = RuntimePaths(Path(plan['home']))
    store, config = Store(paths), ConfigStore(paths)
    model = native_model(config.load()['model'])
    persona, _ = visible_persona(store, plan['persona_id'])
    input_hash = hashlib.sha256((output / 'input.json').read_bytes()).hexdigest()
    runtime_name = 'runtime-native-reference-' + identifier()
    runtime_path = safe(data_root(plan) / 'upstream' / runtime_name, data_root(plan) / 'upstream')
    journal = data_root(plan) / 'reference-cleanup.json'
    clean = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {'version': 1, 'paths': []}
    clean['paths'].append(str(runtime_path))
    atomic_json(journal, clean)
    audit = {}
    completion = None
    dispatches = 0
    started = time.perf_counter()
    result = {**start, 'status': 'running', 'reference_runtime': str(runtime_path), 'actual_qq': False}
    atomic_json(output / 'result.json', result)

    def before_attempt(state):
        if (output / 'cancel.json').exists():
            raise asyncio.CancelledError('user_cancelled')
        verify_trial(root, plan)
        if hashlib.sha256((output / 'input.json').read_bytes()).hexdigest() != input_hash:
            raise RuntimeErrorBase('native_reference_input_changed')
        checked_input(root, turn_id, plan)
        guard_cloud_dispatch(store, plan['persona_id'], 'main', model)
        if any(a.get('status') == 'completed' for a in state['attempts']):
            raise RuntimeErrorBase('native_reference_success_replay')

    def save(value):
        nonlocal audit
        audit = deepcopy(value)
        for attempt in audit.get('generation_retry', {}).get('attempts', []):
            reserve = output / (attempt['id'] + '.reserve.json')
            if not reserve.exists():
                if attempt['status'] != 'running':
                    raise RuntimeErrorBase('native_reference_reservation_missing')
                with reserve.open('x', encoding='utf-8') as stream:
                    json.dump({'kind': kind, 'turn_id': turn_id, 'attempt': attempt}, stream, ensure_ascii=False, indent=2)
            atomic_json(output / (attempt['id'] + '.attempt.json'), attempt)
        atomic_json(output / 'audit.json', audit)

    def validate(text):
        if not isinstance(text, str) or not text.strip():
            raise ModelError('provider_empty_response')

    async def complete(messages):
        nonlocal completion, dispatches
        dispatches += 1
        if dispatches != 1:
            raise RuntimeErrorBase('native_reference_unexpected_auxiliary')
        expected_tail = source_input['history'] + [{'role': 'user', 'content': source_input['text']}]
        if (not isinstance(messages, list) or not messages or messages[0].get('role') != 'system'
                or not isinstance(messages[0].get('content'), str)
                or persona not in messages[0]['content'] or messages[1:] != expected_tail):
            raise RuntimeErrorBase('native_reference_upstream_input_changed')
        if sum(len(m['content']) for m in messages) > model['max_input_chars']:
            raise RuntimeErrorBase('native_reference_context_limit_exceeded')
        wire, transport = build_request(model, messages)
        if {k: v for k, v in wire.items() if k != 'messages'} != plan['frozen']['wire_parameters']:
            raise RuntimeErrorBase('native_reference_wire_changed')
        atomic_json(output / 'request.json', {'messages': messages, 'model': public_model(model),
            'wire': wire, 'wire_hash': fingerprint(wire), 'messages_hash': fingerprint(messages),
            'transport': transport, 'format': 'native_text_no_pcar_output_contract'})
        completion = await audited_complete(client or (FakeClient() if fake else ChatCompletionsClient()),
            model, messages, validate=validate, save=save, before_attempt=before_attempt)
        atomic_json(output / 'generation-result.json', {'kind': kind, 'raw_text': completion[0],
            'usage': completion[1], 'diagnostics': completion.diagnostics})
        return completion[0]

    current = asyncio.current_task()
    async def cancellation_watcher():
        while True:
            if (output / 'cancel.json').exists():
                current.cancel('user_cancelled')
                return
            await asyncio.sleep(.05)
    watcher = asyncio.create_task(cancellation_watcher())
    try:
        before_attempt({'attempts': []})
        with (output / 'upstream-console.txt').open('w', encoding='utf-8') as console:
            with redirect_stdout(console), redirect_stderr(console):
                value = await (upstream or run_reference)(Path(plan['reference_root']), persona=persona,
                    user=source_input['text'], history=source_input['history'], output_contract='',
                    complete=complete, runtime_name=runtime_name, owned_home=paths.home)
        before_attempt({'attempts': []})
        atomic_json(output / 'upstream-result.json', value)
        if completion is None or dispatches != 1 or not audit.get('generation_retry', {}).get('attempts'):
            raise RuntimeErrorBase('native_reference_generation_receipt_missing')
        result.update(status='completed', **native_display(value, plan['reference_root']),
            messages_hash=fingerprint(value['messages']), diagnostics=completion.diagnostics,
            native_history=value['native_history'])
    except BaseException as error:
        result.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
                      error_code='request_interrupted' if isinstance(error, asyncio.CancelledError) else error_code(error))
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
    attempts = audit.get('generation_retry', {}).get('attempts', [])
    result.update(usage=audit, generation_attempts=len(attempts),
        physical_calls=0 if fake else len(attempts), simulated_calls=len(attempts) if fake else 0,
        elapsed_seconds=time.perf_counter() - started)
    # A terminal marker is written last. Failed persistence leaves a visible
    # unfinished turn; the parent must never infer success or regenerate it.
    atomic_json(output / 'result.json', result)
    atomic_json(output / 'terminal.json', {k: result[k] for k in
        ('version', 'turn_id', 'kind', 'status', 'physical_calls', 'simulated_calls', 'elapsed_seconds')}
        | ({'error_code': result['error_code']} if result.get('error_code') else {}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('turn')
    p.add_argument('trial', type=Path)
    p.add_argument('turn_id')
    p.add_argument('--fake', action='store_true')
    p = sub.add_parser('recover', help='offline postprocessing of the saved interval-error response only')
    p.add_argument('trial', type=Path)
    p.add_argument('turn_id')
    args = parser.parse_args()

    async def invoke():
        if args.action == 'recover':
            return await recover_turn(args.trial, args.turn_id)
        if args.fake:
            from unittest.mock import patch
            with patch('socket.socket.connect', side_effect=RuntimeError('offline_network_blocked')), \
                    patch('socket.create_connection', side_effect=RuntimeError('offline_network_blocked')), \
                    patch.object(ChatCompletionsClient, 'complete', side_effect=RuntimeError('offline_real_client_blocked')):
                return await run_turn(args.trial, args.turn_id, fake=True)
        return await run_turn(args.trial, args.turn_id)
    try:
        result = asyncio.run(invoke())
        summary = {k: result[k] for k in ('status', 'physical_calls', 'simulated_calls')}
        if args.action == 'recover':
            summary.update(action='offline_postprocess_recovery', new_generation_calls=0)
        print(json.dumps(summary, ensure_ascii=False))
        if result['status'] != 'completed':
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_code': error_code(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
