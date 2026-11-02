"""CR0 offline contracts and isolated pipeline. There is no real execution action."""
import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import encoded, fingerprint, read_exact, turn_scope
from persona_runtime.chat import ChatEngine
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, Source, Statement, consumer_input, require
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input
from tests.dialogue_refactor_fixture import fixture
from tests.dialogue_refactor_responses import answer

HOME = Path('D:/Persona-Centric Agent Runtime')
OUT = HOME / 'exports/acceptance/PCAR-dialogue-refactor/CR0-contracts'
VERSION = 'cr0-contracts-v1'
PATHS = ('legacy', 'frame', 'spec')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def offline():
    audit = {'external_attempts': 0, 'http_attempts': 0, 'loopback_connections': 0, 'real_model_calls': 0, 'qq_sends': 0}

    def deny(*args, **kwargs):
        audit['external_attempts'] += 1
        raise RuntimeError('cr0_network_forbidden')

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded(original, sock, address):
        # Windows asyncio creates a loopback socketpair for its event loop.
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError):
            allowed = False
        if not allowed:
            return deny()
        audit['loopback_connections'] += 1
        return original(sock, address)

    async def no_http(*args, **kwargs):
        audit['http_attempts'] += 1
        raise RuntimeError('cr0_http_forbidden')

    with patch('socket.socket.connect', lambda s, a: guarded(connect, s, a)), \
            patch('socket.socket.connect_ex', lambda s, a: guarded(connect_ex, s, a)), \
            patch('socket.create_connection', deny), patch('socket.getaddrinfo', deny), \
            patch('socket.gethostbyname', deny), patch('socket.gethostbyname_ex', deny), \
            patch('socket.socket.sendto', deny), \
            patch('aiohttp.ClientSession._request', no_http), \
            patch('persona_runtime.chat.ChatCompletionsClient.complete', no_http):
        yield audit


class NoDispatch:
    async def complete(self, *args, **kwargs):
        raise RuntimeError('cr0_real_client_forbidden')


def close(engine):
    engine.cognitive.shadow.close()
    engine.cognitive.characters.close()


async def create_case(folder, case, role):
    """Create fresh artificial authority, then use normal versioned admission."""
    safe(folder, HOME / 'tmp' if folder.is_relative_to(HOME / 'tmp') else OUT)
    require(not folder.exists(), 'cr0_runtime_exists')
    paths = RuntimePaths(folder)
    store = Store(paths)
    store.initialize()
    cfg = ConfigStore(paths)
    cfg.initialize()
    cfg.update({'model': {'base_url': 'https://example.invalid/v1', 'name': 'grok-4.7',
                          'max_tokens': 8192, 'max_input_chars': 48000, 'timeout': 180}})
    from persona_runtime.dialogue_voice import freeze
    settings = cfg.load()
    settings['model']['_dialogue_profile'] = freeze('cr0-' + case['id'], case['members'], version='natural-dialogue-v2')
    settings['organization']['daily_enabled'] = False
    atomic_json(cfg.file, settings)
    service = RuntimeService(store)
    spec = generic_persona().to_dict()
    spec['name'] = '岚舟'
    spec['expression_style'] = fixture()['roles'][role]['style']
    from persona_runtime.models import PersonaSpec
    pid = service.create_persona(PersonaSpec(**spec))['id']
    service.use_persona(pid)
    sid = service.create_session()['id']
    from tests.m71_fixtures import seed_turn
    with store.connection(write=True) as db:
        for index, (text, reply) in enumerate(case['history']):
            seed_turn(db, pid, sid, 'cr0-history-' + str(index), [text], reply=reply)
        from persona_runtime.profile import append_version
        append_version(db, pid, 'state', {'text': '人工显式状态：今天专心做手工。', 'expires_at': '2099-01-01T00:00:00+00:00'})
    if case['history']:
        service.add_memory('user_fact', case['history'][0][0], 'cr0-history-0-e1')
    engine = ChatEngine(store, cfg, NoDispatch())
    store.character_committed = lambda *_: []
    from persona_runtime.character import CharacterService, empty_character
    characters = CharacterService(store)
    data = empty_character()
    data['identity']['species'] = '人工角色'
    data['examples'] = [{'id': 'cr0-e1', 'enabled': True, 'scenario': '日常交流',
                         'keywords': ['皮影', '招牌', '时候', '木条', '封面', '循环'],
                         'text': fixture()['roles'][role]['stance']}]
    characters.update(pid, data, characters.current(pid)['id'], identifier())
    ingress = IngressService(engine)
    old = ingress.current_policy(pid, 'desktop')
    ingress.update_policy(pid, 'desktop', 'natural_batch', old['id'], identifier(),
                          reply_mode='natural_structured', context_mode='working_set', cross_session=False,
                          character_retrieval_mode='lexical', disposition_mode='baseline',
                          situation_observation_mode='disabled', expression_policy_version=3)
    try:
        with patch.object(engine, '_schedule'):
            receipt = None
            for index, text in enumerate(case['members']):
                value = {'channel': 'desktop', 'persona_id': pid, 'session_id': sid, 'text': text, 'request_id': identifier()}
                if case.get('reference') and index == 0:
                    value['reference'] = {'kind': 'event', 'id': 'cr0-history-0-e1'}
                receipt = ingress.accept(value)
            batch = ingress.get(receipt['batch_id'])
            admitted = ingress.action(batch['id'], 'seal', batch['revision'], identifier(), pid)
        atomic_json(folder / 'artificial-cr0.json', {'tool': VERSION, 'private': False, 'persona_id': pid})
        return engine, admitted['turn_id']
    except BaseException:
        close(engine)
        raise


def raw_turn(engine, tid):
    with engine.store.connection() as db:
        return dict(db.execute('SELECT * FROM turns WHERE id=?', (tid,)).fetchone())


def collect(engine, tid):
    """Reuse effective legacy selection and exact source checks, in isolation only."""
    require((engine.store.paths.home / 'artificial-cr0.json').is_file(), 'cr0_isolation_required')
    turn = raw_turn(engine, tid)
    marker = read(engine.store.paths.home / 'artificial-cr0.json')
    require(marker == {'tool': VERSION, 'private': False, 'persona_id': turn['persona_id']}, 'cr0_isolation_required')
    require(turn['status'] == 'queued' and not turn.get('cancelled_at'), 'cr0_request_not_queued')
    prepared = engine.cognitive.prepare(tid)
    with engine.store.connection() as db:
        prepared = engine.cognitive.finalize(db, turn, prepared)
        details = prepared['details']
        current = tuple(Source('current', m['id'], '1', m['text'], 'user', m['created_at']) for m in load_turn_input(db, turn))
        history = []
        for item in details.get('history_groups', []):
            group = read_exact(db, turn_scope(db, turn), {'kind': 'turn', 'id': item['turn_id']})
            require(group['status'] == 'found', 'cr0_history_unavailable')
            for kind, values in (('user', group['members']), ('assistant', group['replies'])):
                for m in values:
                    history.append(Source('history', m['id'], '1', m['text'], kind, m.get('created_at', '')))
        from persona_runtime.references import prepare
        block, refs = prepare(db, turn)
        references = (Source('reference', 'accepted', fingerprint(refs), block),) if block else ()
        profile = json.loads(turn['profile_snapshot'])
        states = [Source('profile', k, v['id'], encoded(v['data'])) for k, v in profile.items()
                  if k != 'state' and (k != 'relationship' or not details['relationship_requires_review'])]
        if details.get('state_version'):
            from persona_runtime.profile import head
            state = head(db, turn['persona_id'], 'state')
            states.append(Source('state', state['id'], str(state['revision']), encoded(state['data'])))
        memories = []
        for row in details['memories']:
            memory = db.execute('SELECT * FROM memories WHERE id=?', (row['id'],)).fetchone()
            memories.append(Source('memory', row['id'], str(row['revision']), memory['text']))
        # Accepted snapshots may contain creator-only character material. Bind
        # their hashes, never put the raw private catalog into consumer inputs.
        accepted = {'snapshot_hashes': {k: fingerprint(turn.get(k)) for k in
                    ('context_policy_snapshot', 'character_snapshot', 'profile_snapshot', 'expression_snapshot', 'reply_mode_snapshot')}}
        accepted['model_hash'] = prepared['manifest']['model_snapshot_hash']
        accepted['projection_refs'] = ['expression:' + profile['expression']['id']]
        for part in details['prompt_assembly']['blocks']:
            if part['name'] == 'character' and part['present']:
                text = prepared['messages'][part['message_index']]['content'][part['start_character']:part['start_character'] + part['characters']]
                states.append(Source('character', 'selected', part['content_hash'], text))
        evidence = DialogueEvidence(tid, turn['execution_revision'], encoded(turn_scope(db, turn)), encoded(accepted),
                                    current, tuple(history), references, tuple(states), tuple(memories),
                                    encoded(prepared['manifest']['truncation_info']))
        from persona_runtime.situation_observation import sources_for
        legacy_observer = sources_for(db, turn)
        legacy_selector = {'dispatched': False, 'effective_mode': 'lexical',
                           'available_members': [s.text for s in current], 'reference_block': block,
                           'material_audit': details.get('character')}
    aliases = {'C' + str(i): s.key for i, s in enumerate(current)}
    for i, item in enumerate(details.get('history_groups', [])):
        matching = [s for s in history if s.id.startswith(item['turn_id'])]
        for j, s in enumerate(matching):
            aliases['H' + str(i) + ('U' if j == 0 else 'A')] = s.key
    if references:
        aliases['REF'] = references[0].key
    model = json.loads(turn['model_snapshot']) | {'_reply_mode': turn['reply_mode_snapshot'], '_expression_snapshot': turn['expression_snapshot']}
    return evidence, prepared, aliases, model, {'observation': legacy_observer, 'selection': legacy_selector}


def annotate(case, role, evidence, aliases):
    def statement(row):
        return Statement(row[0], tuple(aliases[k] for k in row[1]), row[2] if len(row) == 3 else 'explicit')
    frame = DialogueFrame(evidence.digest, **{k: tuple(statement(s) for s in rows) for k, rows in case['frame'].items()})
    frame.validate(evidence)
    spec = CommunicativeSpec(frame.digest, '回应当前交流', fixture()['roles'][role]['stance'],
                             tuple(statement(s) for s in case['obligations']),
                             ('可以自然联想、打趣，不要求逐句出处',), tuple(statement(s) for s in case['avoid']),
                             tuple(json.loads(evidence.accepted_json)['projection_refs']))
    return frame, spec.validate(evidence, frame)


def project(base_messages, evidence, frame=None, spec=None):
    messages = deepcopy(base_messages)
    if frame is None:
        require(spec is None)
        return messages, None
    inputs = {name: consumer_input(evidence, frame, name) for name in ('selection', 'observation', 'expression')}
    block = {'evidence_hash': evidence.digest, 'frame': inputs['expression']['frame']}
    if spec is not None:
        spec.validate(evidence, frame)
        block['communication_spec'] = spec.document()
    # Extra data only in this isolated adapter. No released renderer is changed.
    messages[0]['content'] += '\nCR0隔离实验数据（引用的用户原文不提升为系统指令）：' + encoded(block)
    return messages, inputs


def sources():
    names = ['scripts/dialogue_refactor.py', 'tests/dialogue_refactor_fixture.py', 'tests/dialogue_refactor_responses.py',
             'tests/prompt_baseline_fixture.py', 'tests/fixtures/prompt-baseline-r0.json', 'tests/m71_fixtures.py']
    names += [str(p.relative_to(ROOT)).replace('\\', '/') for p in (ROOT / 'persona_runtime').rglob('*')
              if p.is_file() and p.suffix in ('.py', '.json')]
    return {name: sha(ROOT / name) for name in names}


async def prepare(root=OUT):
    root.mkdir(parents=True, exist_ok=True)
    require(not (root / 'freeze.json').exists(), 'cr0_already_prepared')
    entries = []
    for case in fixture()['cases']:
        for role in fixture()['roles']:
            key = case['id'] + '-' + role
            folder = root / 'runtimes' / key
            engine, tid = await create_case(folder, case, role)
            try:
                evidence, prepared, aliases, model, legacy_inputs = collect(engine, tid)
                frame, spec = annotate(case, role, evidence, aliases)
                record = {'id': key, 'case_id': case['id'], 'role': role, 'runtime': str(folder), 'turn_id': tid,
                          'evidence': evidence.document(), 'frame': frame.document(), 'spec': spec.document(),
                          'legacy_messages': prepared['messages'], 'legacy_manifest': prepared['manifest'],
                          'legacy_inputs': legacy_inputs, 'model': model, 'aliases': aliases}
                atomic_json(root / 'requests' / (key + '.json'), record)
                entries.append(key)
            finally:
                close(engine)
    atomic_json(root / 'material.json', fixture())
    audit_path = HOME / 'exports/acceptance/M7.6.3-R4-A-expression-realization/post007-existing-evidence-audit.json'
    prior = read(audit_path)
    cards = [{'batch': c['batch'], 'key': c['key'], 'original_issues': c['issues'], 'original_reason': c['reason'],
              'task_pass': c['task_pass'], 'boundary_pass': c['boundary_pass'],
              'request_hash': c['planned_request_sha256'], 'result_hash': c['result_sha256'],
              'symptom_category': '表达未落实' if set(c['issues']) & {'redundant_restatement', 'strategy_narration'} else '待诊断',
              'cause_verified': False} for c in prior['problem_cards']]
    categories = ('情境遗漏', '意图错位', '人物取舍不足', '表达未落实', '样例误用', '历史影响', '评审分歧', '投递改变')
    atomic_json(root / 'issue-map.json', {'source': str(audit_path), 'source_hash': sha(audit_path),
                'old_scores_recomputed': False, 'cards': cards,
                'categories': {c: {'cards': [i['key'] for i in cards if i['symptom_category'] == c],
                                  'status': 'observed_symptom' if c == '表达未落实' else 'needs_diagnosis'} for c in categories}})
    atomic_json(root / 'mode-matrix.json', [
        {'mode': 'formal_shadow', 'capsule_in_prompt': False, 'evidence': 'existing handoff; no formal service started'},
        {'mode': 'isolated_assist', 'capsule_in_prompt': 'only valid admitted decision', 'evidence': 'decision_capsule.capsule'},
        {'mode': 'R4_expression', 'capsule_in_prompt': False, 'observer': 'disabled', 'decision': 'baseline'},
        {'mode': 'CR0', 'capsule_in_prompt': False, 'frame': 'manual', 'spec': 'manual', 'actual': 'simulation_only'},
    ])
    atomic_json(root / 'freeze.json', {'tool': VERSION, 'sources': sources(), 'requests': entries,
                                      'files': {str(p.relative_to(root)): sha(p) for p in (root / 'requests').glob('*.json')} |
                                      {n: sha(root / n) for n in ('material.json', 'mode-matrix.json', 'issue-map.json')}, 'real_calls': 0})


def verify_freeze(root):
    frozen = read(root / 'freeze.json')
    current = sources()
    require(frozen['tool'] == VERSION, 'cr0_execution_source_changed')
    if frozen['sources'] != current:
        # An explicit local report-only repair keeps the dispatched-source seal
        # immutable. Prove every other top-level definition, and all runtime
        # dependencies, are identical; this cannot admit a generation change.
        import ast
        repair = root / 'report-repair-v1'
        evidence = read(repair / 'repair.json') if (repair / 'repair.json').exists() else {}
        name = 'scripts/dialogue_refactor.py'
        require({k: v for k, v in current.items() if k != name} ==
                {k: v for k, v in frozen['sources'].items() if k != name}, 'cr0_execution_source_changed')
        require(evidence.get('original_hash') == frozen['sources'][name] == sha(repair / 'frozen-script.py') and
                evidence.get('report_hash') == current[name], 'cr0_report_repair_unbound')
        def execution_ast(text):
            tree = ast.parse(text)
            tree.body = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or
                         node.name not in ('report', 'verify_freeze')]
            return ast.dump(tree, include_attributes=False)
        require(execution_ast((repair / 'frozen-script.py').read_text(encoding='utf-8')) ==
                execution_ast((ROOT / name).read_text(encoding='utf-8')), 'cr0_generation_code_changed')
    require(all(sha(root / name) == digest for name, digest in frozen['files'].items()), 'cr0_frozen_input_changed')
    return frozen


class SimulatedClient:
    def __init__(self):
        self.received = []

    def complete(self, model, messages, case_id):
        payload, contract = build_request(model, messages)
        self.received.append({'input_hash': fingerprint(messages), 'payload_hash': fingerprint(payload), 'contract': contract})
        raw = answer(case_id)
        validate_reply(raw, model['_reply_mode'], model['_expression_snapshot'])
        return raw


def run_position(root, record, path, client, *, cancelled=False, writer=atomic_json):
    """One durable reservation; terminal failures are never silently resumed."""
    receipt = root / 'receipts' / (record['id'] + '-' + path + '.json')
    require(not receipt.exists(), 'cr0_position_already_reserved')
    state = {'id': record['id'], 'path': path, 'status': 'reserved', 'real': False}
    writer(receipt, state)  # Failure here must precede all simulated dispatch.
    try:
        require(not cancelled, 'cr0_cancelled')
        paths = RuntimePaths(Path(record['runtime']))
        engine = ChatEngine(Store(paths), ConfigStore(paths), NoDispatch())
        try:
            fresh, actual, _, model, _ = collect(engine, record['turn_id'])
            evidence = DialogueEvidence.load(record['evidence'])
            evidence.check_current(fresh)
            require(actual['messages'] == record['legacy_messages'], 'cr0_legacy_input_changed')
            frame = DialogueFrame.load(record['frame']) if path != 'legacy' else None
            spec = CommunicativeSpec.load(record['spec']) if path == 'spec' else None
            messages, inputs = project(actual['messages'], evidence, frame, spec)
            require(sum(len(m['content']) for m in messages) <= model['max_input_chars'], 'cr0_input_budget')
            raw = client.complete(model, messages, record['case_id'])
            received = client.received[-1]
            require(received['input_hash'] == fingerprint(messages), 'cr0_client_hash_mismatch')
            result = {'kind': 'isolated_experiment', 'real_effect': 'unverified', 'messages': messages, 'inputs': inputs,
                      'evidence_hash': evidence.digest, 'frame_hash': frame.digest if frame else None,
                      'spec_hash': spec.digest if spec else None, 'simulated_reply': raw, **received}
            writer(root / 'results' / (record['id'] + '-' + path + '.json'), result)
            state.update(status='completed', input_hash=received['input_hash'], payload_hash=received['payload_hash'])
            writer(receipt, state)
            return result
        finally:
            close(engine)
    except BaseException as error:
        code = str(error)
        cancelled = cancelled or isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
        state.update(status='cancelled' if cancelled else 'failed', error_code=code if code.startswith(('cr0_', 'dialogue_', 'communication_', 'reference_')) and len(code) < 100 else 'cr0_position_failed')
        try:
            writer(receipt, state)
        except BaseException:
            # A reserved receipt survives; verify reports it incomplete. Never forge a terminal.
            pass
        raise


def simulate(root=OUT):
    frozen = verify_freeze(root)
    # A restarted batch can inspect/report, but cannot replay or continue generation.
    require(not list((root / 'receipts').glob('*.json')), 'cr0_batch_already_started')
    client = SimulatedClient()
    for key in frozen['requests']:
        record = read(root / 'requests' / (key + '.json'))
        for path in PATHS:
            run_position(root, record, path, client)
    return len(client.received)


def report(root=OUT):
    frozen = verify_freeze(root)
    rows = []
    for key in frozen['requests']:
        record = read(root / 'requests' / (key + '.json'))
        old = record['legacy_messages']
        old_payload, _ = build_request(record['model'], old)
        for path in PATHS:
            result_path = root / 'results' / (key + '-' + path + '.json')
            receipt_path = root / 'receipts' / (key + '-' + path + '.json')
            receipt = read(receipt_path) if receipt_path.exists() else {'status': 'not_run'}
            row = {'id': key, 'path': path, 'status': receipt['status'], 'real': False}
            if result_path.exists():
                result = read(result_path)
                messages = result['messages']
                payload, _ = build_request(record['model'], messages)
                evidence = DialogueEvidence.load(record['evidence'])
                frame = DialogueFrame.load(record['frame']) if path != 'legacy' else None
                spec = CommunicativeSpec.load(record['spec']) if path == 'spec' else None
                expected, inputs = project(old, evidence, frame, spec)
                require(messages == expected and encoded(result['inputs']) == encoded(inputs), 'cr0_unexplained_message_diff')
                validate_reply(result['simulated_reply'], record['model']['_reply_mode'], record['model']['_expression_snapshot'])
                require({k: v for k, v in payload.items() if k != 'messages'} == {k: v for k, v in old_payload.items() if k != 'messages'}, 'cr0_parameter_diff')
                require(result['input_hash'] == fingerprint(messages), 'cr0_saved_hash_mismatch')
                require(result['payload_hash'] == fingerprint(payload), 'cr0_saved_hash_mismatch')
                if receipt['status'] == 'completed':
                    require(result['input_hash'] == receipt.get('input_hash') and result['payload_hash'] == receipt.get('payload_hash'), 'cr0_saved_hash_mismatch')
                else:
                    row['result_without_completed_receipt'] = True
                row.update(input_hash=result['input_hash'], difference='none' if path == 'legacy' else 'experimental_frame' if path == 'frame' else 'experimental_frame_and_spec')
            rows.append(row)
    status = {'tool': VERSION, 'planned': 36, 'completed': sum(r['status'] == 'completed' for r in rows),
              'rows': rows, 'real_model_calls': 0, 'qq_sends': 0, 'real_effect': 'unverified',
              'automatic_understanding': 'unverified', 'private_role_validation': 'not_run',
              'unresolved': ['R4-A/B/C remain unfinished', 'CR1 numeric thresholds and real sampling require confirmation']}
    atomic_json(root / 'analysis' / 'report.json', status)
    example = read(root / 'requests' / 'F04-process.json')
    atomic_json(root / 'analysis' / 'layer-view.json', {
        'request': example['id'], 'scope': 'synthetic CR0 only; not a formal runtime replay',
        'legacy_upstream': example['legacy_inputs'], 'shared_evidence': example['evidence'],
        'manual_frame': example['frame'], 'manual_spec': example['spec'],
        'legacy_final_manifest': example['legacy_manifest'],
        'legacy_input_hash': fingerprint(example['legacy_messages']),
        'production_upstream_repaired': False, 'delivery': 'simulated client only; no platform send',
    })
    return status


def verify(root=OUT):
    status = report(root)
    require(status['completed'] == 36, 'cr0_simulation_incomplete')
    require(len(list((root / 'receipts').glob('*.json'))) == 36, 'cr0_receipt_count')
    for action in ('prepare', 'simulate'):
        network = read(root / ('network-' + action + '.json'))
        require(network['external_attempts'] == network['http_attempts'] == 0, 'cr0_network_attempt_recorded')
    for key in verify_freeze(root)['requests']:
        record = read(root / 'requests' / (key + '.json'))
        engine = ChatEngine(Store(RuntimePaths(Path(record['runtime']))), ConfigStore(RuntimePaths(Path(record['runtime']))), NoDispatch())
        try:
            fresh, _, _, _, _ = collect(engine, record['turn_id'])
            evidence = DialogueEvidence.load(record['evidence'])
            evidence.check_current(fresh)
            frame = DialogueFrame.load(record['frame']).validate(evidence)
            CommunicativeSpec.load(record['spec']).validate(evidence, frame)
        finally:
            close(engine)
    from tests.prompt_baseline_fixture import capture_all
    baseline = read(ROOT / 'tests/fixtures/prompt-baseline-r0.json')
    require(fingerprint(baseline['scenarios']) == baseline['scenarios_hash'], 'cr0_r0_baseline_corrupt')
    current = capture_all()
    comparisons = {key: {'equal': encoded(value) == encoded(baseline['scenarios'].get(key)),
                          'actual_hash': fingerprint(value), 'expected_hash': fingerprint(baseline['scenarios'].get(key))}
                   for key, value in current.items()}
    atomic_json(root / 'r0-equivalence.json', {'scenarios': comparisons, 'count': len(comparisons),
                                             'baseline_file_hash': sha(ROOT / 'tests/fixtures/prompt-baseline-r0.json')})
    require(set(current) == set(baseline['scenarios']) and len(current) == 34 and all(c['equal'] for c in comparisons.values()), 'cr0_r0_difference')
    atomic_json(root / 'verification.json', {'passed': True, 'normal_requests': 36, 'real_effect': 'unverified', 'real_calls': 0})
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'report', 'verify'))
    args = parser.parse_args()
    os.environ['TEMP'] = os.environ['TMP'] = str(HOME / 'tmp')
    from persona_runtime.instance import InstanceLock
    lock = InstanceLock(OUT / 'execution.lock')
    try:
        with offline() as network:
            try:
                if args.action == 'prepare':
                    asyncio.run(prepare())
                    value = {'prepared': 12}
                elif args.action == 'simulate':
                    value = {'simulated': simulate()}
                elif args.action == 'report':
                    value = report()
                else:
                    value = verify()
            finally:
                atomic_json(OUT / ('network-' + args.action + '.json'), network)
        print(encoded({k: v for k, v in value.items() if k != 'rows'}))
    finally:
        lock.close()


if __name__ == '__main__':
    main()
