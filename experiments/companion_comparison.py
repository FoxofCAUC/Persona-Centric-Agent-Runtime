"""One disposable, private six-pair continuation comparison.

The preceding trial is read-only. Every new position owns a complete prefix
copy and is admitted normally; earlier completed positions are never replayed.
Private text stays under the trial's temporary comparison directory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_trial import load_trial, configure, configure_policy, copy_weights
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store

VERSION = 'companion-six-pair-v1'
CONTINUATIONS = (
    ('sharing', ['刚才出门买东西，回来才发现一路攥着购物清单，却忘了买清单上的东西。']),
    ('wish_change', ['我想把明天安排得充实一点，你帮我想想怎么安排。',
                     '算了，先不安排了。我现在只是想随便聊聊，不想再列计划。']),
    ('continuity', ['接着刚才的话题聊吧。你自己最在意哪一点？']),
)
SECRET_FIELDS = frozenset(('encrypted_key', 'api_key', 'admin_token', 'bridge_token', 'authorization'))


def public_model(value):
    if isinstance(value, dict):
        return {key: public_model(item) for key, item in value.items() if key.lower() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [public_model(item) for item in value]
    return deepcopy(value)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _public_config(config):
    return public_model({key: config[key] for key in ('model', 'character_selector')})


def position(plan, position_id):
    rows = [row for row in plan['positions'] if row['id'] == position_id]
    if len(rows) != 1:
        raise RuntimeErrorBase('comparison_position_invalid')
    return rows[0]


def load_plan(root):
    root = Path(root).resolve()
    plan = json.loads((root / 'plan.json').read_text(encoding='utf-8'))
    safe(root, Path(plan['source']['trial']) / 'comparison')
    if plan.get('version') != VERSION or plan.get('status') != 'prepared':
        raise RuntimeErrorBase('comparison_not_prepared')
    expected = json.loads((root / 'freeze.json').read_text(encoding='utf-8'))
    if fingerprint(plan) != expected['plan_hash']:
        raise RuntimeErrorBase('comparison_plan_changed')
    return plan


def verify_frozen(root, plan):
    fresh = load_plan(root)
    if fresh != plan:
        raise RuntimeErrorBase('comparison_plan_changed')
    source = plan['source']
    trial = load_trial(source['trial'])
    if trial['review_deadline'] != plan['review_deadline']:
        raise RuntimeErrorBase('comparison_trial_changed')
    db = Path(source['home']) / 'data/database/runtime.sqlite3'
    if _sha(db) != source['database_sha256']:
        raise RuntimeErrorBase('comparison_prefix_changed')
    config = ConfigStore(RuntimePaths(Path(source['home']))).load()
    if fingerprint(_public_config(config)) != source['public_config_hash']:
        raise RuntimeErrorBase('comparison_provider_changed')
    for name, expected in plan['source_hashes'].items():
        if _sha(ROOT / name) != expected:
            raise RuntimeErrorBase('comparison_implementation_changed')
    for row in plan['positions']:
        home = safe(row['home'], Path(trial['source_home']) / 'tmp/private-acceptance')
        if _sha(home / 'runtime/private-copy-policy.json') != row['private_policy_sha256']:
            raise RuntimeErrorBase('comparison_private_policy_changed')
        if _sha(home / 'private-sandbox.json') != row['owner_sha256']:
            raise RuntimeErrorBase('comparison_owner_changed')
        if fingerprint(_public_config(ConfigStore(RuntimePaths(home)).load())) != source['public_config_hash']:
            raise RuntimeErrorBase('comparison_position_provider_changed')


def _states(conn):
    return {table: dict(conn.execute('SELECT ' + field + ',count(*) FROM ' + table + ' GROUP BY ' + field))
            for table, field in (('turns', 'status'), ('input_batches', 'state'), ('quota_reservations', 'state'))}


def _ensure_terminal(conn):
    states = _states(conn)
    if any(states['turns'].get(key, 0) for key in ('queued', 'running')):
        raise RuntimeErrorBase('comparison_prefix_has_pending_turn')
    if any(states['input_batches'].get(key, 0) for key in ('collecting', 'sealed_waiting_capacity', 'interrupted_buffer')):
        raise RuntimeErrorBase('comparison_prefix_has_pending_input')
    if states['quota_reservations'].get('active', 0):
        raise RuntimeErrorBase('comparison_prefix_has_active_reservation')
    return states


def local_retrieval_ready(runtime, persona_id, inputs, *, build=False, development_material=False):
    """Synchronously prepare/read/warm the existing local index, never a worker.

    The real provider already requires verified local weights and uses
    local_files_only. A missing/busy dependency blocks this experiment instead
    of turning a lexical fallback into a representative hybrid comparison.
    """
    if development_material:
        return {'status': 'fixture_bypass', 'representative_hybrid': False}
    try:
        version, policy = runtime._scope(persona_id, None, 'desktop')
        index = runtime.index(policy['config'])
        diagnostic = index.provider.diagnose()
        if diagnostic.get('status') != 'ready':
            raise RuntimeErrorBase(diagnostic.get('reason') or 'embedding_unavailable')
        if build:
            index.build(persona_id, version['id'])
        status = runtime.status(persona_id, version['id'])
        if status.get('cache', {}).get('state') != 'ready':
            raise RuntimeErrorBase(status.get('cache', {}).get('reason') or 'character_cache_missing')
        pool = runtime.local_pool(version, policy, inputs)
        return {'status': 'ready', 'representative_hybrid': True,
                'version_id': version['id'], 'model_fingerprint': pool['model_fingerprint'],
                'index_fingerprint': pool['index_fingerprint']}
    except (RuntimeErrorBase, OSError, ValueError, KeyError) as cause:
        error = RuntimeErrorBase('comparison_local_preparation_blocked')
        error.preparation_reason = _error(cause)
        raise error from None


def prepare(trial_path, *, expected_source_turns=13, development_material=False):
    trial_path = Path(trial_path).resolve()
    trial = load_trial(trial_path)
    if not trial.get('private'):
        raise RuntimeErrorBase('comparison_private_trial_required')
    source_arm = trial['arms']['A']
    source_home = safe(source_arm['home'], Path(trial['source_home']) / 'tmp/private-acceptance')
    source_paths = RuntimePaths(source_home)
    source_db = source_home / 'data/database/runtime.sqlite3'
    conn = sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True)
    try:
        states = _ensure_terminal(conn)
        if states['turns'].get('completed', 0) != expected_source_turns:
            raise RuntimeErrorBase('comparison_prefix_count_changed')
    finally:
        conn.close()
    root = safe(trial_path / 'comparison' / identifier(), trial_path)
    root.mkdir(parents=True)
    source_config = ConfigStore(source_paths).load()
    plan = {'version': VERSION, 'status': 'preparing', 'created_at': timestamp(), 'private': True,
            'development_material': bool(development_material),
            'review_deadline': trial['review_deadline'],
            'input_kind': 'new artificial continuations over the complete shared A prefix; not original-message replay',
            'source': {'trial': str(trial_path), 'home': str(source_home),
                       'persona_id': source_arm['persona_id'], 'database_sha256': _sha(source_db),
                       'public_config_hash': fingerprint(_public_config(source_config)), 'states': states},
            'pairs': [], 'positions': [], 'cleanup_homes': [], 'source_hashes': {}}
    atomic_json(root / 'plan.json', plan)
    owned_parent = safe(Path(trial['source_home']) / 'tmp/private-acceptance')
    owned_parent.mkdir(parents=True, exist_ok=True)
    source_marker = json.loads((source_home / 'private-sandbox.json').read_text(encoding='utf-8'))
    policy = json.loads((source_home / 'runtime/private-copy-policy.json').read_text(encoding='utf-8'))
    for index in range(6):
        category, inputs = CONTINUATIONS[index % 3]
        pair_id = 'p%02d' % (index + 1)
        modes = ('current', 'minimal') if index < 3 else ('minimal', 'reference')
        ordered = list(modes)
        random.SystemRandom().shuffle(ordered)
        pair = {'id': pair_id, 'category': category, 'inputs': inputs,
                'positions': [pair_id + '-' + mode for mode in modes],
                'anonymous': {'A': pair_id + '-' + ordered[0], 'B': pair_id + '-' + ordered[1]}}
        plan['pairs'].append(pair)
        for mode in modes:
            home = safe(owned_parent / identifier(), owned_parent)
            paths = RuntimePaths(home)
            paths.initialize()
            plan['cleanup_homes'].append(str(home))
            row = {'id': pair_id + '-' + mode, 'pair_id': pair_id, 'mode': mode,
                   'home': str(home), 'persona_id': source_arm['persona_id'], 'inputs': inputs,
                   'source_position_id': pair_id + '-minimal' if mode == 'reference' else None,
                   'paired_pcar_position': pair_id + '-minimal' if mode == 'reference' else None}
            plan['positions'].append(row)
            atomic_json(root / 'plan.json', plan)
            atomic_json(home / 'private-sandbox.json', source_marker)
            atomic_json(home / 'runtime/private-copy-policy.json', policy)
            src = sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True)
            dst = sqlite3.connect(home / 'data/database/runtime.sqlite3')
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            configure(paths, source_config, source_arm['persona_id'])
            configure_policy(Store(paths), ConfigStore(paths), source_arm['persona_id'],
                             mode if mode != 'reference' else 'minimal', background_prepare=False)
            from persona_runtime.character_runtime import CharacterRuntime
            runtime = CharacterRuntime(Store(paths))
            try:
                copy_weights(source_paths, paths)
                row['local_preparation'] = local_retrieval_ready(runtime, source_arm['persona_id'], inputs,
                    build=True, development_material=development_material)
            except (RuntimeErrorBase, OSError) as error:
                reason = getattr(error, 'preparation_reason', _error(error))
                row['local_preparation'] = {'status': 'blocked', 'reason': reason}
                plan['status'] = 'preparation_blocked'
                atomic_json(root / 'plan.json', plan)
                atomic_json(root / 'state.json', {'status': 'preparation_blocked', 'position_id': row['id'],
                                                 'reason': reason, 'automatic_resume': False})
                raise RuntimeErrorBase('comparison_local_preparation_blocked') from None
            finally:
                runtime.close()
            row['initial_database_sha256'] = _sha(home / 'data/database/runtime.sqlite3')
            row['private_policy_sha256'] = _sha(home / 'runtime/private-copy-policy.json')
            row['owner_sha256'] = _sha(home / 'private-sandbox.json')
    relevant = [p for p in (ROOT / 'persona_runtime').glob('*.py')]
    relevant += [Path(__file__), ROOT / 'experiments/companion_trial.py', ROOT / 'experiments/companion_reference.py']
    if (ROOT / 'experiments/companion_private_reference.py').is_file():
        relevant.append(ROOT / 'experiments/companion_private_reference.py')
    plan['source_hashes'] = {str(p.relative_to(ROOT)).replace('\\', '/'): _sha(p) for p in relevant}
    plan['status'] = 'prepared'
    atomic_json(root / 'plan.json', plan)
    atomic_json(root / 'freeze.json', {'version': VERSION, 'plan_hash': fingerprint(plan)})
    atomic_json(root / 'state.json', {'status': 'prepared', 'automatic_resume': False})
    verify_frozen(root, plan)
    return root


class FakeClient:
    async def complete(self, model, messages):
        if model.get('_character_selection'):
            return ModelCompletion('{"schema_version":1,"lore_ids":[],"example_ids":[]}', None,
                                   {'finish_reason': 'stop'})
        return ModelCompletion(json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': '这是独立人工夹具，不代表真实聊天效果。', 'content_kind': 'chat', 'cancel_if_new_input': False}]},
            ensure_ascii=False), None, {'finish_reason': 'stop'})


def _error(error):
    value = str(error)
    return value if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', value) else type(error).__name__


def ensure_no_unfinished_position(root):
    root = Path(root)
    for started in (root / 'positions').glob('*/started.json'):
        terminal = started.with_name('terminal.json')
        if not terminal.is_file():
            raise RuntimeErrorBase('comparison_unfinished_position_no_auto_resume')
        data = json.loads(terminal.read_text(encoding='utf-8'))
        if data.get('status') != 'completed':
            raise RuntimeErrorBase('comparison_batch_stopped')


class CapturingClient:
    def __init__(self, delegate, root, plan, output, *, store=None, representative=False):
        self.delegate, self.root, self.plan, self.output = delegate, root, plan, output
        self.calls = []
        self.store, self.representative = store, representative
        self.preparation_blocked_reason = None

    def _check_actual_retrieval(self):
        with self.store.connection() as conn:
            row = conn.execute("SELECT manifest_json FROM context_manifests WHERE task_type='dialogue' ORDER BY rowid DESC LIMIT 1").fetchone()
        retrieval = json.loads(row[0]).get('character_retrieval', {}) if row else {}
        if (retrieval.get('effective_mode') not in ('hybrid', 'hybrid_llm')
                or retrieval.get('degrade_reason')):
            error = RuntimeErrorBase('comparison_local_preparation_blocked')
            error.preparation_reason = retrieval.get('degrade_reason') or 'actual_hybrid_not_ready'
            self.preparation_blocked_reason = error.preparation_reason
            raise error

    async def complete(self, model, messages):
        verify_frozen(self.root, self.plan)
        kind = 'selection' if model.get('_character_selection') else 'main'
        if self.representative and kind == 'main':
            self._check_actual_retrieval()
        wire, transport = build_request(model, messages)
        capture = {'kind': kind, 'messages': messages, 'model': public_model(model),
                   'wire': wire, 'transport': transport, 'wire_hash': fingerprint(wire),
                   'messages_hash': fingerprint(messages)}
        self.calls.append(capture)
        atomic_json(self.output / ('dispatch-%03d.json' % len(self.calls)), capture)
        if kind == 'main':
            request_path = self.output / 'request.json'
            if request_path.exists() and json.loads(request_path.read_text(encoding='utf-8'))['wire_hash'] != capture['wire_hash']:
                raise RuntimeErrorBase('comparison_retry_input_changed')
            atomic_json(request_path, capture)
        return await self.delegate.complete(model, messages)


async def run_pcar(root, position_id, fake=False):
    from persona_runtime.instance import InstanceLock
    lease = InstanceLock(Path(root).resolve() / 'comparison-execution.lock')
    try:
        return await _run_pcar_locked(root, position_id, fake=fake)
    finally:
        lease.close()


async def _run_pcar_locked(root, position_id, fake=False):
    root = Path(root).resolve()
    plan = load_plan(root)
    verify_frozen(root, plan)
    row = position(plan, position_id)
    if row['mode'] not in ('current', 'minimal'):
        raise RuntimeErrorBase('comparison_not_pcar_position')
    if bool(fake) != bool(plan.get('development_material')):
        raise RuntimeErrorBase('comparison_simulation_scope_mismatch')
    state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
    if state['status'] == 'stopped':
        raise RuntimeErrorBase('comparison_batch_stopped')
    ensure_no_unfinished_position(root)
    output = root / 'positions' / position_id
    output.mkdir(parents=True, exist_ok=True)
    kind = 'simulated' if fake else 'real'
    with (output / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump({'kind': kind, 'started_at': timestamp(), 'position_id': position_id}, stream)
    result = {'kind': kind, 'position_id': position_id, 'status': 'reserved', 'source_prefix_unchanged': True}
    atomic_json(output / 'result.json', result)
    started = time.perf_counter()
    engine = None
    ingress = None
    capture = None
    try:
        paths = RuntimePaths(Path(row['home']))
        config = ConfigStore(paths)
        if fingerprint(_public_config(config.load())) != plan['source']['public_config_hash']:
            raise RuntimeErrorBase('comparison_position_provider_changed')
        if _sha(paths.home / 'data/database/runtime.sqlite3') != row['initial_database_sha256']:
            raise RuntimeErrorBase('comparison_initial_prefix_changed')
        capture = CapturingClient(FakeClient() if fake else ChatCompletionsClient(), root, plan, output,
                                  store=Store(paths), representative=not fake)
        engine = ChatEngine(Store(paths), config, client=capture)
        ingress = IngressService(engine)
        result['local_preparation'] = local_retrieval_ready(engine.cognitive.characters, row['persona_id'], row['inputs'],
            development_material=bool(plan.get('development_material')))
        accepted = [ingress.accept({'channel': 'desktop', 'text': text, 'request_id': identifier(),
                                   'persona_id': row['persona_id']}) for text in row['inputs']]
        if len({value['batch_id'] for value in accepted}) != 1:
            raise RuntimeErrorBase('comparison_members_split')
        batch = ingress.get(accepted[0]['batch_id'])
        ingress.action(batch['id'], 'seal', batch['revision'], identifier(), row['persona_id'])
        await engine.wait()
        batch = ingress.get(batch['id'])
        turn = engine.get(batch['turn_id'])
        result.update(turn_id=turn['id'], status=turn['status'], error_code=turn.get('error_code'),
                      reply=turn.get('reply'), display=turn.get('display_reply'),
                      elapsed_seconds=time.perf_counter() - started)
        with engine.store.connection() as conn:
            manifests = [json.loads(item[0]) for item in conn.execute(
                "SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid", (turn['id'],))]
            audits = {table: [dict(item) for item in conn.execute(
                'SELECT status,error_code,usage_json FROM ' + table + ' WHERE turn_id=?', (turn['id'],))]
                      for table in ('attempts', 'character_selection_attempts', 'situation_observation_attempts')}
            _ensure_terminal(conn)
        atomic_json(output / 'manifests.json', manifests)
        atomic_json(output / 'audit.json', audits)
        mains = [item for item in capture.calls if item['kind'] == 'main']
        result['messages_hash'] = mains[-1]['messages_hash'] if mains else None
        result['physical_calls'] = len(capture.calls) if not fake else 0
        result['simulated_calls'] = len(capture.calls) if fake else 0
        if turn['status'] != 'completed':
            raise RuntimeErrorBase(turn.get('error_code') or 'comparison_turn_failed')
        if not mains or not manifests or manifests[-1]['input_snapshot_hash'] != mains[-1]['messages_hash']:
            raise RuntimeErrorBase('comparison_manifest_input_mismatch')
        result['manifest_matches_actual_input'] = True
    except BaseException as error:
        result.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed',
                      error_code=_error(error), elapsed_seconds=time.perf_counter() - started,
                      physical_calls=0 if fake or capture is None else len(capture.calls),
                      simulated_calls=len(capture.calls) if fake and capture is not None else 0)
        if result['error_code'] == 'comparison_local_preparation_blocked':
            result['status'] = 'preparation_blocked'
            result['preparation_reason'] = getattr(error, 'preparation_reason', None) or (
                capture.preparation_blocked_reason if capture is not None else None) or 'actual_hybrid_not_ready'
        atomic_json(root / 'state.json', {'status': 'stopped', 'position_id': position_id,
                                         'error_code': result['error_code'], 'automatic_resume': False})
    finally:
        if engine is not None:
            if ingress is not None:
                for batch in ingress.list(row['persona_id']):
                    if batch['state'] in ('collecting', 'sealed_waiting_capacity', 'interrupted_buffer'):
                        ingress.action(batch['id'], 'cancel', batch['revision'], identifier(), row['persona_id'])
            for turn in engine.requests():
                if turn['status'] in ('queued', 'running'):
                    await engine.cancel(turn['id'])
            await engine.wait()
            engine.cognitive.characters.close()
            engine.cognitive.shadow.close()
        atomic_json(output / 'result.json', result)
        atomic_json(output / 'terminal.json', {key: value for key, value in result.items()
            if key not in ('reply', 'display')})
    return result


def report(root):
    root = Path(root).resolve()
    plan = load_plan(root)
    lines = ['# 六组匿名续接对照', '', '共享A试聊的完整前文，使用事前冻结的新人工续接输入；不是原句复现。',
             '请按交流感觉、人物感及是否想继续聊选择A、B、平或无法判断；不按长短判断。',
             '当前与最简比较同时包含编译和选材变化，不能直接归因为单个组件。',
             '参考使用真实文本装配/agent/钩子，并在钩子后用共同schema3解码显示；本轮不比较其原生分泡。', '']
    complete = True
    comparisons = []
    for pair in plan['pairs']:
        requests = []
        for pid in pair['positions']:
            request = root / 'positions' / pid / 'request.json'
            requests.append(json.loads(request.read_text(encoding='utf-8')) if request.is_file() else None)
        histories = [item['messages'][1:-1] if item else None for item in requests]
        currents = [item['messages'][-1] if item else None for item in requests]
        parameters = [{key: value for key, value in item['wire'].items() if key != 'messages'} if item else None for item in requests]
        matched = histories[0] is not None and histories[0] == histories[1]
        comparisons.append({'pair_id': pair['id'], 'history_hashes': [fingerprint(item) if item is not None else None for item in histories],
                            'actual_history_equal': matched,
                            'current_input_hashes': [fingerprint(item) if item is not None else None for item in currents],
                            'current_inputs_equal': currents[0] is not None and currents[0] == currents[1],
                            'public_wire_parameters_equal': parameters[0] is not None and parameters[0] == parameters[1],
                            'full_request_equal': requests[0] is not None and requests[1] is not None and requests[0]['wire'] == requests[1]['wire'],
                            'limitation': 'compiler and selection vary together' if pair['id'] in ('p01','p02','p03') else
                                          'upstream assembly/hooks and shared schema3 display adapter; native segmentation not compared'})
        lines.extend(['## ' + pair['id'], '', '本轮输入：', '', '\n\n'.join(pair['inputs']), ''])
        lines.extend(['实际入模普通前文' + ('一致。' if matched else '不一致或未采集；不能称为相同最终历史。'), ''])
        for label in ('A', 'B'):
            path = root / 'positions' / pair['anonymous'][label] / 'result.json'
            result = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            if result.get('status') != 'completed' or result.get('kind') != 'real':
                complete = False
                text = '未完成真实生成，不计偏好。'
            else:
                text = result.get('display') or result.get('reply') or '无可展示正文。'
            lines.extend(['### ' + label, '', text, ''])
    target = root / '匿名六组审核.md'
    target.write_text('\n'.join(lines), encoding='utf-8')
    atomic_json(root / 'report-status.json', {'real_pairs_complete': complete, 'file': str(target)})
    atomic_json(root / 'comparison-summary.json', {'pairs': comparisons, 'source_prefix_equal': True,
                                                 'source_prefix_is_not_effective_history': True})
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare'); p.add_argument('trial', type=Path)
    p = sub.add_parser('run-pcar'); p.add_argument('root', type=Path); p.add_argument('position_id'); p.add_argument('--fake', action='store_true')
    p = sub.add_parser('report'); p.add_argument('root', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps({'root': str(prepare(args.trial)), 'status': 'prepared'}))
    elif args.action == 'report':
        print(json.dumps({'report': str(report(args.root))}, ensure_ascii=False))
    else:
        if args.fake:
            from unittest.mock import patch
            guard = patch('socket.socket.connect', side_effect=RuntimeError('comparison_fake_network_blocked'))
        else:
            guard = nullcontext()
        with guard:
            result = asyncio.run(run_pcar(args.root, args.position_id, fake=args.fake))
        print(json.dumps({key: value for key, value in result.items() if key not in ('reply', 'display')}, ensure_ascii=False))
        raise SystemExit(0 if result['status'] == 'completed' else 1)


if __name__ == '__main__':
    main()
