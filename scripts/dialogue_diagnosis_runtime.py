"""CR1 isolated offline preparation and simulation; no supplier executor.

Use actual versioned admission, the existing local recall model and the actual
v2 selector runtime with a fake client. Main sampling positions use the shared
audited_complete retry implementation. Their records are experimental inputs,
never saved production invocation history.
"""
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from persona_runtime.archive import encoded, fingerprint, read_exact, turn_scope
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_policy import choose
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, Source, consumer_input, require
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.dialogue_voice import freeze as freeze_voice
from persona_runtime.generation_retry import audited_complete
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input
from scripts.dialogue_diagnosis import HOME, OUT, ROOT, CONDITIONS, bind_annotations, validate_materials
from scripts.dialogue_refactor import NoDispatch, close
from scripts.example_calibration_r2 import IsolatedEmbeddingProvider
from tests.dialogue_diagnosis_fixture import fixture
from tests.dialogue_diagnosis_responses import selection as mock_selection, reply as mock_reply
from tests.m71_fixtures import seed_turn

VERSION = 'cr1-offline-path-v1'
FROZEN = ('profile_snapshot', 'character_snapshot', 'context_policy_snapshot', 'model_snapshot',
          'selector_model_snapshot', 'observation_model_snapshot', 'expression_snapshot', 'reply_mode_snapshot')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation makes a crash visible rather than overwriting evidence.
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded(value) + '\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())


def confined(path):
    path = Path(path).absolute()
    parent = HOME / 'tmp' if path.is_relative_to(HOME / 'tmp') else OUT
    return safe(path, parent)


def sources():
    paths = list((ROOT / 'persona_runtime').glob('*.py')) + list((ROOT / 'persona_runtime').glob('*.json'))
    paths += [ROOT / name for name in ('scripts/dialogue_diagnosis.py', 'scripts/dialogue_diagnosis_runtime.py',
              'scripts/dialogue_refactor.py', 'scripts/example_calibration_r2.py', 'tests/dialogue_diagnosis_fixture.py',
              'tests/dialogue_diagnosis_responses.py', 'tests/m71_fixtures.py')]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def public_models():
    config = ConfigStore(RuntimePaths(HOME)).load()
    return {section: {key: deepcopy(value) for key, value in config[section].items() if key != 'encrypted_key'}
            for section in ('model', 'character_selector')}


def configure(paths, models, case=None):
    cfg = ConfigStore(paths)
    cfg.initialize()
    value = cfg.load()
    for section in ('model', 'character_selector'):
        for key in value[section]:
            if key != 'encrypted_key' and key in models[section]:
                value[section][key] = deepcopy(models[section][key])
        value[section]['encrypted_key'] = ''
    value['organization']['daily_enabled'] = False
    if case is not None:
        value['model']['_dialogue_profile'] = freeze_voice('cr1-' + case['id'], case['members'], version='natural-dialogue-v2')
    atomic_json(cfg.file, value)
    return cfg


def clone(source, target):
    source, target = Path(source), Path(target)
    confined(target)
    require(not Path(target).exists(), 'cr1_runtime_exists')
    paths = RuntimePaths(target)
    paths.initialize()
    dest = Store(paths)
    srcfile = Store(RuntimePaths(source)).path
    with closing(sqlite3.connect(srcfile.resolve().as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(dest.path)) as dst:
        src.backup(dst)
    dest.initialize()
    return dest


def turn_for(engine, item):
    marker = read(engine.store.paths.home / 'artificial-cr1.json')
    require(marker == {'tool': VERSION, 'persona_id': item['persona_id'], 'key': item['key'], 'private': False},
            'cr1_isolation_marker')
    with engine.store.connection() as db:
        turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (item['turn_id'],)).fetchone())
    require(turn['persona_id'] == item['persona_id'] and turn['status'] == 'queued' and not turn['cancelled_at'],
            'cr1_request_inactive')
    require({key: fingerprint(turn[key]) for key in FROZEN} == item['snapshots'], 'cr1_accepted_snapshot_changed')
    require(not json.loads(turn['model_snapshot']).get('encrypted_key') and
            not json.loads(turn['selector_model_snapshot']).get('encrypted_key'), 'cr1_simulation_credentials')
    return turn


async def admit(store, config, person, case, role_id):
    engine = ChatEngine(store, config, NoDispatch())
    engine.store.character_committed = lambda *_: []
    try:
        service = RuntimeService(store)
        service.use_persona(person['persona_id'])
        sid = service.create_session()['id']
        history_ids = []
        with store.connection(write=True) as db:
            for index, pair in enumerate(case['history']):
                hid = f'cr1-{case["id"]}-{role_id}-history-{index}'
                history_ids.append(hid)
                seed_turn(db, person['persona_id'], sid, hid, [pair['user']], reply=pair['assistant'])
        ingress = IngressService(engine)
        current = ingress.current_policy(person['persona_id'], 'desktop')
        ingress.update_policy(person['persona_id'], 'desktop', 'natural_batch', current['id'], identifier(),
                              reply_mode='natural_structured', context_mode='working_set', cross_session=False,
                              character_retrieval_mode='hybrid_llm', disposition_mode='baseline',
                              situation_observation_mode='disabled', expression_policy_version=3)
        with patch.object(engine, '_schedule'):
            for index, text in enumerate(case['members']):
                payload = {'channel': 'desktop', 'persona_id': person['persona_id'], 'session_id': sid,
                           'text': text, 'request_id': identifier()}
                if case['reference'] and index == 0:
                    require(case['reference'].startswith('H') and case['reference'].endswith('U'), 'cr1_reference_alias')
                    payload['reference'] = {'kind': 'event', 'id': history_ids[int(case['reference'][1:-1])] + '-e1'}
                receipt = ingress.accept(payload)
            batch = ingress.get(receipt['batch_id'])
            tid = ingress.action(batch['id'], 'seal', batch['revision'], identifier(), person['persona_id'])['turn_id']
        with store.connection() as db:
            turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (tid,)).fetchone())
        key = case['id'] + '-' + role_id
        write_once(store.paths.home / 'artificial-cr1.json',
                   {'tool': VERSION, 'persona_id': person['persona_id'], 'key': key, 'private': False})
        return {'key': key, 'case_id': case['id'], 'role_id': role_id, 'persona_id': person['persona_id'],
                'turn_id': tid, 'runtime': str(store.paths.home), 'history_ids': history_ids,
                'snapshots': {key: fingerprint(turn[key]) for key in FROZEN}}
    finally:
        close(engine)


async def prepare(root):
    root = confined(root)
    require(not root.exists(), 'cr1_preparation_exists')
    data = fixture()
    validate_materials(data)
    models = public_models()
    require(models['character_selector']['enabled'], 'cr1_selector_disabled')
    root.mkdir(parents=True)
    write_once(root / 'materials.json', {k: v for k, v in data.items() if k != 'evaluation'})
    write_once(root / 'evaluation-only.json', data['evaluation'])
    policy = choose('hybrid_llm')
    provider = IsolatedEmbeddingProvider(RuntimePaths(HOME), HOME / 'models/character-embedding' /
                                         policy['config']['model_fingerprint'], root / 'embedding-cache')
    diagnostic = provider.diagnose()
    write_once(root / 'embedding.json', diagnostic)
    require(diagnostic['status'] == 'ready', 'cr1_local_embedding_unavailable')
    seed = Store(RuntimePaths(root / 'seed'))
    seed.initialize()
    configure(seed.paths, models)
    service = RuntimeService(seed)
    chars = CharacterService(seed)
    people = {}
    for role_id, role in data['roles'].items():
        spec = PersonaSpec(format_version=1, name=role['name'], introduction=role['identity'],
                           core_traits=list(role['disposition'].values()), expression_style=role['base_prompt'],
                           values_and_boundaries=['尊重明确更正与当前意愿'],
                           initial_background='\n'.join(role['facts']))
        pid = service.create_persona(spec)['id']
        service.use_persona(pid)
        from persona_runtime.profile import append_version
        with seed.connection(write=True) as db:
            append_version(db, pid, 'relationship', {'description': role['relationship']}, actor='test_author')
        character = empty_character()
        character['identity']['species'] = '人类'
        character['examples'] = [dict(row, enabled=True) for row in role['examples']]
        version = chars.update(pid, character, chars.current(pid)['id'], identifier())
        people[role_id] = {'persona_id': pid, 'character_version': version['id']}
    index = CharacterSemanticIndex(seed, provider)
    for person in people.values():
        index.build(person['persona_id'], person['character_version'])
    items = []
    cases = {c['id']: c for c in data['cases']}
    for case_id in data['real_subset']:
        case = cases[case_id]
        for role_id, person in people.items():
            folder = root / 'runtime' / (case_id + '-' + role_id)
            store = clone(seed.paths.home, folder)
            cfg = configure(store.paths, models, case)
            item = await admit(store, cfg, person, case, role_id)
            engine = ChatEngine(store, cfg, NoDispatch())
            try:
                engine.cognitive.characters.index_factory = lambda _: index
                turn = turn_for(engine, item)
                from persona_runtime.behavior import resolve_behavior
                members = case['members']
                scenarios = [s['act'] for s in resolve_behavior('\n\n'.join(members))['signals']]
                character = json.loads(turn['character_snapshot'])
                pool = engine.cognitive.characters.local_pool(character, policy, members, scenarios)
                from persona_runtime.references import prepare as references
                with store.connection() as db:
                    ref, _ = references(db, turn)
                from persona_runtime.character_selector_protocol import messages
                payload = messages(pool, members, ref, 4, models['character_selector']['max_input_chars'],
                                   prompt_version='character-selector-v2')
                item.update(pool=pool, pool_guard=fingerprint([character, policy, members, scenarios]),
                            selector_messages=payload, accepted={key: turn[key] for key in FROZEN})
                items.append(item)
            finally:
                close(engine)
        print(encoded({'prepared_scene': case_id, 'source_pairs': len(items), 'real_calls': 0}), flush=True)
    positions = []
    # Six positions per scenario/role, conditions rotate, replicate order alternates.
    # The expression seed is held fixed. No provider RNG seed is claimed.
    for scene_index, case_id in enumerate(data['real_subset']):
        group = [i for i in items if i['case_id'] == case_id]
        for replicate in range(2):
            for item in group if (scene_index + replicate) % 2 == 0 else reversed(group):
                start = (scene_index + replicate) % len(CONDITIONS)
                order = CONDITIONS[start:] + CONDITIONS[:start]
                for condition in order:
                    positions.append({'id': f'{item["key"]}/{condition}/r{replicate + 1}', 'item': item['key'],
                                      'condition': condition, 'replicate': replicate + 1})
    prepared = {'tool': VERSION, 'real': False, 'models': models, 'items': items, 'positions': positions,
                'expression_seed_policy': 'fixed_across_conditions_and_replicates; no provider RNG seed',
                'normal_selection_positions': 24, 'normal_main_positions': 144,
                'numeric_protocol': 'pending_user_choice', 'anonymous_review_count': 'pending_user_choice'}
    write_once(root / 'prepared.json', prepared)
    files = [root / name for name in ('materials.json', 'evaluation-only.json', 'embedding.json', 'prepared.json')]
    write_once(root / 'freeze.json', {'files': {p.name: sha(p) for p in files}, 'sources': sources()})
    return {'prepared_pairs': len(items), 'main_positions': len(positions), 'real_calls': 0}


def verify_freeze(root):
    root = confined(root)
    seal = read(root / 'freeze.json')
    require(all(sha(root / name) == value for name, value in seal['files'].items()), 'cr1_frozen_file_changed')
    require(seal['sources'] == sources(), 'cr1_execution_source_changed')
    return read(root / 'prepared.json')


class Ledger:
    """Uncapped physical receipts; max first+3 per logical position."""
    def __init__(self, root):
        self.root = Path(root)

    def rows(self):
        rows = []
        reserves = sorted(self.root.glob('*.reserve.json'))
        require({p.name.replace('.terminal.', '.reserve.') for p in self.root.glob('*.terminal.json')} <=
                {p.name for p in reserves}, 'cr1_orphan_terminal')
        for reserve in reserves:
            row = read(reserve)
            final = reserve.with_name(reserve.name.replace('.reserve.', '.terminal.'))
            if final.exists():
                terminal = read(final)
                require(terminal['reservation_hash'] == fingerprint(row), 'cr1_receipt_binding')
                row |= terminal
            rows.append(row)
        require([r['ordinal'] for r in rows] == list(range(1, len(rows) + 1)), 'cr1_receipt_sequence')
        return rows

    def reserve(self, slot, model, messages, kind):
        rows = self.rows()
        require(not any(row['status'] in ('reserved', 'interrupted') for row in rows), 'cr1_unsettled_receipt')
        old = [r for r in rows if r['slot'] == slot]
        require(len(old) < 4, 'cr1_retry_limit')
        require(not any(r['status'] == 'completed' for r in old), 'cr1_success_never_replay')
        request = build_request(model, messages)[0]
        require(not old or all(r['request'] == request for r in old), 'cr1_retry_request_changed')
        row = {'ordinal': len(rows) + 1, 'slot': slot, 'attempt_index': len(old), 'status': 'reserved',
               'kind': kind, 'simulation': True, 'request': request, 'input_hash': fingerprint(messages)}
        write_once(self.root / f'{row["ordinal"]:04d}.reserve.json', row)
        return row

    def finish(self, row, status, **details):
        require(status in ('completed', 'failed', 'interrupted'), 'cr1_terminal_status')
        write_once(self.root / f'{row["ordinal"]:04d}.terminal.json',
                   {'reservation_hash': fingerprint(row), 'status': status, **details})


class MockClient:
    def __init__(self, ledger, slot, raw, *, failures=()):
        self.ledger, self.slot, self.raw = ledger, slot, raw
        self.failures = list(failures)
        self.received = []

    async def complete(self, model, messages):
        require(not model.get('encrypted_key'), 'cr1_simulation_credentials')
        row = self.ledger.reserve(self.slot, model, messages, 'selector' if model.get('_character_selection') else 'main')
        self.received.append(deepcopy(messages))
        try:
            if self.failures:
                error = self.failures.pop(0)
                if error is not None:
                    raise error
            diagnostics = {'simulated': True, 'finish_reason': 'stop', 'response_model': 'simulated',
                           'system_fingerprint': 'simulation-only'}
            value = ModelCompletion(self.raw, {'total_tokens': 0}, diagnostics)
        except BaseException as error:
            self.ledger.finish(row, 'interrupted' if isinstance(error, asyncio.CancelledError) else 'failed',
                               error_type=type(error).__name__, usage=None)
            raise
        self.ledger.finish(row, 'completed', raw=self.raw, usage=value[1], diagnostics=diagnostics)
        return value


def collect(engine, item, selection):
    turn = turn_for(engine, item)
    prepared = engine.cognitive.prepare(item['turn_id'], character_selection=deepcopy(selection))
    with engine.store.connection() as db:
        prepared = engine.cognitive.finalize(db, turn, prepared)
        details = prepared['details']
        current = tuple(Source('current', m['id'], '1', m['text'], 'user', m['created_at'])
                        for m in load_turn_input(db, turn))
        history, aliases = [], {f'C{i}': s.key for i, s in enumerate(current)}
        for selected in details.get('history_groups', []):
            group = read_exact(db, turn_scope(db, turn), {'kind': 'turn', 'id': selected['turn_id']})
            require(group['status'] == 'found', 'cr1_history_unavailable')
            for speaker, entries in (('user', group['members']), ('assistant', group['replies'])):
                for entry in entries:
                    src = Source('history', entry['id'], '1', entry['text'], speaker, entry.get('created_at', ''))
                    history.append(src)
                    if selected['turn_id'] in item['history_ids']:
                        alias = f'H{item["history_ids"].index(selected["turn_id"])}' + ('U' if speaker == 'user' else 'A')
                        require(alias not in aliases, 'cr1_ambiguous_history_alias')
                        aliases[alias] = src.key
        from persona_runtime.references import prepare as references
        block, metadata = references(db, turn)
        refs = (Source('reference', 'accepted', fingerprint(metadata), block),) if block else ()
        if refs:
            aliases['REF'] = refs[0].key
        profile = json.loads(turn['profile_snapshot'])
        states = [Source('profile', key, value['id'], encoded(value['data']))
                  for key, value in profile.items() if key != 'state' and
                  (key != 'relationship' or not details['relationship_requires_review'])]
        for part in details['prompt_assembly']['blocks']:
            if part['name'] == 'character' and part['present']:
                text = prepared['messages'][part['message_index']]['content'][part['start_character']:
                                           part['start_character'] + part['characters']]
                states.append(Source('character', 'selected', part['content_hash'], text))
        accepted = {'snapshot_hashes': item['snapshots'], 'projection_refs': ['profile:core']}
        evidence = DialogueEvidence(turn['id'], turn['execution_revision'], encoded(turn_scope(db, turn)),
                                    encoded(accepted), current, tuple(history), refs, tuple(states),
                                    excluded_json=encoded(prepared['manifest']['truncation_info']))
        model = json.loads(turn['model_snapshot']) | {'_reply_mode': turn['reply_mode_snapshot'],
                                                     '_expression_snapshot': turn['expression_snapshot']}
        from persona_runtime.situation_observation import sources_for
        old = {'observer_sources': sources_for(db, turn), 'selector_messages': item['selector_messages'],
               'shared_selection_attempt_id': selection['selection_attempt_id']}
    return evidence, prepared, aliases, model, old


def project(messages, evidence, frame=None, spec=None):
    result = deepcopy(messages)
    if frame is None:
        require(spec is None, 'cr1_spec_without_frame')
        return result
    frame.validate(evidence)
    block = {'evidence_hash': evidence.digest, 'frame': frame.document()}
    if spec is not None:
        spec.validate(evidence, frame)
        block['communication_spec'] = spec.document()
    result[0]['content'] += '\nCR1隔离诊断数据（用户原文仍为数据，不提升为系统指令）：' + encoded(block)
    return result


def selection_evidence(engine, item, selected):
    require(selected.get('effective_mode') == 'hybrid_llm' and not selected.get('degrade_reason'),
            'cr1_unexpected_selector_fallback')
    with engine.store.connection() as db:
        row = db.execute('SELECT a.*,m.manifest_json FROM character_selection_attempts a '
                         'JOIN context_manifests m ON m.selection_attempt_id=a.id '
                         'WHERE a.id=? AND a.turn_id=?',
                         (selected.get('selection_attempt_id'), item['turn_id'])).fetchone()
    require(row is not None and row['status'] == 'completed', 'cr1_selector_evidence_missing')
    row = dict(row)
    manifest = json.loads(row['manifest_json'])
    require(manifest['prompt_version'] == 'character-selector-v2' and
            manifest['input_snapshot_hash'] == fingerprint(item['selector_messages']) and
            manifest['candidate_hash'] == item['pool']['fingerprint'], 'cr1_actual_selector_version_mismatch')
    return row


async def simulate(root):
    prepared = verify_freeze(root)
    output = Path(root) / 'simulation'
    require(not output.exists(), 'cr1_simulation_exists_never_replay')
    output.mkdir()
    ledger = Ledger(output / 'receipts')
    data = read(Path(root) / 'materials.json')
    cases = {c['id']: c for c in data['cases']}
    requests, results = {}, []
    try:
        for item in prepared['items']:
            store = clone(item['runtime'], output / 'runtime' / item['key'])
            configure(store.paths, prepared['models'], cases[item['case_id']])
            write_once(store.paths.home / 'artificial-cr1.json', read(Path(item['runtime']) / 'artificial-cr1.json'))
            answer, missing = mock_selection(item['case_id'], item['pool'])
            client = MockClient(ledger, item['key'] + '/selection', encoded(answer))
            engine = ChatEngine(store, ConfigStore(store.paths), NoDispatch())
            engine.selector.client = client
            try:
                def pool(version, policy, members, scenario=()):
                    require(fingerprint([version, policy, members, scenario]) == item['pool_guard'], 'cr1_candidate_guard')
                    return deepcopy(item['pool'])
                engine.cognitive.characters.local_pool = pool
                selected = await engine.selector.prepare(item['turn_id'])
                selector_audit = selection_evidence(engine, item, selected)
                require(client.received == [item['selector_messages']], 'cr1_selector_input_changed')
                evidence, base, aliases, model, old = collect(engine, item, selected)
                frame, spec, audit = bind_annotations(cases[item['case_id']], item['role_id'], evidence, aliases)
                base_hash = fingerprint(base['messages'])
                require(base_hash == base['manifest']['input_snapshot_hash'], 'cr1_legacy_manifest_input_mismatch')
                records = {}
                for condition in CONDITIONS:
                    messages = project(base['messages'], evidence, frame if condition != 'B0' else None,
                                       spec if condition == 'B2' else None)
                    require(sum(len(m['content']) for m in messages) <= model['max_input_chars'], 'cr1_input_limit')
                    records[condition] = {'messages': messages, 'model': model, 'input_hash': fingerprint(messages),
                                          'provider_request_hash': fingerprint(build_request(model, messages)[0])}
                    requests[(item['key'], condition)] = records[condition]
                direct = project(base['messages'], evidence, frame)
                require(direct == records['B1']['messages'], 'cr1_B6_requires_user_decision')
                write_once(output / 'contexts' / (item['key'] + '.json'),
                           {'simulation': True, 'item': item['key'], 'selection': selected, 'selector_runtime_audit': selector_audit,
                            'mock_missing_candidates': missing, 'legacy_manifest': base['manifest'],
                            'legacy_inputs': old, 'evidence': evidence.document(), 'frame': frame.document(),
                            'spec': spec.document(), 'annotation_audit': audit,
                            'adapter_inputs': {name: consumer_input(evidence, frame, name)
                                               for name in ('selection', 'observation', 'expression')},
                            'upstream_frame_adapter_redispatched': False, 'conditions': records,
                            'B6_merged_with': 'B1'})
            finally:
                close(engine)
            print(encoded({'simulated_shared_selection': item['key'], 'real_calls': 0}), flush=True)
        for position in prepared['positions']:
            record = requests[(position['item'], position['condition'])]
            item = next(i for i in prepared['items'] if i['key'] == position['item'])
            client = MockClient(ledger, position['id'], encoded(mock_reply(item['case_id'])))
            state_path = output / 'retry-audit' / (position['id'].replace('/', '--') + '.json')
            def before_attempt(state):
                require(fingerprint(build_request(record['model'], record['messages'])[0]) ==
                        record['provider_request_hash'], 'cr1_main_request_changed')
            value = await audited_complete(client, record['model'], record['messages'],
                        validate=lambda raw: validate_reply(raw, record['model']['_reply_mode'], record['model']['_expression_snapshot']),
                        save=lambda value: atomic_json(state_path, value), before_attempt=before_attempt)
            require(client.received == [record['messages']], 'cr1_main_client_input_mismatch')
            result = {'position': position, 'simulation': True, 'status': 'completed',
                      'input_hash': fingerprint(client.received[0]), 'request_hash': record['provider_request_hash'],
                      'raw': value[0], 'usage': value[1], 'diagnostics': value.diagnostics}
            write_once(output / 'results' / (position['id'].replace('/', '--') + '.json'), result)
            results.append(result)
        write_once(output / 'terminal.json', {'status': 'completed', 'simulation': True, 'shared_selections': 24,
                                              'main_positions': len(results), 'real_calls': 0})
    except BaseException as error:
        write_once(output / 'terminal.json', {'status': 'interrupted' if isinstance(error, asyncio.CancelledError) else 'failed',
                                              'error_type': type(error).__name__, 'error_code': str(error),
                                              'simulation': True, 'completed_main_positions': len(results), 'real_calls': 0})
        raise
    return verify_simulation(root)


def verify_simulation(root):
    prepared = verify_freeze(root)
    output = Path(root) / 'simulation'
    terminal = read(output / 'terminal.json')
    require(terminal['status'] == 'completed' and terminal['main_positions'] == 144, 'cr1_simulation_incomplete')
    rows = Ledger(output / 'receipts').rows()
    require(len(rows) == 168 and all(r['status'] == 'completed' and r['simulation'] for r in rows), 'cr1_normal_receipts')
    require(sum(r['kind'] == 'selector' for r in rows) == 24, 'cr1_selection_count')
    require([r['slot'] for r in rows if r['kind'] == 'main'] == [p['id'] for p in prepared['positions']],
            'cr1_main_order_changed')
    contexts = {}
    differences = []
    for item in prepared['items']:
        context = read(output / 'contexts' / (item['key'] + '.json'))
        contexts[item['key']] = context
        evidence = DialogueEvidence.load(context['evidence'])
        frame = DialogueFrame.load(context['frame']).validate(evidence)
        spec = CommunicativeSpec.load(context['spec']).validate(evidence, frame)
        require(json.loads(evidence.accepted_json)['snapshot_hashes'] == item['snapshots'], 'cr1_evidence_snapshot_mismatch')
        actual = context['selector_runtime_audit']
        manifest = json.loads(actual['manifest_json'])
        require(manifest['prompt_version'] == 'character-selector-v2' and actual['status'] == 'completed',
                'cr1_selector_runtime_version_mismatch')
        selected_rows = [r for r in rows if r['slot'] == item['key'] + '/selection']
        require(len(selected_rows) == 1, 'cr1_shared_selection_repeated')
        selected_row = selected_rows[0]
        audit = json.loads(actual['usage_json'])['generation_retry']['attempts']
        require(len(audit) == 1 and audit[0]['status'] == selected_row['status'] and
                audit[0]['request_hash'] == fingerprint(selected_row['request']), 'cr1_selector_runtime_audit_mismatch')
        require(json.loads(actual['selected_json']) == json.loads(selected_row['raw']) ==
                context['selection']['selector_returned'], 'cr1_selector_returned_mismatch')
        require(fingerprint(item['selector_messages']) == selected_row['input_hash'] == manifest['input_snapshot_hash'],
                'cr1_selector_client_input_mismatch')
        base = context['conditions']['B0']
        require(fingerprint(base['messages']) == context['legacy_manifest']['input_snapshot_hash'],
                'cr1_legacy_baseline_changed')
        for condition in CONDITIONS:
            request = context['conditions'][condition]
            require(request['model'] == base['model'], 'cr1_condition_model_changed')
            expected = project(base['messages'], evidence, frame if condition != 'B0' else None,
                               spec if condition == 'B2' else None)
            require(expected == request['messages'], 'cr1_unexplained_message_difference')
            require(fingerprint(expected) == request['input_hash'], 'cr1_context_input_hash')
            require(sum(len(m['content']) for m in expected) <= request['model']['max_input_chars'], 'cr1_input_limit')
        require(build_request(base['model'], project(base['messages'], evidence, frame))[0] ==
                build_request(base['model'], context['conditions']['B1']['messages'])[0], 'cr1_B6_not_equivalent')
        differences.append({'item': item['key'], 'B1_minus_B0': 'registered Frame block only',
                            'B2_minus_B1': 'registered Spec within experiment block only',
                            'B6': 'same full provider payload as B1; merged',
                            'scripted_choice_missing_from_candidates': context['mock_missing_candidates'],
                            'input_characters': {c: sum(len(m['content']) for m in context['conditions'][c]['messages'])
                                                 for c in CONDITIONS},
                            'selected_budget': context['selection']['budgets'],
                            'effect': 'unverified_simulation_only'})
    for position in prepared['positions']:
        result = read(output / 'results' / (position['id'].replace('/', '--') + '.json'))
        context = contexts[position['item']]
        request = context['conditions'][position['condition']]
        physical = [r for r in rows if r['slot'] == position['id']]
        require(len(physical) == 1 and result['input_hash'] == request['input_hash'] == physical[0]['input_hash'],
                'cr1_result_input_mismatch')
        require(result['request_hash'] == fingerprint(physical[0]['request']) == request['provider_request_hash'],
                'cr1_result_request_mismatch')
        require(result['usage']['generation_retry']['attempts'][0]['request_hash'] == result['request_hash'],
                'cr1_main_runtime_audit_mismatch')
        require(result['status'] == 'completed' and result['simulation'] and result['raw'] == physical[0]['raw'],
                'cr1_main_output_mismatch')
    require(len(list((output / 'results').glob('*.json'))) == 144, 'cr1_unexpected_result_files')
    summary = {'tool': VERSION, 'status': 'offline_simulation_passed', 'normal_shared_selections': 24,
               'normal_main_positions': 144, 'physical_simulations': len(rows), 'B6_merged_with': 'B1',
               'real_calls': 0, 'qq_sends': 0, 'expression_effect': 'unverified',
               'numeric_protocol': 'pending_user_choice', 'anonymous_review_count': 'pending_user_choice'}
    atomic_json(Path(root) / 'input-differences.json', differences)
    atomic_json(Path(root) / 'offline-report.json', summary)
    return summary
