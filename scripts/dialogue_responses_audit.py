"""Read-only seal and receipt verification for the Responses recovery package.

Only standard-library and predecessor read-only audit modules are imported.
No runtime, provider client, report writer, or execution entry point is loaded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-responses-3')
ORIGINAL = OUT.with_name('CR3-A.1-real-consumption-ablation')
PREVIOUS = OUT.with_name('CR3-A.1-real-consumption-ablation-continuation')
MESSAGES = OUT.with_name('CR3-A.1-real-consumption-ablation-recovery-2')
STAGE = 'CR3-A.1-responses-recovery-3'
PROFILE = 'openai-responses-v1'


def require(value, code):
    if not value:
        raise ValueError('responses_audit_' + code)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def child(root, name):
    require(isinstance(name, str) and bool(name), 'invalid_path')
    value = (Path(root) / name).resolve()
    require(value != Path(root).resolve() and value.is_relative_to(Path(root).resolve()), 'outside_path')
    return value


def verify_origins(seals):
    allowed = {(path / 'closure.json').resolve() for path in (ORIGINAL, PREVIOUS, MESSAGES)}
    require(isinstance(seals, list) and len(seals) == 3, 'origin_inventory')
    paths = [Path(row['path']).resolve() for row in seals]
    require(len(set(paths)) == 3 and set(paths) == allowed, 'origin_outside_path')
    for row, path in zip(seals, paths):
        require(digest(path.read_bytes()) == row['sha256'], 'origin_seal_changed')
    from scripts.dialogue_communication_real_audit import verify as verify_original
    from scripts.dialogue_communication_continuation_audit import verify as verify_previous
    from scripts.dialogue_messages_audit import verify as verify_messages
    first, previous, messages = verify_original(ORIGINAL), verify_previous(PREVIOUS), verify_messages(MESSAGES)
    require(all(value.get('passed') is True for value in (first, previous, messages)), 'origin_not_verified')
    return {'original': first, 'previous': previous, 'messages': messages}


def _receipt_ids(receipts, suffix):
    ids = set()
    for path in receipts.glob('*.' + suffix + '.json'):
        name = path.name[:-len('.' + suffix + '.json')]
        require(bool(re.fullmatch('[0-9]{4,}', name)), 'invalid_receipt_id')
        ids.add(name)
    return ids


def _mapped_messages(messages, payload):
    """Check text/order mapping without importing the provider implementation."""
    require(isinstance(messages, list) and isinstance(payload, dict), 'wire_shape')
    require(bool(messages) and any(message.get('role') == 'user' for message in messages if isinstance(message, dict)),
            'conversation_shape')
    for message in messages:
        require(isinstance(message, dict) and isinstance(message.get('content'), str), 'logical_message_shape')
        role = message.get('role')
        require(role in ('system', 'user', 'assistant'), 'logical_role')
    require(payload.get('input') == messages, 'wire_input_changed')
    require('instructions' not in payload, 'hidden_instructions_added')
    require(payload.get('store') is False, 'wire_store_changed')
    require(payload.get('stream') is False, 'wire_stream_changed')


def _real(value, *, transport=False):
    require(value.get('real') is True, 'mock_mixed_with_real')
    simulation = value.get('simulation', False) if transport else value.get('simulation')
    require(simulation is False, 'mock_mixed_with_real')


def imported_success_ids(root):
    path = child(root, 'imports.json')
    if not path.exists():
        return {'results': set(), 'auxiliary': set()}
    imports = read(path)
    parent = (ORIGINAL / 'real-v1').resolve()
    require(Path(imports.get('parent_root', '')).resolve() == parent, 'imports_wrong_parent')
    result = {}
    for category in ('results', 'auxiliary'):
        entries = imports.get(category, {})
        require(isinstance(entries, dict), 'imports_shape')
        for ident, pointer in entries.items():
            source = Path(pointer['path']).resolve()
            require(source.parent == child(parent, category) and source.stem == ident and source.suffix == '.json',
                    'import_outside_path')
            require(digest(source.read_bytes()) == pointer['sha256'], 'import_changed')
        result[category] = set(entries)
    return result


def documented_interruption(root, state, ids, transports, terminals):
    """Recognize one bound user-stop gap, never invent a native terminal."""
    require(transports <= ids and terminals <= ids, 'unsettled_receipt')
    absent_transport, absent_terminal = ids - transports, ids - terminals
    path = child(root, 'user-cancellation-reconciliation.json')
    if not absent_transport and not absent_terminal:
        require(not state.get('external_reconciliation') and not state.get('native_audit_gap'),
                'interruption_without_native_gap')
        return None
    require(bool(ids) and absent_transport == absent_terminal == {max(ids)}, 'unsettled_receipt')
    require(path.is_file(), 'unsettled_receipt')
    require(state.get('status') == 'stopped' and state.get('reason') == 'user_cancelled' and
            state.get('external_reconciliation') is True and state.get('not_original_runner_finally_state') is True and
            state.get('reconciled_by') == 'manual_external_termination' and
            state.get('reconciliation') == path.name and state.get('native_audit_gap') == 1,
            'interruption_state_not_user_stop')
    receipt_id = max(ids)
    require(state.get('provider_outcome_unknown') == [receipt_id], 'interruption_unknown_binding')
    reserve = read(child(root, 'receipts/' + receipt_id + '.reserve.json'))
    document = read(path)
    require(document.get('version') == 'responses-user-cancel-v1' and
            document.get('status') == 'provider_outcome_unknown' and document.get('request') == 'user_cancelled' and
            document.get('user_requested_stop') is True, 'interruption_authorization_missing')
    require(document.get('receipt_id') == document.get('native_gap_receipt_id') == receipt_id and
            document.get('reservation_hash') == fingerprint(reserve) and document.get('slot') == reserve.get('slot'),
            'interruption_reservation_changed')
    require(document.get('native_audit_gap') == 1 and document.get('native_terminal_missing') is True and
            document.get('transport_receipt_missing') is True and document.get('native_receipts_preserved') is True and
            document.get('no_synthetic_native_terminal') is True and document.get('native_state_not_synthesized') is True,
            'interruption_native_evidence_changed')
    require(document.get('provider_outcome_unknown') is True and document.get('usage_unknown') is True and
            document.get('last_usage_unknown') is True and document.get('further_dispatch_prohibited') is True,
            'interruption_unknown_not_preserved')
    require(document.get('reserved_calls') == len(ids) and document.get('settled_calls') == len(ids) - 1,
            'interruption_count_changed')
    missing = document.get('unsettled')
    require(isinstance(missing, list) and len(missing) == 1 and missing[0].get('id') == receipt_id and
            missing[0].get('reservation_hash') == fingerprint(reserve) and
            missing[0].get('terminal_present') is False and missing[0].get('transport_present') is False and
            missing[0].get('supplier_outcome') == missing[0].get('usage') == 'unknown',
            'interruption_missing_receipt_changed')
    require(document.get('local_process_not_running') is True and document.get('local_executor_process_absent') is True and
            type(document.get('unresolved_local_processes')) is int and document['unresolved_local_processes'] == 0 and
            isinstance(document.get('process_check'), str) and bool(document['process_check'].strip()),
            'interruption_process_not_proven')
    authorization_path = child(root.parent, 'user-stop-authorization.json')
    require(authorization_path.is_file(), 'interruption_authorization_missing')
    authorization = read(authorization_path)
    require(authorization.get('request') == 'user_cancelled' and
            isinstance(authorization.get('user_message'), str) and bool(authorization['user_message'].strip()) and
            isinstance(authorization.get('recorded_at'), str) and bool(authorization['recorded_at']),
            'interruption_authorization_missing')
    proof = authorization.get('local_process_proof', {})
    local_proof = document.get('local_process_proof', {})
    require(proof.get('status') == 'not_running' and Path(proof.get('batch_root', '')).resolve() == root and
            type(proof.get('matched_processes')) is int and proof['matched_processes'] == 0 and
            str(proof.get('session_id')) == str(local_proof.get('session_id')) and
            local_proof.get('owned_python_process_matches') == [] and
            isinstance(proof.get('checked_at'), str) and bool(proof['checked_at']) and
            isinstance(proof.get('method'), str) and bool(proof['method'].strip()),
            'interruption_process_not_proven')
    return {'receipt_id': receipt_id, 'reservation_hash': fingerprint(reserve),
            'provider_outcome': 'unknown', 'usage': 'unknown', 'native_terminal_synthesized': False,
            'reported_failed_calls': document.get('known_failed_calls'),
            'reported_successful_calls': document.get('successful_calls')}


def batch(root):
    """Verify new physical calls only; predecessor calls remain separately owned."""
    root = Path(root).resolve()
    state = read(child(root, 'state.json'))
    require(state.get('real') is True and state.get('status') in ('completed', 'stopped'), 'unsettled_batch')
    receipts = child(root, 'receipts')
    ids = _receipt_ids(receipts, 'reserve')
    gap = documented_interruption(root, state, ids, _receipt_ids(receipts, 'transport'), _receipt_ids(receipts, 'terminal'))
    require('physical_calls' not in state or state['physical_calls'] == len(ids), 'state_call_count_changed')
    imports = imported_success_ids(root)
    rows, slots = {}, {}
    for ident in sorted(ids):
        reserve = read(child(receipts, ident + '.reserve.json'))
        missing = gap is not None and ident == gap['receipt_id']
        terminal = None if missing else read(child(receipts, ident + '.terminal.json'))
        transport = None if missing else read(child(receipts, ident + '.transport.json'))
        _real(reserve)
        require(reserve.get('id') == ident, 'receipt_identity')
        attempt = None if missing else terminal.get('runtime_attempt', {})
        if not missing:
            _real(terminal)
            _real(transport, transport=True)
            require(fingerprint(reserve) == terminal.get('reservation_hash'), 'reservation_binding')
            require(terminal.get('status') in ('completed', 'failed', 'cancelled', 'interrupted') and
                    attempt.get('status') == terminal['status'], 'unfinished_attempt')
        payload = reserve.get('wire_request')
        require(isinstance(payload, dict) and fingerprint(payload) == reserve.get('request_hash'), 'wire_binding')
        if not missing:
            require(reserve['request_hash'] == attempt.get('request_hash'), 'wire_binding')
        require(reserve.get('wire_protocol') == PROFILE and reserve.get('protocol') == PROFILE, 'wrong_protocol')
        require('max_tokens' not in payload and 'max_output_tokens' not in payload and
                'effective_output_max_tokens' in reserve and reserve['effective_output_max_tokens'] is None, 'output_limit_returned')
        require('effective_timeout_seconds' in reserve and reserve['effective_timeout_seconds'] is None, 'local_timeout_returned')
        if not missing:
            require('timeout_seconds' in attempt and attempt['timeout_seconds'] is None and
                    attempt.get('local_timeout_policy') == 'none', 'local_timeout_returned')
        model = reserve.get('model', {})
        require(isinstance(model, dict) and '_request_deadline' not in model, 'deadline_returned')
        require(not model.get('encrypted_key'), 'credential_in_public_evidence')
        require(payload.get('model') == model.get('name'), 'wire_model_changed')
        require(fingerprint(reserve.get('messages')) == reserve.get('input_hash'), 'logical_input_binding')
        _mapped_messages(reserve['messages'], payload)
        require(reserve.get('kind') in ('main', 'joint') and isinstance(reserve.get('slot'), str) and
                reserve['slot'].endswith('/' + reserve['kind']), 'slot_kind')
        ident_in_slot = reserve['slot'][:-len('/' + reserve['kind'])]
        require(ident_in_slot not in imports['results' if reserve['kind'] == 'main' else 'auxiliary'], 'imported_success_replayed')
        if terminal is not None and terminal['status'] == 'completed':
            require(transport.get('status') == 'returned' and isinstance(transport.get('raw'), str) and
                    bool(transport['raw'].strip()), 'completed_transport_missing')
        row = {'reserve': reserve, 'terminal': terminal, 'transport': transport}
        rows[ident] = row
        slots.setdefault(reserve['slot'], []).append(row)
    for attempts in slots.values():
        require(len(attempts) <= 4 and len({r['reserve']['request_hash'] for r in attempts}) == 1, 'retry_changed_or_exceeded')
        successful = [i for i, row in enumerate(attempts) if row['terminal'] is not None and row['terminal']['status'] == 'completed']
        require(not successful or successful == [len(attempts) - 1], 'success_replayed')
    values = {}
    for path in child(root, 'results').glob('*.json'):
        value = read(child(root / 'results', path.name))
        require(value.get('id') == path.stem, 'result_identity')
        _real(value)
        require(value.get('protocol') == PROFILE, 'result_wrong_protocol')
        require(path.stem not in imports['results'], 'imported_success_replayed')
        values[path.stem] = value
    used_main = set()
    for value in values.values():
        if value.get('status') == 'aliased':
            canonical = values.get(value.get('canonical_id'))
            require(canonical and canonical.get('status') == 'completed' and
                    canonical['group'] == value['group'] and
                    all(canonical[k] == value[k] for k in ('request_hash', 'messages', 'reply')), 'invalid_alias')
            require('manifest' not in value and not value.get('calls') and
                    value.get('actual_manifest_from') == canonical['id'], 'alias_fake_evidence')
            continue
        require(value.get('status') == 'completed', 'result_not_completed')
        calls = value.get('calls')
        require(isinstance(calls, list) and bool(calls) and len(calls) == len(set(calls)) and all(c in rows for c in calls), 'result_calls')
        require(all(rows[c]['terminal'] is not None for c in calls), 'result_missing_native_terminal')
        require(not used_main.intersection(calls), 'main_receipt_reused')
        used_main.update(calls)
        for ident in calls:
            reserve = rows[ident]['reserve']
            require(reserve['kind'] == 'main' and reserve['slot'] == value['id'] + '/main' and
                    reserve['messages'] == value['messages'] and reserve['request_hash'] == value['request_hash'], 'result_receipt_changed')
        actual = rows[calls[-1]]
        require(actual['terminal']['status'] == 'completed' and actual['transport'].get('raw') == value['reply'], 'result_reply_changed')
        manifest = value.get('manifest', {})
        require(manifest.get('input_snapshot_hash') == fingerprint(value['messages']), 'manifest_input_changed')
        contract = manifest.get('provider_request_contract', {})
        require(contract.get('profile') == PROFILE and contract.get('wire_hash') == value['request_hash'] and
                contract.get('logical_input_hash') == fingerprint(value['messages']), 'manifest_wire_changed')
        require(contract.get('max_tokens_mode') == 'omit' and contract.get('max_output_tokens_mode') == 'omit' and
                contract.get('timeout_policy') == 'none' and contract.get('store') is False and contract.get('stream') is False and
                'effective_max_tokens' in contract and contract['effective_max_tokens'] is None and
                'effective_timeout_seconds' in contract and contract['effective_timeout_seconds'] is None, 'manifest_limit_returned')
    auxiliary = 0
    for path in child(root, 'auxiliary').glob('*.json'):
        value = read(child(root / 'auxiliary', path.name))
        _real(value)
        require(value.get('protocol') == PROFILE and isinstance(value.get('calls'), list) and bool(value['calls']), 'auxiliary_shape')
        require(all(ident in rows and rows[ident]['reserve']['slot'] == path.stem + '/joint' for ident in value['calls']), 'auxiliary_call_changed')
        last = rows[value['calls'][-1]]
        require(last['terminal'] is not None and last['terminal']['status'] == 'completed' and
                last['transport'].get('raw') == value.get('raw'), 'auxiliary_reply_changed')
        auxiliary += 1
    settled = [row for row in rows.values() if row['terminal'] is not None]
    if gap:
        require(gap['reported_failed_calls'] == sum(row['terminal']['status'] == 'failed' for row in settled) and
                gap['reported_successful_calls'] == sum(row['terminal']['status'] == 'completed' for row in settled),
                'interruption_count_changed')
    return {'audit_passed': gap is None, 'native_audit_complete': gap is None,
            'verification_status': 'audited_with_documented_interruption' if gap else 'native_audit_complete',
            'local_accounting_reconciled': True, 'batch_complete': state['status'] == 'completed',
            'state': state, 'physical_calls': len(ids),
            'physical_calls_confirmation': 'conservative_reservations' if gap else 'settled_native_attempts',
            'confirmed_terminal_calls': len(settled),
            'completed_calls': sum(r['terminal']['status'] == 'completed' for r in settled),
            'failed_calls': sum(r['terminal']['status'] == 'failed' for r in settled),
            'interrupted_or_cancelled_calls': sum(r['terminal']['status'] in ('interrupted', 'cancelled') for r in settled),
            'logical_conditions': len(values), 'unique_main': sum(v['status'] == 'completed' for v in values.values()),
            'aliases': sum(v['status'] == 'aliased' for v in values.values()), 'auxiliary_completed': auxiliary,
            'unsettled_receipts': int(gap is not None), 'native_audit_gaps': int(gap is not None),
            'unknown_calls': int(gap is not None), 'unresolved_local_processes': 0,
            'documented_interruption': gap, 'success_replays': 0, 'output_cap_sent': False,
            'local_generation_timeout': None, 'writes': 0, 'generation_calls': 0}


def verify(root=OUT, worktree=False):
    root = Path(root).resolve()
    final = (root / 'closure.json').is_file()
    seal = read(child(root, 'closure.json' if final else 'checkpoint.json'))
    require(seal.get('stage') == STAGE, 'wrong_stage')
    for name, expected in seal['evidence_hashes'].items():
        require(digest(child(root, name).read_bytes()) == expected, 'evidence_changed:' + name)
    if final:
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p != root / 'closure.json'}
        require(actual == set(seal['evidence_hashes']), 'evidence_inventory_changed')
    with zipfile.ZipFile(child(root, seal['source_archive'])) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and set(names) == set(seal['source_hashes']), 'source_inventory_changed')
        for name, expected in seal['source_hashes'].items():
            source = child(ROOT, name)
            require(digest(archive.read(name)) == expected, 'archived_source_changed:' + name)
            if worktree:
                require(digest(source.read_bytes()) == expected, 'worktree_changed:' + name)
    origins = verify_origins(seal['origin_seals'])
    domain = batch(child(root, 'real-v1'))
    if domain['native_audit_gaps']:
        require(seal.get('status') == 'user_stopped' and seal.get('stage_complete') is False,
                'interruption_seal_not_user_stopped')
    require(not seal.get('stage_complete') or
            (domain['native_audit_complete'] and domain['batch_complete'] and seal.get('user_review') == 'completed'),
            'premature_completion')
    if seal.get('stage_complete'):
        require(domain['logical_conditions'] == 66 and domain['auxiliary_completed'] == 22,
                'completed_batch_missing_conditions')
    return {'stage': STAGE, 'passed': True, 'final_seal': final,
            'stage_complete': seal.get('stage_complete', False),
            'source_files': len(seal['source_hashes']), 'evidence_files': len(seal['evidence_hashes']),
            'origins_unchanged': all(v.get('passed') for v in origins.values()),
            'real_batch_status': domain['state']['status'], 'new_physical_calls': domain['physical_calls'],
            'new_unique_main': domain['unique_main'], 'new_aliases': domain['aliases'],
            'verification_status': domain['verification_status'], 'native_audit_complete': domain['native_audit_complete'],
            'audit_passed': domain['audit_passed'],
            'native_audit_gaps': domain['native_audit_gaps'], 'documented_unknown': domain['unknown_calls'],
            'unsettled_receipts': domain['unsettled_receipts'], 'unresolved_local_processes': domain['unresolved_local_processes'],
            'local_generation_timeout': None, 'output_cap_sent': False,
            'writes': 0, 'generation_calls': 0, 'worktree_checked': worktree}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=OUT)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.worktree), ensure_ascii=False))
