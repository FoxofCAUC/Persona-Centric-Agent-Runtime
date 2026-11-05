"""One private six-position prompt-order comparison, never a production route.

Source requests are admitted normally and frozen from a verified preview, then
cancelled without dispatch. The two experimental generations have their own
native retry ledger. Explicitly authored history is local simulated material,
not restored private conversation or real provider success.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_block_order import transform_request
from experiments.companion_comparison import local_retrieval_ready, public_model
from experiments.companion_trial import configure, configure_policy, copy_weights
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.generation_retry import audited_complete
from persona_runtime.ingress import IngressService
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, guard_cloud_dispatch, provider_identity, safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-order-real-v1'
CONTRACT_VERSION = 'companion-contract-language-real-v1'


def experiment_settings(kind):
    if kind == 'block_order':
        return {'version': VERSION, 'namespace': 'companion-order',
                'title': '三组匿名顺序对照',
                'description': '相同人物资料、人工上下文、模型与参数；只改变提示块顺序。'}
    if kind == 'contract_language':
        return {'version': CONTRACT_VERSION, 'namespace': 'companion-contract',
                'title': '三组匿名合同文字对照',
                'description': '相同人物资料、人工上下文、模型、参数与提示块顺序；只改变回复合同文字。'}
    raise RuntimeErrorBase('companion_comparison_kind_invalid')


def _plan_kind(plan):
    # Historical v1 order plans predate this explicit discriminator.
    kind = plan.get('experiment_kind', 'block_order')
    settings = experiment_settings(kind)
    if plan.get('version') != settings['version']:
        raise RuntimeErrorBase('companion_comparison_kind_version_mismatch')
    return kind


def _transform(kind, source):
    experiment_settings(kind)
    if kind == 'block_order':
        return transform_request(source)
    from experiments.companion_contract_language import transform_request as contract_transform
    return contract_transform(source)
CASES = (
    {'id': 'O01', 'category': 'daily_sharing', 'history': [], 'inputs': [
        '本来想走近路，结果拐进一条全是老树的小巷，慢了十分钟，倒觉得赚了。']},
    {'id': 'O02', 'category': 'ordered_wish_change', 'history': [], 'inputs': [
        '帮我挑一下这两张海报，第一张规整，第二张乱一点但很有劲。',
        '不用替我决定了，我大概会选第二张。就是想听听你自己的感觉。']},
    {'id': 'O03', 'category': 'continuing_viewpoint', 'history': [
        ['路过广场，露天舞台很热闹，旁边小路却很安静。', '你最后往哪边走了？'],
        ['我先在小路走了一会儿，回头又听了一首现场。', '两边都逛到了。']],
     'inputs': ['要是你，会在哪边多待一会儿？']},
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _error(error):
    text = str(error)
    return text if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', text) else type(error).__name__


def _public_config(config):
    return public_model({key: config[key] for key in ('model', 'character_selector')})


def _source_files(experiment_kind='block_order'):
    names = list((ROOT / 'persona_runtime').glob('*.py')) + [Path(__file__)]
    names += [ROOT / 'experiments' / name for name in (
        'companion_block_order.py', 'companion_private_reference.py',
        'companion_comparison.py', 'companion_trial.py')]
    if experiment_kind == 'contract_language':
        names += [ROOT / 'experiments' / name for name in (
            'companion_contract_language.py', 'companion_contract_comparison.py')]
    return {str(p.relative_to(ROOT)).replace('\\', '/'): _sha(p) for p in names}


def _path(root, *, expected_kind=None):
    root = safe(root)
    plan = json.loads(safe(root / 'plan.json', root).read_text('utf-8'))
    kind = _plan_kind(plan)
    if expected_kind is not None and kind != expected_kind:
        raise RuntimeErrorBase('companion_comparison_kind_mismatch')
    source_home = Path(plan['source_home']).resolve()
    if not plan.get('development_material') and source_home != DEFAULT_HOME.resolve():
        raise RuntimeErrorBase('order_comparison_formal_source_scope')
    parent = source_home / 'tmp' / experiment_settings(kind)['namespace']
    safe(root, parent)
    if root.parent != parent or not re.fullmatch(r'[0-9a-f-]{36}', root.name):
        raise RuntimeErrorBase('order_comparison_package_scope')
    return root, plan


def load_plan(root, *, expected_kind=None):
    root, plan = _path(root, expected_kind=expected_kind)
    if plan.get('status') != 'prepared':
        raise RuntimeErrorBase('order_comparison_not_prepared')
    freeze = json.loads((root / 'freeze.json').read_text('utf-8'))
    if freeze['plan_hash'] != fingerprint(plan):
        raise RuntimeErrorBase('order_comparison_plan_changed')
    if datetime.now(timezone.utc) > datetime.fromisoformat(plan['review_deadline']):
        raise RuntimeErrorBase('order_comparison_review_expired')
    case_ids = ['O01', 'O02', 'O03']
    if [row.get('id') for row in plan['cases']] != case_ids:
        raise RuntimeErrorBase('order_comparison_case_scope')
    expected_positions = {case + '-' + condition for case in case_ids for condition in ('control', 'variant')}
    if (len(plan['positions']) != 6 or {row.get('id') for row in plan['positions']} != expected_positions
            or plan['order'] != ['O01-control', 'O01-variant', 'O02-variant', 'O02-control', 'O03-control', 'O03-variant']):
        raise RuntimeErrorBase('order_comparison_position_scope')
    for case in plan['cases']:
        cid = case['id']
        if case.get('source_request_file') != cid + '/source.json' or case.get('source_preview_file') != cid + '/preview.json':
            raise RuntimeErrorBase('order_comparison_case_scope')
        safe(root / cid, root)
    for row in plan['positions']:
        if (row.get('case_id') not in case_ids or row.get('condition') not in ('control', 'variant')
                or row['id'] != row['case_id'] + '-' + row['condition']
                or row.get('request_file') != row['case_id'] + '/' + row['condition'] + '.json'):
            raise RuntimeErrorBase('order_comparison_position_scope')
        safe(root / row['request_file'], root)
    safe(root / 'positions', root)
    return plan


def verify(root, *, expected_kind=None):
    root = Path(root).resolve()
    plan = load_plan(root, expected_kind=expected_kind)
    kind = _plan_kind(plan)
    for name, digest in plan['source_hashes'].items():
        if _sha(ROOT / name) != digest:
            raise RuntimeErrorBase('order_comparison_implementation_changed')
    for case in plan['cases']:
        home = safe(case['home'], Path(plan['source_home']) / 'tmp/private-acceptance')
        checks = {
            home / 'data/database/runtime.sqlite3': case['source_database_sha256'],
            home / 'private-sandbox.json': case['source_owner_sha256'],
            home / 'runtime/private-copy-policy.json': case['source_policy_sha256'],
            root / case['source_request_file']: case['source_request_sha256'],
            root / case['source_preview_file']: case['source_preview_sha256'],
        }
        if any(_sha(path) != digest for path, digest in checks.items()):
            raise RuntimeErrorBase('order_comparison_source_changed')
        config = ConfigStore(RuntimePaths(home)).load()
        if fingerprint(_public_config(config)) != case['source_public_config_hash']:
            raise RuntimeErrorBase('order_comparison_provider_changed')
        source = json.loads((root / case['source_request_file']).read_text('utf-8'))
        variants = _transform(kind, source)
        for condition in ('control', 'variant'):
            item = root / case['id'] / (condition + '.json')
            if json.loads(item.read_text('utf-8')) != variants[condition]:
                raise RuntimeErrorBase('order_comparison_request_changed')
    return {'version': plan['version'], 'experiment_kind': kind, 'status': 'verified', 'case_count': 3,
            'positions': 6, 'private': not plan['development_material'], 'real_model_calls': 0}


def _ensure_no_unfinished(root):
    for started in (Path(root) / 'positions').glob('*/started.json'):
        terminal = started.with_name('terminal.json')
        if not terminal.is_file():
            raise RuntimeErrorBase('order_comparison_unfinished_no_auto_resume')
        if json.loads(terminal.read_text('utf-8')).get('status') != 'completed':
            raise RuntimeErrorBase('order_comparison_batch_stopped')


def _ready(engine, pid, inputs, *, build, development):
    return local_retrieval_ready(engine.cognitive.characters, pid, inputs,
                                 build=build, development_material=development)


class FakeClient:
    """Local fixture client; its replies never count as real model calls."""
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = 0

    async def complete(self, model, messages):
        self.calls += 1
        text = self.replies.pop(0) if self.replies else '这是人工离线回包，不能判断真实效果。'
        body = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': text, 'content_kind': 'chat', 'cancel_if_new_input': False}]}, ensure_ascii=False)
        return ModelCompletion(body, None, {'finish_reason': 'stop'})


async def _history(engine, ingress, pid, history):
    for user, reply in history:
        receipt = ingress.accept({'channel': 'desktop', 'text': user,
                                  'request_id': identifier(), 'persona_id': pid})
        batch = ingress.get(receipt['batch_id'])
        ingress.action(batch['id'], 'seal', batch['revision'], identifier(), pid)
        await engine.wait()
        turn = engine.get(ingress.get(batch['id'])['turn_id'])
        if turn['status'] != 'completed' or turn.get('reply') != reply:
            raise RuntimeErrorBase('order_comparison_simulated_history_failed')


async def _prepare_case(root, case, paths, pid, *, development, experiment_kind='block_order'):
    store, config = Store(paths), ConfigStore(paths)
    fixture = FakeClient([row[1] for row in case['history']])
    engine = ChatEngine(store, config, client=fixture)
    ingress = IngressService(engine)
    try:
        # No preparation callback may ever reach an HTTP client.
        _ready(engine, pid, case['inputs'], build=True, development=development)
        await _history(engine, ingress, pid, case['history'])
        _ready(engine, pid, case['inputs'], build=False, development=development)
        with patch.object(engine, '_schedule'):
            receipts = [ingress.accept({'channel': 'desktop', 'text': text,
                        'request_id': identifier(), 'persona_id': pid}) for text in case['inputs']]
            if len({r['batch_id'] for r in receipts}) != 1:
                raise RuntimeErrorBase('order_comparison_members_split')
            batch = ingress.get(receipts[0]['batch_id'])
            ingress.action(batch['id'], 'seal', batch['revision'], identifier(), pid)
        turn_id = ingress.get(batch['id'])['turn_id']
        prepared = engine.cognitive.prepare(turn_id)
        with store.connection() as conn:
            turn = dict(conn.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone())
            prepared = engine.cognitive.finalize(conn, turn, prepared)
        retrieval = prepared['manifest'].get('character_retrieval') or {}
        if not development and (retrieval.get('effective_mode') != 'hybrid' or retrieval.get('degrade_reason')):
            raise RuntimeErrorBase('order_comparison_retrieval_degraded')
        model = json.loads(turn['model_snapshot'])
        model.update(_reply_mode=turn['reply_mode_snapshot'], _expression_snapshot=turn['expression_snapshot'])
        messages = prepared['messages']
        wire, transport = build_request(model, messages)
        request = {'kind': 'private_frozen_source' if not development else 'development_frozen_source',
                   'messages': messages, 'model': public_model(model), 'wire': wire,
                   'transport': transport, 'messages_hash': fingerprint(messages), 'wire_hash': fingerprint(wire)}
        result = _transform(experiment_kind, request)
        directory = root / case['id']; directory.mkdir()
        for condition in ('control', 'variant'):
            atomic_json(directory / (condition + '.json'), result[condition])
        atomic_json(directory / 'source.json', request)
        atomic_json(directory / 'diagnostic.json', result['diagnostic'])
        # External experiment evidence, not a persisted production invocation.
        atomic_json(directory / 'preview.json', prepared['manifest'])
        await engine.cancel(turn_id)
        with store.connection() as conn:
            source = dict(conn.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone())
            if source['status'] in ('queued', 'running'):
                raise RuntimeErrorBase('order_comparison_source_not_cancelled')
            if conn.execute('SELECT count(*) FROM attempts WHERE turn_id=?', (turn_id,)).fetchone()[0]:
                raise RuntimeErrorBase('order_comparison_source_dispatched')
        return {'id': case['id'], 'category': case['category'], 'inputs': case['inputs'],
                'artificial_history': case['history'], 'simulated_source_history': bool(case['history']),
                'simulated_history_calls': fixture.calls, 'source_preview_real_calls': 0,
                'home': str(paths.home), 'persona_id': pid, 'source_turn_id': turn_id,
                'source_request_file': case['id'] + '/source.json',
                'source_preview_file': case['id'] + '/preview.json',
                'source_request_sha256': _sha(directory / 'source.json'),
                'source_preview_sha256': _sha(directory / 'preview.json'),
                'source_database_sha256': _sha(paths.home / 'data/database/runtime.sqlite3'),
                'source_owner_sha256': _sha(paths.home / 'private-sandbox.json'),
                'source_policy_sha256': _sha(paths.home / 'runtime/private-copy-policy.json'),
                'source_public_config_hash': fingerprint(_public_config(config.load())),
                'local_retrieval': retrieval}
    finally:
        for turn in engine.requests():
            if turn['status'] in ('queued', 'running'):
                await engine.cancel(turn['id'])
        await engine.wait()
        engine.cognitive.characters.close(); engine.cognitive.shadow.close()


async def prepare(source_home=DEFAULT_HOME, *, approve_current_provider=False, development_material=False,
                  experiment_kind='block_order'):
    settings = experiment_settings(experiment_kind)
    source_paths = RuntimePaths(Path(source_home).resolve())
    if not development_material and source_paths.home != DEFAULT_HOME.resolve():
        raise RuntimeErrorBase('order_comparison_formal_source_scope')
    config = ConfigStore(source_paths).load()
    private = PrivatePersonas(source_paths)
    ready = [] if development_material else [r for r in private.load(True)['records'] if r.get('state') == 'ready']
    if not development_material:
        if len(ready) != 1:
            raise RuntimeErrorBase('order_comparison_requires_one_ready_role')
        if not approve_current_provider and any(ready[0].get('approved_models', {}).get(kind) !=
                provider_identity(config[field]) for kind, field in (('main', 'model'), ('selector', 'character_selector'))):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    parent = safe(source_paths.home / 'tmp' / settings['namespace']); parent.mkdir(parents=True, exist_ok=True)
    root = safe(parent / identifier(), parent); root.mkdir()
    plan = {'version': settings['version'], 'experiment_kind': experiment_kind,
            'status': 'preparing', 'source_home': str(source_paths.home),
            'private': not development_material, 'development_material': bool(development_material),
            'created_at': timestamp(), 'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            'source_kind': 'normal admission preview, cancelled before main dispatch; authored simulated history',
            'cases': [], 'positions': [], 'order': [], 'cleanup_homes': [], 'source_hashes': {}}
    atomic_json(root / 'plan.json', plan)
    try:
        for index, case in enumerate(CASES):
            if development_material:
                home = safe(source_paths.home / 'tmp/private-acceptance' / identifier(), source_paths.home / 'tmp')
                paths = RuntimePaths(home); store = Store(paths); store.initialize()
                service = RuntimeService(store); pid = service.create_persona(generic_persona())['id']; service.use_persona(pid)
                atomic_json(home / 'private-sandbox.json', {'persona_id': pid, 'private_id': 'artificial-fixture',
                            'formal_home': str(source_paths.home)})
            else:
                copy = private.create_sandbox(ready[0]['id']); paths = RuntimePaths(Path(copy['home'])); pid = copy['persona_id']
            plan['cleanup_homes'].append(str(paths.home)); atomic_json(root / 'plan.json', plan)
            configure(paths, config, pid)
            atomic_json(paths.home / 'runtime/private-copy-policy.json', {'persona_id': pid,
                'approved_models': {kind: provider_identity(config[field]) for kind, field in
                                    (('main', 'model'), ('selector', 'character_selector'))},
                'approval_scope': 'this_companion_order_round_only' if experiment_kind == 'block_order'
                                  else 'this_companion_contract_language_round_only'})
            if not development_material:
                copy_weights(source_paths, paths)
            configure_policy(Store(paths), ConfigStore(paths), pid, 'minimal', background_prepare=False)
            # Preparation is entirely local, including explicitly authored history.
            with patch('socket.socket.connect', side_effect=RuntimeErrorBase('order_prepare_network_disabled')):
                row = await _prepare_case(root, case, paths, pid, development=development_material,
                                          experiment_kind=experiment_kind)
            sides = ['control', 'variant']; random.SystemRandom().shuffle(sides)
            row['anonymous'] = {'A': case['id'] + '-' + sides[0], 'B': case['id'] + '-' + sides[1]}
            plan['cases'].append(row)
            for condition in ('control', 'variant'):
                plan['positions'].append({'id': case['id'] + '-' + condition, 'case_id': case['id'],
                                         'condition': condition, 'request_file': case['id'] + '/' + condition + '.json'})
            execution = ('variant', 'control') if index == 1 else ('control', 'variant')
            plan['order'].extend(case['id'] + '-' + name for name in execution)
            atomic_json(root / 'plan.json', plan)
        plan['source_hashes'] = _source_files(experiment_kind); plan['status'] = 'prepared'
        atomic_json(root / 'plan.json', plan)
        atomic_json(root / 'freeze.json', {'plan_hash': fingerprint(plan)})
        atomic_json(root / 'state.json', {'status': 'prepared', 'automatic_resume': False})
        verify(root)
        return root
    except BaseException as error:
        atomic_json(root / 'state.json', {'status': 'preparation_blocked', 'error_code': _error(error),
                    'reason': getattr(error, 'preparation_reason', None), 'real_model_calls': 0,
                    'automatic_resume': False})
        raise


def _lookup(plan, position_id):
    row = next((r for r in plan['positions'] if r['id'] == position_id), None)
    if row is None:
        raise RuntimeErrorBase('order_comparison_position_invalid')
    case = next(c for c in plan['cases'] if c['id'] == row['case_id'])
    return case, row


async def run_position(root, position_id, *, fake=False, client=None, expected_kind=None):
    root = Path(root).resolve()
    load_plan(root, expected_kind=expected_kind)  # Validate ownership before creating the execution lease.
    lease = InstanceLock(safe(root / 'execution.lock', root))
    try:
        return await _run_locked(root, position_id, fake=fake, client=client)
    finally:
        lease.close()


async def _run_locked(root, position_id, *, fake, client):
    plan = load_plan(root); verify(root)
    if bool(fake) != bool(plan['development_material']):
        raise RuntimeErrorBase('order_comparison_simulation_scope_mismatch')
    state = json.loads((root / 'state.json').read_text('utf-8'))
    if state['status'] == 'stopped':
        raise RuntimeErrorBase('order_comparison_batch_stopped')
    _ensure_no_unfinished(root)
    case, row = _lookup(plan, position_id)
    output = safe(root / 'positions' / position_id, root)
    if (output / 'started.json').exists():
        raise FileExistsError('order_comparison_position_already_started')
    completed = {path.parent.name for path in (root / 'positions').glob('*/terminal.json')
                 if json.loads(path.read_text('utf-8')).get('status') == 'completed'}
    expected_next = next((name for name in plan['order'] if name not in completed), None)
    if position_id != expected_next:
        raise RuntimeErrorBase('order_comparison_execution_order_mismatch')
    output.mkdir(parents=True, exist_ok=True)
    kind = 'simulated' if fake else 'real'
    with (output / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump({'position_id': position_id, 'kind': kind, 'experiment_kind': _plan_kind(plan),
                   'version': plan['version'], 'started_at': timestamp()}, stream)
    result = {'position_id': position_id, 'kind': kind, 'experiment_kind': _plan_kind(plan),
              'version': plan['version'], 'status': 'reserved', 'physical_calls': 0,
              'simulated_calls': 0, 'experiment_history_not_production_manifest': True}
    audit = {}; engine = None; started = time.perf_counter()
    try:
        paths = RuntimePaths(Path(case['home'])); store = Store(paths); config = ConfigStore(paths)
        engine = ChatEngine(store, config, client=FakeClient())
        request = json.loads((root / row['request_file']).read_text('utf-8'))
        with store.connection() as conn:
            source = dict(conn.execute('SELECT * FROM turns WHERE id=?', (case['source_turn_id'],)).fetchone())
        accepted = json.loads(source['model_snapshot'])
        model = deepcopy(request['model']); model['encrypted_key'] = accepted.get('encrypted_key', '')
        if public_model(model) != request['model']:
            raise RuntimeErrorBase('order_comparison_model_restore_mismatch')

        def before_attempt(native):
            verify(root)
            guard_cloud_dispatch(store, case['persona_id'], 'main', model)
            _ready(engine, case['persona_id'], case['inputs'], build=False, development=fake)
            fresh = engine.cognitive.prepare(case['source_turn_id'])
            with store.connection() as conn:
                current = dict(conn.execute('SELECT * FROM turns WHERE id=?', (case['source_turn_id'],)).fetchone())
                fresh = engine.cognitive.finalize(conn, current, fresh)
            frozen = json.loads((root / case['source_request_file']).read_text('utf-8'))
            retrieval = fresh['manifest'].get('character_retrieval') or {}
            if not fake and (retrieval.get('effective_mode') != 'hybrid' or retrieval.get('degrade_reason')):
                raise RuntimeErrorBase('order_comparison_retrieval_degraded')
            if fresh['messages'] != frozen['messages']:
                raise RuntimeErrorBase('order_comparison_sources_changed')
            if any(attempt['status'] == 'completed' for attempt in native['attempts']):
                raise RuntimeErrorBase('order_comparison_success_replay')

        def save(value):
            nonlocal audit
            audit = deepcopy(value)
            for attempt in audit.get('generation_retry', {}).get('attempts', []):
                reserve = output / (attempt['id'] + '.reserve.json')
                if not reserve.exists():
                    if attempt['status'] != 'running':
                        raise RuntimeErrorBase('order_comparison_reservation_missing')
                    with reserve.open('x', encoding='utf-8') as stream:
                        json.dump({'kind': kind, 'attempt': attempt}, stream)
                atomic_json(output / (attempt['id'] + '.attempt.json'), attempt)
            atomic_json(output / 'audit.json', audit)

        atomic_json(output / 'request.json', request)
        completion = await audited_complete(client or (FakeClient() if fake else ChatCompletionsClient()),
            model, request['messages'], validate=lambda text: validate_reply(text, model['_reply_mode'], model['_expression_snapshot']),
            save=save, before_attempt=before_attempt)
        batch = validate_reply(completion[0], model['_reply_mode'], model['_expression_snapshot'])
        segments = [{'text': segment.text, 'format': segment.format, 'content_kind': segment.content_kind,
                     'cancel_if_new_input': segment.cancel_if_new_input, 'wait_ms': segment.wait_ms}
                    for segment in batch.segments]
        result.update(raw=completion[0], reply=batch.text, decoded_segments=segments,
                      bubbles=[segment.text for segment in batch.segments], diagnostics=completion.diagnostics,
                      request_messages_hash=request['messages_hash'], request_wire_hash=request['wire_hash'])
        # A completed generation remains completed even if common presentation
        # later fails. Presentation findings never enter the model retry loop.
        atomic_json(output / 'generation-result.json', {'kind': kind, 'raw': completion[0],
                    'reply': batch.text, 'decoded_segments': segments, 'usage': completion[1],
                    'diagnostics': completion.diagnostics})
        if _plan_kind(plan) == 'contract_language':
            from persona_runtime.chat_display import bubble_plan
            from persona_runtime.bubble_contract import V3
            displayed = bubble_plan(segments)
            result.update(display='\n\n'.join(message['text'] for message in displayed),
                          display_messages=displayed, display_projection=V3.presentation,
                          display_projection_diagnostics=[message['diagnostics'] for message in displayed])
        else:
            result['display'] = batch.text
        result['status'] = 'completed'
    except BaseException as error:
        result.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed', error_code=_error(error))
        atomic_json(root / 'state.json', {'status': 'stopped', 'position_id': position_id,
                    'error_code': result['error_code'], 'automatic_resume': False})
    finally:
        attempts = audit.get('generation_retry', {}).get('attempts', [])
        result.update(usage=audit, physical_calls=0 if fake else len(attempts),
                      simulated_calls=len(attempts) if fake else 0, elapsed_seconds=time.perf_counter() - started)
        if engine is not None:
            engine.cognitive.characters.close(); engine.cognitive.shadow.close()
        atomic_json(output / 'result.json', result)
        atomic_json(output / 'terminal.json', {key: result.get(key) for key in (
            'position_id', 'kind', 'status', 'physical_calls', 'simulated_calls', 'elapsed_seconds', 'error_code')})
    return result


def report(root, *, expected_kind=None):
    root = Path(root).resolve(); plan = load_plan(root, expected_kind=expected_kind)
    settings = experiment_settings(_plan_kind(plan))
    lines = ['# ' + settings['title'], '', settings['description'],
             '请选择每组更有观点和人物感、更自然的A/B，或平、无法判断。',
             '本轮前文为新人工场景，不是恢复已删除的私人短聊。', '']
    complete = True
    for case in plan['cases']:
        lines.extend(['## ' + case['id'], '', '人工前文：', ''])
        for user, assistant in case['artificial_history']:
            lines.extend(['用户：' + user, '', '对方：' + assistant, ''])
        lines.extend(['本轮输入：', '', '\n\n'.join(case['inputs']), ''])
        for side in ('A', 'B'):
            file = root / 'positions' / case['anonymous'][side] / 'result.json'
            value = json.loads(file.read_text('utf-8')) if file.is_file() else {}
            valid = value.get('status') == 'completed' and value.get('kind') == 'real'
            complete &= valid
            lines.extend(['### ' + side, '', value.get('display', '') if valid else '真实生成未完成，不计偏好。', ''])
    output = root / '三组匿名审核.md'; output.write_text('\n'.join(lines), 'utf-8')
    atomic_json(root / 'report-status.json', {'real_pairs_complete': complete, 'pairs': 3, 'file': str(output)})
    return output


def main(*, experiment_kind='block_order'):
    experiment_settings(experiment_kind)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--approve-current-provider', action='store_true')
    p = sub.add_parser('verify'); p.add_argument('root', type=Path)
    p = sub.add_parser('report'); p.add_argument('root', type=Path)
    p = sub.add_parser('run-position'); p.add_argument('root', type=Path); p.add_argument('position_id')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = asyncio.run(prepare(approve_current_provider=args.approve_current_provider,
                                      experiment_kind=experiment_kind))
            value = {'status': 'prepared', 'root': str(root), 'real_model_calls': 0}
        elif args.action == 'verify': value = verify(args.root, expected_kind=experiment_kind)
        elif args.action == 'report': value = {'report': str(report(args.root, expected_kind=experiment_kind))}
        else:
            row = asyncio.run(run_position(args.root, args.position_id, expected_kind=experiment_kind))
            value = {key: row.get(key) for key in ('position_id', 'kind', 'status', 'physical_calls', 'elapsed_seconds', 'error_code')}
        print(json.dumps(value, ensure_ascii=False))
        if value.get('status') in ('failed', 'cancelled'):raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error_code': _error(error),
                          'preparation_reason': getattr(error, 'preparation_reason', None)}))
        raise SystemExit(1) from None


if __name__ == '__main__': main()
