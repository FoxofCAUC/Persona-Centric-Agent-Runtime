"""C2 replay through normal admission, native retry and actual request assembly."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments import companion_expression_adoption as adoption
from experiments import companion_formulaic as base
from experiments.companion_c2_materials import CASES, positions
from experiments.companion_trial import configure, configure_policy, copy_weights
from experiments.companion_comparison import local_retrieval_ready
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.companion_contract import structural_contract
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import provider_identity, safe
from persona_runtime.profile import ProfileService
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-c2-replay-v1'
NAMESPACE = 'companion-c2'
MODES = {'control': 'minimal_structural', 'candidate': 'minimal_chat'}
read = base.read
sha = base.sha


def require(condition, code):
    if not condition:
        raise RuntimeErrorBase('c2_' + code)


def source_hashes():
    result = base.source_hashes()
    for path in (Path(__file__), ROOT / 'experiments/companion_c2_materials.py',
                 ROOT / 'experiments/companion_expression_adoption.py'):
        result[path.relative_to(ROOT).as_posix()] = sha(path)
    return result


def data_root(owner):
    return safe(Path(owner['home']) / 'runtime/c2-replay', owner['home'])


def create_copy(source_home, binding_path, owner, root):
    """Use current approved visible author data without reopening a prior trial."""
    paths = RuntimePaths(Path(source_home).resolve())
    if binding_path:
        paths, _, record, metadata, _ = adoption._saved(binding_path, source_home)
        private_id = record['id']
    else:
        private_id = 'artificial-c2-fixture'
    home = safe(paths.home / 'tmp/private-acceptance' / identifier(), paths.home)
    owner.update(home=str(home), persona_id=None, private_id=private_id)
    atomic_json(root / 'trial.json', owner)
    home.mkdir(parents=True)
    marker = {'private_id': private_id, 'persona_id': None, 'formal_home': str(paths.home),
              'purpose': VERSION, 'created_at': timestamp()}
    atomic_json(home / 'private-sandbox.json', marker)
    with adoption._maintenance(paths, private_id) if binding_path else nullcontext():
        target = Store(RuntimePaths(home))
        target.initialize()
        runtime = RuntimeService(target)
        pid = runtime.create_persona(generic_persona())['id']
        runtime.use_persona(pid)
        if binding_path:
            _, source_store, record, metadata, versions = adoption._saved(binding_path, source_home)
            adoption._idle(source_store, record['persona_id'])
            visible = adoption._visible_data(versions)
            package = {'format': 'pcar_character_v1',
                       'persona_spec': {'format_version': 1, **visible['core'],
                                        'expression_style': visible['expression']['style']},
                       'character': visible['character']}
            pid = CharacterService(target).import_package(pid, package, identifier())['id']
            runtime.use_persona(pid)
            profile = ProfileService(target)
            relation = profile.current('relationship')
            profile.update('relationship', visible['relationship'], relation['id'], identifier())
            copied = adoption._versions(target, pid)
            require({k: v['data'] for k, v in copied.items()} == visible, 'approved_copy_changed')
            owner.update(author_binding=str(Path(binding_path).resolve()),
                         author_binding_hash=sha(binding_path),
                         approved_versions=metadata['after_versions'])
        with target.connection() as conn:
            counts = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('events', 'turns', 'attempts', 'memories', 'summaries')}
        require(not any(counts.values()), 'copy_not_empty')
        marker['persona_id'] = pid
        atomic_json(home / 'private-sandbox.json', marker)
        owner.update(persona_id=pid, initial_counts=counts)
        atomic_json(root / 'trial.json', owner)
    return RuntimePaths(home), pid


def load_trial(root):
    root = safe(root)
    owner = read(root / 'trial.json')
    source = Path(owner['source_home']).resolve()
    require(root.parent == source / 'tmp' / NAMESPACE, 'trial_scope')
    require(owner.get('version') == VERSION and owner.get('status') == 'prepared', 'trial_not_prepared')
    home = safe(owner['home'], source / 'tmp/private-acceptance')
    marker = read(home / 'private-sandbox.json')
    require(marker['persona_id'] == owner['persona_id'] and marker['purpose'] == VERSION
            and marker['formal_home'] == str(source), 'copy_owner_changed')
    require(datetime.now(timezone.utc) <= datetime.fromisoformat(owner['review_deadline']), 'review_expired')
    return owner


def comparable_request(request):
    """Admission IDs vary, while expression policy contents and wire stay exact."""
    value = deepcopy(request)
    expression = value['model'].get('_expression_snapshot')
    if expression is not None:
        expression = json.loads(expression) if isinstance(expression, str) else deepcopy(expression)
        expression.pop('policy_id', None)
        value['model']['_expression_snapshot'] = expression
    return value


def capture_hook(folder, expected=None, *, development=False):
    hook, captured = base.capture_hook(folder, development=development)
    original = hook.before_dispatch

    def before_dispatch(engine, turn, prepared):
        if expected is not None:
            actual = base.request_record(turn, prepared)
            require(comparable_request(actual) == comparable_request(expected), 'frozen_input_mismatch')
        original(engine, turn, prepared)

    hook.before_dispatch = before_dispatch
    return hook, captured


def validate_pairs(data, rows):
    checks = []
    for case in CASES:
        requests = [read(data / 'preflight' / (case['id'] + '-' + arm) / 'request.json')
                    for arm in MODES]
        control, candidate = requests
        before, after = control['messages'], candidate['messages']
        old, new = structural_contract(), chat_contract()
        require(before[0]['content'].endswith(old), 'old_contract_missing')
        require(after[0]['content'] == before[0]['content'][:-len(old)] + new, 'unexpected_system_change')
        require(before[1:] == after[1:], 'history_or_input_changed')
        require(control['selected_materials'] == candidate['selected_materials'], 'materials_changed')
        require({k: v for k, v in control['wire'].items() if k != 'messages'} ==
                {k: v for k, v in candidate['wire'].items() if k != 'messages'}, 'wire_parameters_changed')
        for arm, request in zip(MODES, requests):
            require(request['model']['_companion_profile'] == choose(MODES[arm]), 'profile_mismatch')
        comparable_models = [comparable_request(r)['model'] for r in requests]
        require({k: v for k, v in comparable_models[0].items() if k != '_companion_profile'} ==
                {k: v for k, v in comparable_models[1].items() if k != '_companion_profile'}, 'model_changed')
        checks.append({'case_id': case['id'], 'only_contract_changed': True,
                       'admission_policy_lineage': 'distinct_ids_same_expression_contract',
                       'selected_history_pairs': (len(before) - 2) // 2,
                       'control_hash': control['messages_hash'], 'candidate_hash': candidate['messages_hash']})
    require(len(rows) == 16 and [r['position_id'] for r in rows] ==
            [r['position_id'] for r in positions()], 'position_scope')
    return checks


def verify(root):
    owner = load_trial(root)
    data = data_root(owner)
    plan = read(data / 'plan.json')
    require(fingerprint(plan) == owner['plan_hash'], 'plan_changed')
    require(source_hashes() == plan['source_hashes'], 'source_changed')
    require(base.current_binding(RuntimePaths(Path(owner['home'])), owner['persona_id']) == plan['binding'],
            'copy_conditions_changed')
    if owner['private']:
        require(sha(owner['author_binding']) == owner['author_binding_hash'], 'author_binding_changed')
        adoption._saved(owner['author_binding'], owner['source_home'])
        require(fingerprint(ConfigStore(RuntimePaths(Path(owner['source_home']))).load()) ==
                owner['source_config_hash'], 'source_config_changed')
    require(plan['cases'] == list(CASES), 'materials_changed')
    for row in plan['positions']:
        folder = data / 'preflight' / row['position_id']
        for name, digest in row['frozen_files'].items():
            require(sha(folder / name) == digest, 'frozen_request_changed')
    validate_pairs(data, plan['positions'])
    return owner, plan


def enable(engine, pid, arm):
    ingress = IngressService(engine)
    policy = ingress.current_policy(pid, 'desktop')
    callback = getattr(engine.store, 'character_committed', None)
    # This isolated runner owns synchronous local-index preparation, as does
    # configure_policy(background_prepare=False). Avoid competing worker builds.
    engine.store.character_committed = None
    try:
        ingress.update_policy(pid, 'desktop', policy['mode'], policy['id'], identifier(),
                              companion_mode=MODES[arm], expression_policy_version=3,
                              reply_mode='natural_structured')
    finally:
        engine.store.character_committed = callback


async def history(engine, ingress, pid, case, folder):
    fixture = {'users': [pair[0] for pair in case['history']],
               'histories': {'shared': [pair[1] for pair in case['history']]}}
    return await base.seed_history(engine, ingress, pid, fixture, 'shared', folder)


async def prepare(source_home=DEFAULT_HOME, *, binding_path=None):
    source_home = Path(source_home).resolve()
    private = binding_path is not None
    require(private or source_home != DEFAULT_HOME.resolve(), 'artificial_home_required')
    require(not private or source_home == DEFAULT_HOME.resolve(), 'formal_source_scope')
    source_paths = RuntimePaths(source_home)
    source = deepcopy(ConfigStore(source_paths).load())
    source_config_hash = fingerprint(source)
    source['character_selector'].update(enabled=False, encrypted_key='')
    source['organization']['model']['encrypted_key'] = ''
    if not private:
        source['model']['encrypted_key'] = ''
    root = source_home / 'tmp' / NAMESPACE / identifier()
    root.mkdir(parents=True)
    owner = {'version': VERSION, 'status': 'preparing', 'private': private,
             'source_home': str(source_home), 'source_config_hash': source_config_hash,
             'created_at': timestamp(), 'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
    atomic_json(root / 'trial.json', owner)
    engine = None
    try:
        with base.offline():
            paths, pid = create_copy(source_home, binding_path, owner, root)
            config = configure(paths, source, pid)
            atomic_json(paths.home / 'runtime/private-copy-policy.json',
                        {'persona_id': pid, 'approved_models': {'main': provider_identity(source['model'])},
                         'approval_scope': 'user_authorized_stage4C_C2_16_positions'})
            if private:
                copy_weights(source_paths, paths)
            store = Store(paths)
            configure_policy(store, config, pid, MODES['control'], background_prepare=False)
            engine = ChatEngine(store, config, client=base.FixtureClient())
            ingress = IngressService(engine)
            require(not ingress.current_policy(pid, 'desktop')['cross_session'], 'cross_session_forbidden')
            local_retrieval_ready(engine.cognitive.characters, pid, [c['current_input'] for c in CASES],
                                  build=True, development_material=not private)
            data = data_root(owner)
            rows = positions()
            for row in rows:
                case = next(c for c in CASES if c['id'] == row['case_id'])
                folder = data / 'preflight' / row['position_id']
                folder.mkdir(parents=True)
                engine.evaluation_session = None
                enable(engine, pid, row['arm'])
                session = await history(engine, ingress, pid, case, folder)
                hook, captured = capture_hook(folder, development=not private)
                engine.evaluation_session = hook
                engine.client = base.FixtureClient()
                turn = await base.admit(engine, ingress, pid, case['current_input'])
                require(turn['status'] == 'completed' and bool(captured), 'preflight_failed')
                require(engine.client.calls[0]['wire'] == captured['request']['wire'], 'client_input_mismatch')
                atomic_json(folder / 'synthetic-target.json',
                            {'kind': 'synthetic_preflight', 'turn_id': turn['id'], 'real_calls': 0})
                base.seal_session(engine, pid, session)
                row['frozen_files'] = {name: sha(folder / name) for name in
                                      ('request.json', 'manifest.json', 'reachability.json', 'prompt-sources.json')}
            checks = validate_pairs(data, rows)
            rng = random.SystemRandom()
            mapping = {case['id']: dict(zip(('A', 'B'), rng.sample(list(MODES), 2))) for case in CASES}
            plan = {'version': VERSION, 'cases': list(CASES), 'positions': rows,
                    'order': [r['position_id'] for r in rows], 'pair_checks': checks,
                    'anonymous_mapping': mapping, 'source_hashes': source_hashes(),
                    'binding': base.current_binding(paths, pid), 'planned_positions': 16,
                    'sampling': 'one_per_arm_no_quality_retry', 'numeric_metrics': 'auxiliary_only'}
            atomic_json(data / 'plan.json', plan)
            atomic_json(data / 'state.json', {'status': 'prepared', 'automatic_resume': False})
            owner.update(status='prepared', plan_hash=fingerprint(plan))
            atomic_json(root / 'trial.json', owner)
            verify(root)
        return root
    except BaseException as error:
        owner.update(status='preparation_blocked', error_code=base.error_code(error))
        atomic_json(root / 'trial.json', owner)
        error.preparation_trial_root = str(root)
        raise
    finally:
        if engine is not None:
            await base.close_engine(engine)


async def run_position(root, position_id, *, simulated=False, client=None):
    owner = load_trial(root)
    require(simulated != owner['private'], 'simulation_scope_mismatch')
    require(simulated or client is None, 'real_client_injection_forbidden')
    lease = InstanceLock(Path(root) / 'execution.lock')
    try:
        with base.offline() if simulated else nullcontext():
            return await _run_position(root, position_id, simulated, client)
    finally:
        lease.close()


async def _run_position(root, position_id, simulated, client):
    owner, plan = verify(root)
    data = data_root(owner)
    require(read(data / 'state.json')['status'] != 'stopped' and not (data / 'cancel.json').exists(), 'batch_stopped')
    completed = []
    for prior in plan['order']:
        previous = data / 'positions' / prior
        if not (previous / 'started.json').exists():
            break
        require((previous / 'terminal.json').exists(), 'unfinished_no_auto_resume')
        require(read(previous / 'terminal.json')['status'] == 'completed', 'batch_stopped')
        completed.append(prior)
    require(position_id not in completed, 'success_already_completed')
    require(len(completed) < len(plan['order']) and position_id == plan['order'][len(completed)], 'order_mismatch')
    row = next(r for r in plan['positions'] if r['position_id'] == position_id)
    case = next(c for c in plan['cases'] if c['id'] == row['case_id'])
    folder = data / 'positions' / position_id
    folder.mkdir(parents=True, exist_ok=True)
    kind = 'simulated' if simulated else 'real'
    with (folder / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump({'kind': kind, 'at': timestamp()}, stream)
    result = {k: row[k] for k in ('position_id', 'case_id', 'arm')}
    result.update(kind=kind, status='running')
    atomic_json(folder / 'result.json', result)
    engine = watcher = None
    audit = {}
    dispatches = 0
    started = time.perf_counter()
    try:
        paths = RuntimePaths(Path(owner['home']))
        engine = ChatEngine(Store(paths), ConfigStore(paths), client=base.FixtureClient())
        ingress = IngressService(engine)
        enable(engine, owner['persona_id'], row['arm'])
        local_retrieval_ready(engine.cognitive.characters, owner['persona_id'], [case['current_input']],
                              build=False, development_material=simulated)
        session = await history(engine, ingress, owner['persona_id'], case, folder)
        expected = read(data / 'preflight' / position_id / 'request.json')
        hook, _ = capture_hook(folder, expected, development=simulated)
        engine.evaluation_session = hook

        def save(value):
            nonlocal audit
            audit = deepcopy(value)
            for attempt in audit.get('generation_retry', {}).get('attempts', []):
                reserve = folder / (attempt['id'] + '.reserve.json')
                if not reserve.exists():
                    require(attempt['status'] == 'running', 'reservation_missing')
                    with reserve.open('x', encoding='utf-8') as stream:
                        json.dump({'kind': kind, 'attempt': attempt}, stream)
                atomic_json(folder / (attempt['id'] + '.attempt.json'), attempt)
            atomic_json(folder / 'audit.json', audit)

        engine.generation_audit_sink = save
        target = client or (base.FixtureClient() if simulated else ChatCompletionsClient())

        class CheckedClient:
            async def complete(self, model, messages):
                nonlocal dispatches
                verify(root)
                if (data / 'cancel.json').exists():
                    raise asyncio.CancelledError()
                wire = build_request(model, messages)[0]
                require(messages == expected['messages'] and wire == expected['wire'], 'dispatch_changed')
                attempts = audit.get('generation_retry', {}).get('attempts', [])
                require(bool(attempts), 'audit_missing')
                attempt_id = attempts[-1]['id']
                atomic_json(folder / (attempt_id + '.dispatch.json'),
                            {'kind': kind, 'attempt_id': attempt_id, 'at': timestamp(), 'wire_hash': fingerprint(wire)})
                dispatches += 1
                value = await target.complete(model, messages)
                atomic_json(folder / (attempt_id + '.response.json'),
                            {'raw': value[0], 'usage': value[1], 'diagnostics': getattr(value, 'diagnostics', {})})
                return value

        engine.client = CheckedClient()

        async def cancel_watch():
            while True:
                if (data / 'cancel.json').exists():
                    for turn in engine.requests():
                        if turn['status'] in ('queued', 'running'):
                            await engine.cancel(turn['id'])
                    return
                await asyncio.sleep(.05)

        watcher = asyncio.create_task(cancel_watch())
        turn = await base.admit(engine, ingress, owner['persona_id'], case['current_input'])
        result.update(turn_id=turn['id'], session_id=session, status=turn['status'], error_code=turn.get('error_code'),
                      reply=turn.get('reply'), display=turn.get('display_reply'), display_messages=turn.get('display_messages'),
                      context=turn.get('context'))
        require(turn['status'] == 'completed', turn.get('error_code') or 'target_failed')
        base.assert_runtime(engine, owner['persona_id'], {'manifest': read(folder / 'manifest.json')}, development=simulated)
        result.update(messages_hash=expected['messages_hash'], wire_hash=expected['wire_hash'])
        base.seal_session(engine, owner['persona_id'], session)
    except BaseException as error:
        cancelled = isinstance(error, asyncio.CancelledError) or (data / 'cancel.json').exists()
        result.update(status='cancelled' if cancelled else 'failed', error_code=base.error_code(error),
                      preparation_reason=getattr(error, 'preparation_reason', None))
        atomic_json(data / 'state.json', {'status': 'stopped', 'position_id': position_id,
                                        'error_code': result['error_code'], 'automatic_resume': False})
    finally:
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        if engine is not None:
            await base.close_engine(engine)
    result.update(usage=audit, generation_attempts=len(audit.get('generation_retry', {}).get('attempts', [])),
                  physical_calls=0 if simulated else dispatches, simulated_calls=dispatches if simulated else 0,
                  elapsed_seconds=time.perf_counter() - started)
    atomic_json(folder / 'result.json', result)
    atomic_json(folder / 'terminal.json', {k: result.get(k) for k in
                ('position_id', 'kind', 'status', 'error_code', 'physical_calls', 'simulated_calls')})
    return result


async def execute(root, *, simulated=False, client=None):
    owner, plan = verify(root)
    require(not any((data_root(owner) / 'positions').glob('*/started.json')), 'batch_started_no_auto_resume')
    for position_id in plan['order']:
        result = await run_position(root, position_id, simulated=simulated, client=client)
        print(json.dumps({k: result.get(k) for k in ('position_id', 'status', 'physical_calls', 'simulated_calls')},
                         ensure_ascii=False), flush=True)
        if result['status'] != 'completed':
            break
    return report(root)


def report(root):
    owner = load_trial(root)
    data = data_root(owner)
    plan = read(data / 'plan.json')
    rows = []
    for position in plan['positions']:
        folder = data / 'positions' / position['position_id']
        if (folder / 'terminal.json').exists():
            rows.append(read(folder / 'result.json'))
        else:
            started = (folder / 'started.json').exists()
            kind = read(folder / 'started.json')['kind'] if started else None
            rows.append({**position, 'status': 'unfinished' if started else 'not_executed', 'kind': kind,
                         'physical_calls': len(list(folder.glob('*.dispatch.json'))) if kind == 'real' else 0,
                         'simulated_calls': len(list(folder.glob('*.dispatch.json'))) if kind == 'simulated' else 0})
    summary = {'version': VERSION, 'planned_positions': 16, 'completed': sum(r['status'] == 'completed' for r in rows),
               'physical_calls': sum(r['physical_calls'] for r in rows),
               'simulated_calls': sum(r['simulated_calls'] for r in rows),
               'unfinished_positions': sum(r['status'] == 'unfinished' for r in rows),
               'user_review': 'pending', 'real_effect': 'not_settled'}
    atomic_json(data / 'report.json', {**summary, 'rows': rows})
    by_id = {r['position_id']: r for r in rows}
    lines = ['# 八组匿名合同对照', '', '每组采用相同人物及人工前文。请选更愿意继续交流的一条，'
             '可回复A、B、平或无法判断，并可标出具体出戏处。数字指标作辅助观察。', '',
             '复述、确认和追问是否自然，请结合当下语境判断。认真任务同时看完整性与可用性。', '']
    for index, case in enumerate(plan['cases'], 1):
        lines.extend([f'## {index:02d}', '', '人工前文', ''])
        for user, assistant in case['history']:
            lines.extend(['用户　' + user, '', '人物　' + assistant, ''])
        lines.extend(['本轮输入', '', case['current_input'], ''])
        for label, arm in plan['anonymous_mapping'][case['id']].items():
            row = by_id[case['id'] + '-' + arm]
            lines.extend(['### ' + label, ''])
            if row['status'] == 'completed':
                lines.extend([row.get('display') or row.get('reply') or '正文不可用', ''])
            else:
                lines.extend(['本位置未完成，无法比较。', ''])
        lines.extend(['选择　　出戏处或原因（可选）', ''])
    (data / '八组匿名审核.md').write_text('\n'.join(lines), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'run', 'report', 'verify'))
    parser.add_argument('trial', nargs='?', type=Path)
    parser.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--binding', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = asyncio.run(prepare(args.source_home, binding_path=args.binding))
            result = {'trial': str(root), 'status': 'prepared', 'physical_calls': 0}
        elif args.action in ('run', 'simulate'):
            result = asyncio.run(execute(args.trial, simulated=args.action == 'simulate'))
        elif args.action == 'report':
            result = report(args.trial)
        else:
            verify(args.trial)
            result = {'status': 'verified', 'physical_calls': 0}
        print(json.dumps(result, ensure_ascii=False))
    except BaseException as error:
        print(json.dumps({'status': 'blocked', 'error_code': base.error_code(error),
                          'trial': getattr(error, 'preparation_trial_root', None)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
