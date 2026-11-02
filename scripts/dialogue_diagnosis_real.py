"""CR1 approved real cohort, separate from the sealed offline runner.

Only run on a --real prepared artificial pack can dispatch a provider call.
One native selector per scene/role; main positions reuse its frozen material.
No outer retries, paid judge, successful replay or automatic crash recovery.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import encoded, fingerprint, read_exact, turn_scope
from persona_runtime.chat import ChatCompletionsClient, ChatEngine, ModelCompletion
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, Source, consumer_input, require
from persona_runtime.generation_retry import audited_complete, safe_diagnostics
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input
from scripts import dialogue_diagnosis_runtime as base
from scripts.dialogue_diagnosis import bind_annotations
from scripts.dialogue_refactor import offline, NoDispatch, close

HOME, OUT = base.HOME, base.OUT
PARENT = OUT / 'offline-v3'
VERSION = 'cr1-real-v1'
PROTOCOL = ROOT / 'tests/fixtures/dialogue-diagnosis-protocol-v1.json'
read, sha, once = base.read, base.sha, base.write_once


def public(value):
    if isinstance(value, dict):
        return {k: (encoded(public(json.loads(v or '{}'))) if k.endswith('model_snapshot') and isinstance(v, str)
                    else public(v)) for k, v in value.items() if k != 'encrypted_key'}
    if isinstance(value, list): return [public(v) for v in value]
    return value


def snapshot(turn):
    return {k: public(json.loads(turn[k] or '{}')) if k.endswith('model_snapshot') else turn[k] for k in base.FROZEN}


def selector_material_control(payload):
    """Only the newly admitted current-event link is metadata, not reference text."""
    result = deepcopy(payload)
    body = json.loads(result[1]['content'])
    references = body.get('references', '')
    if references:
        offset = references.index('{')
        value = json.loads(references[offset:])
        for selection in value['selections']:
            selection['input_event_id'] = '<new-admitted-current-member>'
        body['references'] = references[:offset] + encoded(value)
    result[1]['content'] = encoded(body)
    return result


def execution_sources():
    paths = dict(base.sources())
    for name in ('scripts/dialogue_diagnosis_real.py', 'scripts/dialogue_diagnosis_analysis.py',
                 'tests/fixtures/dialogue-diagnosis-protocol-v1.json'):
        paths[name] = sha(ROOT / name)
    return paths


def protect():
    start = read(OUT / 'start.json')
    for name, value in start['protected_before'].items():
        path = HOME / name
        require((sha(path) if path.is_file() else None) == value, 'cr1_formal_file_changed')
    old = OUT.parent / 'CR0-contracts'
    require(sha(old / 'closure.json') == start['CR0_closure_hash'], 'cr1_CR0_closure_changed')
    for name, value in read(old / 'closure.json')['evidence_hashes'].items():
        require(sha(old / name) == value, 'cr1_CR0_evidence_changed')


def check_freeze(root):
    root = base.confined(root)
    seal = read(root / 'freeze.json')
    require(seal['sources'] == execution_sources(), 'cr1_real_execution_source_changed')
    require(all(sha(root / name) == value for name, value in seal['files'].items()), 'cr1_real_frozen_input_changed')
    return read(root / 'prepared.json')


def owned_turn(engine, item, root):
    home = base.confined(engine.store.paths.home)
    require(home.is_relative_to(Path(root).resolve() / 'runtime'), 'cr1_runtime_outside_pack')
    require(read(home / 'artificial-cr1.json') == {'tool': base.VERSION, 'persona_id': item['persona_id'],
                'key': item['key'], 'private': False}, 'cr1_real_artificial_marker')
    require(sha(engine.config.file) == item['config_hash'], 'cr1_isolated_configuration_changed')
    with engine.store.connection() as db:
        turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (item['turn_id'],)).fetchone())
    require(turn['persona_id'] == item['persona_id'] and turn['status'] == 'queued' and not turn['cancelled_at'],
            'cr1_real_request_inactive')
    require({k: fingerprint(turn[k]) for k in base.FROZEN} == item['private_snapshot_guards'], 'cr1_real_snapshot_changed')
    return turn


async def prepare(root, *, real):
    root = base.confined(root)
    require(not root.exists(), 'cr1_real_preparation_exists')
    protect()
    old = base.verify_freeze(PARENT)
    decisions = read(OUT / 'decisions.json')
    require(not decisions['pending'] and decisions['research_signal'] == .5 and
            decisions['user_anonymous_review_count'] == 12, 'cr1_protocol_not_approved')
    approved = ConfigStore(RuntimePaths(HOME)).load()
    require({k: public(approved[k]) for k in ('model', 'character_selector')} == old['models'], 'cr1_public_models_changed')
    if real:
        require(all(approved[k].get('encrypted_key') for k in ('model', 'character_selector')), 'cr1_credential_missing')
    root.mkdir(parents=True)
    once(root / 'owner.json', {'tool': VERSION, 'real': real, 'artificial_only': True})
    materials = read(PARENT / 'materials.json')
    cases = {c['id']: c for c in materials['cases']}
    once(root / 'materials.json', materials)
    once(root / 'evaluation-only.json', read(PARENT / 'evaluation-only.json'))
    once(root / 'protocol.json', read(PROTOCOL))
    items = []
    for original in old['items']:
        key = original['key']
        # Copy the unexecuted artificial seed, never a simulation database.
        source = base.confined(PARENT / 'seed')
        store = base.clone(source, root / 'runtime' / key)
        cfg = base.configure(store.paths, old['models'], cases[original['case_id']])
        if real:
            value = cfg.load()
            for section in ('model', 'character_selector'):
                value[section]['encrypted_key'] = approved[section]['encrypted_key']
            atomic_json(cfg.file, value)  # Before admission only; DPAPI remains encrypted.
        item = await base.admit(store, cfg, {'persona_id': original['persona_id']}, cases[original['case_id']], original['role_id'])
        with store.connection() as db:
            turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (item['turn_id'],)).fetchone())
            from persona_runtime.references import prepare as references
            refs, _ = references(db, turn)
        from persona_runtime.character_selector_protocol import messages
        selector_messages = messages(original['pool'], cases[item['case_id']]['members'], refs, 4,
                                     old['models']['character_selector']['max_input_chars'], prompt_version='character-selector-v2')
        require(selector_material_control(selector_messages) == selector_material_control(original['selector_messages']),
                'cr1_selector_material_changed')
        require(turn['profile_snapshot'] == original['accepted']['profile_snapshot'] and
                turn['character_snapshot'] == original['accepted']['character_snapshot'], 'cr1_persona_material_changed')
        # Public model snapshots must equal the offline preparation. Policy IDs,
        # request IDs and timestamps are newly admitted metadata, not prompt edits.
        for field in ('model_snapshot', 'selector_model_snapshot', 'observation_model_snapshot'):
            require(public(json.loads(turn[field] or '{}')) == public(json.loads(original['accepted'][field] or '{}')),
                    'cr1_public_snapshot_changed')
        private_guards = item.pop('snapshots')
        item.update(private_snapshot_guards=private_guards, snapshots={k: fingerprint(v) for k, v in snapshot(turn).items()},
                    accepted_public=snapshot(turn), pool=deepcopy(original['pool']),
                    pool_guard=original['pool_guard'], selector_messages=selector_messages, config_hash=sha(cfg.file),
                    reference_metadata_change='new admitted input_event_id only' if selector_messages != original['selector_messages'] else None)
        items.append(item)
    prepared = {'tool': VERSION, 'real': real, 'models': old['models'], 'items': items, 'positions': old['positions'],
                'parent_freeze_hash': sha(PARENT / 'freeze.json'), 'protocol_hash': sha(root / 'protocol.json'),
                'normal_logical_positions': 168, 'aggregate_call_cap': None,
                'admission_metadata_note': 'new request/policy IDs and timestamps; no accepted snapshot edits',
                'execution_order': 'all 24 shared selections, then frozen interleaved main positions; first two scenes are main smoke',
                'real_effect': 'not_executed'}
    once(root / 'prepared.json', prepared)
    files = ('owner.json', 'materials.json', 'evaluation-only.json', 'protocol.json', 'prepared.json')
    once(root / 'freeze.json', {'files': {name: sha(root / name) for name in files}, 'sources': execution_sources()})
    return {'prepared': len(items), 'main_positions': len(prepared['positions']), 'real_pack': real, 'real_calls': 0}


class Receipts:
    """Transport returns are not successes until the runtime validates them."""
    def __init__(self, root): self.root = Path(root)

    def rows(self):
        reserves = sorted(self.root.glob('*.reserve.json'))
        require({p.name.replace('.terminal.', '.reserve.') for p in self.root.glob('*.terminal.json')} <=
                {p.name for p in reserves}, 'cr1_orphan_real_terminal')
        result = []
        for file in reserves:
            row = read(file)
            reservation_hash = fingerprint(row)
            for suffix in ('transport', 'terminal'):
                path = file.with_name(file.name.replace('.reserve.', '.' + suffix + '.'))
                if path.exists():
                    value = read(path)
                    require(value['reservation_hash'] == reservation_hash, 'cr1_real_receipt_binding')
                    row[suffix] = value
            result.append(row)
        require([r['ordinal'] for r in result] == list(range(1, len(result) + 1)), 'cr1_real_receipt_sequence')
        return result

    def sync(self, slot, state):
        rows = [r for r in self.rows() if r['slot'] == slot]
        attempts = (state or {}).get('generation_retry', {}).get('attempts', [])
        for row in rows:
            require(row['attempt_index'] < len(attempts), 'cr1_runtime_receipt_missing')
            audit = attempts[row['attempt_index']]
            require(audit['request_hash'] == row['request_hash'], 'cr1_runtime_request_hash_mismatch')
            if audit['status'] != 'running':
                final = {'reservation_hash': fingerprint({k: v for k, v in row.items() if k not in ('transport', 'terminal')}),
                         'status': audit['status'], 'runtime_audit': audit}
                path = self.root / f'{row["ordinal"]:04d}.terminal.json'
                if path.exists(): require(read(path) == final, 'cr1_terminal_changed')
                else: once(path, final)
                elapsed = row.get('transport', {}).get('elapsed_seconds', 0)
                require(not (audit['status'] != 'completed' and elapsed > 600), 'cr1_physical_failure_over_600s')

    def reserve(self, slot, kind, model, messages, real):
        rows = self.rows()
        require(all(r.get('terminal', {}).get('status') in ('completed', 'failed') for r in rows), 'cr1_unsettled_real_receipt')
        previous = [r for r in rows if r['slot'] == slot]
        require(len(previous) < 4, 'cr1_physical_retry_limit')
        require(not any(r['terminal']['status'] == 'completed' for r in previous), 'cr1_real_success_never_replay')
        payload = build_request(model, messages)[0]
        request_hash = fingerprint(payload)
        require(all(r['request_hash'] == request_hash for r in previous), 'cr1_retry_payload_changed')
        row = {'ordinal': len(rows) + 1, 'slot': slot, 'kind': kind, 'attempt_index': len(previous),
               'real': real, 'status': 'reserved', 'started_epoch': time.time(), 'input_hash': fingerprint(messages),
               'request_hash': request_hash, 'request': payload}
        once(self.root / f'{row["ordinal"]:04d}.reserve.json', row)
        return row

    def transport(self, row, details):
        once(self.root / f'{row["ordinal"]:04d}.transport.json', {'reservation_hash': fingerprint(row), **details})


class TrackedClient:
    def __init__(self, transport, ledger, slot, kind, real, audit, guard, expected_messages):
        self.transport, self.ledger, self.slot, self.kind = transport, ledger, slot, kind
        self.real, self.audit, self.guard, self.expected_messages = real, audit, guard, expected_messages

    async def complete(self, model, messages):
        self.guard()
        require(messages == self.expected_messages, 'cr1_dispatched_messages_changed')
        self.ledger.sync(self.slot, self.audit())
        row = self.ledger.reserve(self.slot, self.kind, model, messages, self.real)
        started = time.monotonic()
        try:
            value = await self.transport.complete(model, messages)
        except BaseException as error:
            code = str(error)
            if not re.fullmatch('[a-z][a-z0-9_]{0,100}', code): code = type(error).__name__
            self.ledger.transport(row, {'status': 'error', 'error_code': code, 'usage': getattr(error, 'usage', None),
                       'diagnostics': safe_diagnostics(getattr(error, 'diagnostics', None)),
                       'elapsed_seconds': time.monotonic() - started})
            raise
        self.ledger.transport(row, {'status': 'returned', 'raw': value[0], 'usage': value[1],
                   'diagnostics': safe_diagnostics(getattr(value, 'diagnostics', None)),
                   'elapsed_seconds': time.monotonic() - started})
        return value


def selector_audit(engine, item):
    with engine.store.connection() as db:
        row = db.execute('SELECT usage_json FROM character_selection_attempts WHERE turn_id=? ORDER BY rowid DESC',
                         (item['turn_id'],)).fetchone()
    return json.loads(row[0] or '{}') if row else {}


def collect(engine, item, selected, root):
    """Same collection as sealed offline path, with credential-safe source hashes."""
    turn = owned_turn(engine, item, root)
    prepared = engine.cognitive.prepare(item['turn_id'], character_selection=deepcopy(selected))
    with engine.store.connection() as db:
        prepared = engine.cognitive.finalize(db, turn, prepared)
        details = prepared['details']
        current = tuple(Source('current', m['id'], '1', m['text'], 'user', m['created_at']) for m in load_turn_input(db, turn))
        aliases = {f'C{i}': s.key for i, s in enumerate(current)}
        history = []
        for item_history in details.get('history_groups', []):
            group = read_exact(db, turn_scope(db, turn), {'kind': 'turn', 'id': item_history['turn_id']})
            require(group['status'] == 'found', 'cr1_real_history_unavailable')
            for speaker, entries in (('user', group['members']), ('assistant', group['replies'])):
                for entry in entries:
                    source = Source('history', entry['id'], '1', entry['text'], speaker, entry.get('created_at', ''))
                    history.append(source)
                    if item_history['turn_id'] in item['history_ids']:
                        aliases[f'H{item["history_ids"].index(item_history["turn_id"])}' + ('U' if speaker == 'user' else 'A')] = source.key
        from persona_runtime.references import prepare as references
        block, metadata = references(db, turn)
        refs = (Source('reference', 'accepted', fingerprint(metadata), block),) if block else ()
        if refs: aliases['REF'] = refs[0].key
        profile = json.loads(turn['profile_snapshot'])
        states = [Source('profile', key, value['id'], encoded(value['data'])) for key, value in profile.items()
                  if key != 'state' and (key != 'relationship' or not details['relationship_requires_review'])]
        for part in details['prompt_assembly']['blocks']:
            if part['name'] == 'character' and part['present']:
                text = prepared['messages'][part['message_index']]['content'][part['start_character']:part['start_character'] + part['characters']]
                states.append(Source('character', 'selected', part['content_hash'], text))
        evidence = DialogueEvidence(turn['id'], turn['execution_revision'], encoded(turn_scope(db, turn)),
                encoded({'snapshot_hashes': item['snapshots'], 'projection_refs': ['profile:core']}),
                current, tuple(history), refs, tuple(states), excluded_json=encoded(prepared['manifest']['truncation_info']))
        model = json.loads(turn['model_snapshot']) | {'_reply_mode': turn['reply_mode_snapshot'], '_expression_snapshot': turn['expression_snapshot']}
    return evidence, prepared, aliases, model


def context_for(engine, item, selected, case, root):
    evidence, baseline, aliases, model = collect(engine, item, selected, root)
    frame, spec, audit = bind_annotations(case, item['role_id'], evidence, aliases)
    conditions = {}
    for condition in base.CONDITIONS:
        messages = base.project(baseline['messages'], evidence, frame if condition != 'B0' else None, spec if condition == 'B2' else None)
        require(sum(len(m['content']) for m in messages) <= model['max_input_chars'], 'cr1_real_input_limit')
        conditions[condition] = {'messages': messages, 'input_hash': fingerprint(messages),
                                'request_hash': fingerprint(build_request(model, messages)[0])}
    require(conditions['B0']['input_hash'] == baseline['manifest']['input_snapshot_hash'], 'cr1_real_baseline_mismatch')
    return {'item': item['key'], 'source_hash': evidence.digest, 'evidence': evidence.document(), 'frame': frame.document(),
            'spec': spec.document(), 'annotation_audit': audit, 'selected': selected,
            'baseline_preview_manifest': baseline['manifest'], 'conditions': conditions, 'model_public': public(model),
            'adapter_inputs': {name: consumer_input(evidence, frame, name) for name in ('selection', 'observation', 'expression')},
            'upstream_frame_redispatched': False, 'B6_merged_with': 'B1'}, model


async def execute(root, *, transport_factory=None):
    prepared = check_freeze(root)
    root = Path(root)
    require(not (root / 'execution-start.json').exists(), 'cr1_execution_already_started_no_automatic_recovery')
    protect()
    once(root / 'execution-start.json', {'tool': VERSION, 'real': prepared['real'], 'started_epoch': time.time()})
    ledger = Receipts(root / 'receipts')
    cases = {c['id']: c for c in read(root / 'materials.json')['cases']}
    engines, contexts = {}, {}
    completed, phase = 0, 'selection'
    try:
        for item in prepared['items']:
            engine = ChatEngine(Store(RuntimePaths(Path(item['runtime']))), ConfigStore(RuntimePaths(Path(item['runtime']))), NoDispatch())
            engines[item['key']] = engine
            def pool(version, policy, members, scenario=(), item=item):
                require(fingerprint([version, policy, members, scenario]) == item['pool_guard'], 'cr1_real_candidate_guard')
                return deepcopy(item['pool'])
            engine.cognitive.characters.local_pool = pool
            slot = item['key'] + '/selection'
            # Synthetic transport is injected only in the separately prepared simulation pack.
            if prepared['real']:
                require(transport_factory is None, 'cr1_real_transport_override_forbidden')
                transport = ChatCompletionsClient()
            else:
                from tests.dialogue_diagnosis_responses import selection
                raw, _ = selection(item['case_id'], item['pool'])
                transport = transport_factory(slot, encoded(raw)) if transport_factory else SyntheticTransport(encoded(raw))
            tracker = TrackedClient(transport, ledger, slot, 'selector', prepared['real'],
                        lambda engine=engine, item=item: selector_audit(engine, item),
                        lambda engine=engine, item=item: owned_turn(engine, item, root), item['selector_messages'])
            engine.selector.client = tracker
            try: selected = await engine.selector.prepare(item['turn_id'])
            finally: ledger.sync(slot, selector_audit(engine, item))
            actual = base.selection_evidence(engine, item, selected)
            once(root / 'selection' / (item['key'] + '.json'), {'selected': selected, 'runtime_audit': public(actual), 'real': prepared['real']})
            context, model = context_for(engine, item, selected, cases[item['case_id']], root)
            contexts[item['key']] = (context, model)
            once(root / 'contexts' / (item['key'] + '.json'), context)
            print(encoded({'phase': 'selection', 'item': item['key'], 'logical_completed': len(contexts), 'physical_calls': len(ledger.rows()), 'real': prepared['real']}), flush=True)
        once(root / 'contexts-freeze.json', {key: sha(root / 'contexts' / (key + '.json')) for key in contexts})
        phase = 'main'
        smoke_cases = set(read(root / 'materials.json')['real_subset'][:2])
        for position in prepared['positions']:
            check_freeze(root)
            item = next(i for i in prepared['items'] if i['key'] == position['item'])
            engine = engines[item['key']]
            context, model = contexts[item['key']]
            request = context['conditions'][position['condition']]
            slot = position['id']
            audit_path = root / 'retry-audit' / (slot.replace('/', '--') + '.json')
            def audit(audit_path=audit_path): return read(audit_path) if audit_path.exists() else {}
            def guard(engine=engine, item=item, context=context, request=request):
                owned_turn(engine, item, root)
                fresh, fresh_model = context_for(engine, item, context['selected'], cases[item['case_id']], root)
                require(fresh['source_hash'] == context['source_hash'] and
                        fresh['conditions'][position['condition']] == request and fresh_model == model,
                        'cr1_real_sources_or_inputs_changed')
            transport = ChatCompletionsClient() if prepared['real'] else SyntheticTransport('')
            if not prepared['real']:
                from tests.dialogue_diagnosis_responses import reply
                raw = encoded(reply(item['case_id']))
                transport = transport_factory(slot, raw) if transport_factory else SyntheticTransport(raw)
            tracker = TrackedClient(transport, ledger, slot, 'main', prepared['real'], audit, guard, request['messages'])
            def save(value):
                atomic_json(audit_path, value)
                ledger.sync(slot, value)
            started = time.time()
            once(root / 'positions' / (slot.replace('/', '--') + '.reserve.json'), {'position': position, 'request_hash': request['request_hash'], 'real': prepared['real']})
            value = await audited_complete(tracker, model, request['messages'],
                        validate=lambda raw: validate_reply(raw, model['_reply_mode'], model['_expression_snapshot']),
                        save=save, before_attempt=lambda _: guard())
            result = {'position': position, 'real': prepared['real'], 'status': 'completed', 'raw': value[0],
                      'usage': value[1], 'diagnostics': value.diagnostics, 'started_epoch': started, 'finished_epoch': time.time(),
                      'experiment_manifest': {'kind': 'CR1_experimental_invocation_not_production_history',
                            'input_hash': request['input_hash'], 'provider_request_hash': request['request_hash'],
                            'source_hash': context['source_hash'], 'condition': position['condition']}}
            once(root / 'results' / (slot.replace('/', '--') + '.json'), result)
            completed += 1
            print(encoded({'phase': phase, 'position': slot, 'main_completed': completed, 'physical_calls': len(ledger.rows()), 'real': prepared['real']}), flush=True)
            if completed == 24:
                require(all(next(i for i in prepared['items'] if i['key'] == p['item'])['case_id'] in smoke_cases
                            for p in prepared['positions'][:24]), 'cr1_smoke_order')
                once(root / 'smoke.json', {'status': 'passed_protocol_and_audit', 'main_completed': 24, 'quality': 'retained_for_later_analysis'})
        protect()
        once(root / 'terminal.json', {'status': 'completed', 'main_completed': completed, 'selection_completed': len(contexts),
                                     'real': prepared['real'], 'finished_epoch': time.time()})
    except BaseException as error:
        code = str(error)
        if not re.fullmatch('[a-z][a-z0-9_]{0,100}', code): code = type(error).__name__
        once(root / 'terminal.json', {'status': 'interrupted' if isinstance(error, asyncio.CancelledError) else 'stopped',
                 'phase': phase, 'error_code': code, 'main_completed': completed, 'selection_completed': len(contexts),
                 'real': prepared['real'], 'finished_epoch': time.time()})
        raise
    finally:
        for engine in engines.values(): close(engine)
    return verify(root)


class SyntheticTransport:
    def __init__(self, raw, errors=()): self.raw, self.errors = raw, list(errors)
    async def complete(self, model, messages):
        require(not model.get('encrypted_key'), 'cr1_synthetic_credentials_forbidden')
        if self.errors:
            error = self.errors.pop(0)
            if isinstance(error, BaseException): raise error
            if isinstance(error, str): return ModelCompletion(error, {'total_tokens': 0}, {'finish_reason': 'stop'})
        return ModelCompletion(self.raw, {'total_tokens': 0}, {'finish_reason': 'stop', 'response_model': 'simulated', 'system_fingerprint': 'simulation-only'})


def verify(root):
    prepared = check_freeze(root)
    root = Path(root)
    rows = Receipts(root / 'receipts').rows()
    require(all('terminal' in row for row in rows), 'cr1_real_receipt_gap')
    terminal = read(root / 'terminal.json')
    require(terminal['status'] == 'completed' and terminal['main_completed'] == 144 and terminal['selection_completed'] == 24, 'cr1_real_batch_incomplete')
    require(all(row['real'] == prepared['real'] for row in rows), 'cr1_simulation_real_mixed')
    require(sum(row['terminal']['status'] == 'completed' for row in rows) == 168, 'cr1_logical_success_count')
    for position in prepared['positions']:
        result = read(root / 'results' / (position['id'].replace('/', '--') + '.json'))
        observed = [r for r in rows if r['slot'] == position['id']]
        require(1 <= len(observed) <= 4 and observed[-1]['terminal']['status'] == 'completed', 'cr1_main_physical_count')
        require(result['experiment_manifest']['input_hash'] == observed[-1]['input_hash'] and
                result['experiment_manifest']['provider_request_hash'] == observed[-1]['request_hash'] and
                result['raw'] == observed[-1]['transport']['raw'], 'cr1_real_final_input_mismatch')
    summary = {'status': 'completed_engineering_verification', 'real': prepared['real'], 'main_positions': 144,
               'shared_selections': 24, 'physical_calls': len(rows), 'retries': len(rows) - 168,
               'effect': 'requires_full_machine_analysis_and_user_review', 'stage_complete': False}
    atomic_json(root / 'engineering-verification.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'run', 'verify', 'report'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--real', action='store_true')
    args = parser.parse_args()
    root = base.confined(args.root or OUT / ('real-v1' if args.real or args.action == 'run' else 'real-runner-simulation-v1'))
    if args.action == 'run':
        require(read(root / 'prepared.json')['real'], 'cr1_run_requires_real_pack')
        result = asyncio.run(execute(root))
    else:
        with offline() as audit:
            if args.action == 'prepare': result = asyncio.run(prepare(root, real=args.real))
            elif args.action == 'simulate':
                require(not read(root / 'prepared.json')['real'], 'cr1_simulate_requires_blank_credentials')
                result = asyncio.run(execute(root))
            elif args.action == 'verify': result = verify(root)
            else:
                from scripts.dialogue_diagnosis_analysis import report
                result = report(root)
            atomic_json(root / (args.action + '-network-audit.json'), dict(audit))
    print(encoded(result))


if __name__ == '__main__': main()
