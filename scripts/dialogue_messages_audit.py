"""Read-only seal and receipt verification for the Messages recovery package.

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
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-recovery-2')
ORIGINAL = OUT.with_name('CR3-A.1-real-consumption-ablation')
PREVIOUS = OUT.with_name('CR3-A.1-real-consumption-ablation-continuation')
STAGE = 'CR3-A.1-messages-recovery-2'
PROFILE = 'anthropic-messages-v1'


def require(value, code):
    if not value:
        raise ValueError('messages_audit_' + code)


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
    allowed = {(ORIGINAL / 'closure.json').resolve(), (PREVIOUS / 'closure.json').resolve()}
    require(isinstance(seals, list) and len(seals) == 2, 'origin_inventory')
    paths = [Path(row['path']).resolve() for row in seals]
    require(len(set(paths)) == 2 and set(paths) == allowed, 'origin_outside_path')
    for row, path in zip(seals, paths):
        require(digest(path.read_bytes()) == row['sha256'], 'origin_seal_changed')
    from scripts.dialogue_communication_real_audit import verify as verify_original
    from scripts.dialogue_communication_continuation_audit import verify as verify_previous
    first, previous = verify_original(ORIGINAL), verify_previous(PREVIOUS)
    require(first.get('passed') is True and previous.get('passed') is True, 'origin_not_verified')
    return {'original': first, 'previous': previous}


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
    system, conversation = [], []
    for message in messages:
        require(isinstance(message, dict) and isinstance(message.get('content'), str), 'logical_message_shape')
        role = message.get('role')
        require(role in ('system', 'user', 'assistant'), 'logical_role')
        if role == 'system':
            require(not conversation, 'interleaved_system')
            system.append({'type': 'text', 'text': message['content']})
        else:
            conversation.append({'role': role, 'content': message['content']})
    require(bool(conversation) and conversation[0]['role'] == 'user', 'conversation_shape')
    require(payload.get('messages') == conversation, 'wire_messages_changed')
    require(payload.get('system', []) == system, 'wire_system_changed')
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


def batch(root):
    """Verify new physical calls only; predecessor calls remain separately owned."""
    root = Path(root).resolve()
    state = read(child(root, 'state.json'))
    require(state.get('real') is True and state.get('status') in ('completed', 'stopped'), 'unsettled_batch')
    receipts = child(root, 'receipts')
    ids = _receipt_ids(receipts, 'reserve')
    require(_receipt_ids(receipts, 'transport') == ids == _receipt_ids(receipts, 'terminal'), 'unsettled_receipt')
    require('physical_calls' not in state or state['physical_calls'] == len(ids), 'state_call_count_changed')
    imports = imported_success_ids(root)
    rows, slots = {}, {}
    for ident in sorted(ids):
        reserve = read(child(receipts, ident + '.reserve.json'))
        terminal = read(child(receipts, ident + '.terminal.json'))
        transport = read(child(receipts, ident + '.transport.json'))
        _real(reserve)
        _real(terminal)
        _real(transport, transport=True)
        require(reserve.get('id') == ident, 'receipt_identity')
        require(fingerprint(reserve) == terminal.get('reservation_hash'), 'reservation_binding')
        attempt = terminal.get('runtime_attempt', {})
        require(terminal.get('status') in ('completed', 'failed', 'cancelled', 'interrupted') and
                attempt.get('status') == terminal['status'], 'unfinished_attempt')
        payload = reserve.get('wire_request')
        require(isinstance(payload, dict) and fingerprint(payload) == reserve.get('request_hash') == attempt.get('request_hash'), 'wire_binding')
        require(reserve.get('wire_protocol') == PROFILE and reserve.get('protocol') == PROFILE, 'wrong_protocol')
        require('max_tokens' not in payload and 'max_output_tokens' not in payload and
                'effective_output_max_tokens' in reserve and reserve['effective_output_max_tokens'] is None, 'output_limit_returned')
        require('effective_timeout_seconds' in reserve and reserve['effective_timeout_seconds'] is None and
                'timeout_seconds' in attempt and attempt['timeout_seconds'] is None and
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
        if terminal['status'] == 'completed':
            require(transport.get('status') == 'returned' and isinstance(transport.get('raw'), str) and
                    bool(transport['raw'].strip()), 'completed_transport_missing')
        row = {'reserve': reserve, 'terminal': terminal, 'transport': transport}
        rows[ident] = row
        slots.setdefault(reserve['slot'], []).append(row)
    for attempts in slots.values():
        require(len(attempts) <= 4 and len({r['reserve']['request_hash'] for r in attempts}) == 1, 'retry_changed_or_exceeded')
        successful = [i for i, row in enumerate(attempts) if row['terminal']['status'] == 'completed']
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
        require(contract.get('max_tokens_mode') == 'omit' and contract.get('timeout_policy') == 'none' and
                'effective_max_tokens' in contract and contract['effective_max_tokens'] is None and
                'effective_timeout_seconds' in contract and contract['effective_timeout_seconds'] is None, 'manifest_limit_returned')
    auxiliary = 0
    for path in child(root, 'auxiliary').glob('*.json'):
        value = read(child(root / 'auxiliary', path.name))
        _real(value)
        require(value.get('protocol') == PROFILE and isinstance(value.get('calls'), list) and bool(value['calls']), 'auxiliary_shape')
        require(all(ident in rows and rows[ident]['reserve']['slot'] == path.stem + '/joint' for ident in value['calls']), 'auxiliary_call_changed')
        last = rows[value['calls'][-1]]
        require(last['terminal']['status'] == 'completed' and last['transport'].get('raw') == value.get('raw'), 'auxiliary_reply_changed')
        auxiliary += 1
    return {'audit_passed': True, 'batch_complete': state['status'] == 'completed',
            'state': state, 'physical_calls': len(ids),
            'completed_calls': sum(r['terminal']['status'] == 'completed' for r in rows.values()),
            'failed_calls': sum(r['terminal']['status'] == 'failed' for r in rows.values()),
            'interrupted_or_cancelled_calls': sum(r['terminal']['status'] in ('interrupted', 'cancelled') for r in rows.values()),
            'logical_conditions': len(values), 'unique_main': sum(v['status'] == 'completed' for v in values.values()),
            'aliases': sum(v['status'] == 'aliased' for v in values.values()), 'auxiliary_completed': auxiliary,
            'unsettled_receipts': 0, 'success_replays': 0, 'output_cap_sent': False,
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
    require(not seal.get('stage_complete') or
            (domain['batch_complete'] and seal.get('user_review') == 'completed'), 'premature_completion')
    if seal.get('stage_complete'):
        require(domain['logical_conditions'] == 66 and domain['auxiliary_completed'] == 22,
                'completed_batch_missing_conditions')
    return {'stage': STAGE, 'passed': True, 'final_seal': final,
            'stage_complete': seal.get('stage_complete', False),
            'source_files': len(seal['source_hashes']), 'evidence_files': len(seal['evidence_hashes']),
            'origins_unchanged': all(v.get('passed') for v in origins.values()),
            'real_batch_status': domain['state']['status'], 'new_physical_calls': domain['physical_calls'],
            'new_unique_main': domain['unique_main'], 'new_aliases': domain['aliases'],
            'local_generation_timeout': None, 'output_cap_sent': False,
            'writes': 0, 'generation_calls': 0, 'worktree_checked': worktree}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=OUT)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.worktree), ensure_ascii=False))
