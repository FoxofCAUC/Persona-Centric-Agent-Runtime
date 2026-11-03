"""Reports for the independent Responses recovery; never writes predecessors.

The full plan includes preserved OpenAI-protocol observations. New Responses
observations are stratified: no cross-protocol output difference is attributed
to the communication specification or to transport improvements.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import encoded, fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.provider_compat import build_request as build_legacy_request
from scripts.dialogue_responses_transport import build_responses_request
from scripts.dialogue_anthropic_transport import build_messages_request, transport_contract as messages_contract
from scripts import dialogue_communication_continuation_analysis as reading

OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-responses-3')
ORIGINAL = OUT.with_name('CR3-A.1-real-consumption-ablation')
CONTINUATION = OUT.with_name('CR3-A.1-real-consumption-ablation-continuation')
PREVIOUS_MESSAGES = OUT.with_name('CR3-A.1-real-consumption-ablation-recovery-2')
VERSION = 'cr3-responses-analysis-v1'
RESPONSES = 'openai-responses-v1'
ANTHROPIC = 'anthropic-messages-v1'
LEGACY = 'openai-compatible-v1'
read, require, digest_file = reading.read, reading.require, reading.digest_file


def confined(root):
    root = Path(root).resolve()
    require(root.is_relative_to(OUT.resolve()) and root != OUT.resolve(), 'output_outside_responses_recovery')
    require(not (OUT / 'closure.json').exists(), 'responses_recovery_sealed')
    return root


def load_bundle(root):
    root = Path(root).resolve()
    manifest = read(root / 'imports.json')
    parent = (ORIGINAL / 'real-v1').resolve()
    previous = (CONTINUATION / 'real-v1').resolve()
    previous_messages = (PREVIOUS_MESSAGES / 'real-v1').resolve()
    require(Path(manifest['parent_root']).resolve() == parent, 'wrong_parent')
    history = [Path(p).resolve() for p in manifest['history_roots']]
    require(history == [parent, previous, previous_messages], 'history_roots_changed')
    for item in manifest.get('prior_packages', []):
        path = Path(item['closure_path']).resolve()
        require(path in (parent.parent / 'closure.json', previous.parent / 'closure.json',
                         previous_messages.parent / 'closure.json'), 'prior_seal_outside')
        require(digest_file(path) == item['closure_sha256'], 'prior_seal_changed')
    if manifest.get('parent_closure'):
        reading._pointer(root, manifest['parent_closure'], parent.parent)
    prepared, current = read(parent / 'prepared.json'), read(root / 'prepared.json')
    contract = read(root / 'transport-contract.json')
    protocol, materials = read(root / 'protocol.json'), read(root / 'materials.json')
    require(protocol == read(parent / 'protocol.json'), 'protocol_changed')
    require(materials == read(parent / 'materials.json'), 'materials_changed')
    records, auxiliary, origins = {}, {}, {}
    for category, target in (('results', records), ('auxiliary', auxiliary)):
        for ident, pointer in manifest.get(category, {}).items():
            path = reading._pointer(root, pointer, parent)
            value = read(path)
            require(ident == path.stem and (category != 'results' or value['id'] == ident), 'import_identity')
            target[ident], origins[(category, ident)] = value, parent
        for path in sorted((root / category).glob('*.json')):
            value, ident = read(path), path.stem
            require(ident not in target, 'successful_position_replayed')
            if category == 'results': require(value['id'] == ident, 'result_identity')
            target[ident], origins[(category, ident)] = value, root
    receipts = {}
    for origin in history + [root]:
        owner = read(origin / 'prepared.json')
        for path in sorted((origin / 'receipts').glob('*.reserve.json')):
            ident = path.name.split('.')[0]
            reserve = read(path)
            qualified = str(origin) + '::' + owner['batch'] + '::' + ident
            require(qualified not in receipts, 'receipt_duplicate')
            row = {'origin_root': str(origin), 'batch': owner['batch'], 'receipt_id': ident,
                   'qualified_id': qualified, 'reserve': reserve}
            require(fingerprint(reserve['messages']) == reserve['input_hash'], 'receipt_input_changed')
            wire_body(reserve, contract)
            for part in ('transport', 'terminal'):
                file = path.with_name(ident + '.' + part + '.json')
                row[part] = read(file) if file.exists() else None
            require(reserve['real'] is owner['real'], 'receipt_mode')
            if row['terminal']:
                require(fingerprint(reserve) == row['terminal']['reservation_hash'], 'receipt_binding')
                require(reserve['request_hash'] == row['terminal']['runtime_attempt']['request_hash'], 'attempt_binding')
            receipts[qualified] = row
    for category, values in (('results', records), ('auxiliary', auxiliary)):
        for ident, value in values.items():
            expected = prepared['real'] if origins[(category, ident)] == parent else current['real']
            require(value.get('real') is expected and value.get('simulation') is not expected, 'mixed_record_modes')
    bundle = {'root': root, 'parent': parent, 'previous': previous, 'previous_messages': previous_messages, 'history_roots': history,
            'imports': manifest, 'prepared': prepared, 'current_prepared': current,
            'protocol': protocol, 'materials': materials, 'results': records, 'auxiliary': auxiliary,
            'origins': origins, 'receipts': receipts, 'real': current['real'],
            'transport_contract': contract}
    bundle['user_cancellation'] = validate_user_cancellation(bundle)
    return bundle


def validate_user_cancellation(bundle):
    """Bind a user-stop note to missing evidence; never create native evidence."""
    root = bundle['root']; path = root / 'user-cancellation-reconciliation.json'
    state = read(root / 'state.json') if (root / 'state.json').exists() else {}
    if not path.exists():
        require(state.get('reason') != 'user_cancelled' and not state.get('not_original_runner_finally_state'),
                'user_stop_proof_missing')
        return None
    note = read(path)
    require(note.get('version') == 'responses-user-cancel-v1' and note.get('status') == 'provider_outcome_unknown',
            'user_stop_note_invalid')
    for key in ('user_requested_stop', 'local_process_not_running', 'local_executor_process_absent',
                'native_receipts_preserved', 'no_synthetic_native_terminal', 'further_dispatch_prohibited'):
        require(note.get(key) is True, 'user_stop_proof_incomplete')
    require(isinstance(note.get('process_check'), str) and bool(note['process_check'].strip()), 'user_stop_process_check_missing')
    authorization = read(root.parent / 'user-stop-authorization.json')
    proof = authorization.get('local_process_proof') or {}
    require(authorization.get('request') == 'user_cancelled' and isinstance(authorization.get('user_message'), str)
            and bool(authorization['user_message'].strip()), 'user_stop_authorization_missing')
    require(proof.get('status') == 'not_running' and type(proof.get('matched_processes')) is int
            and proof['matched_processes'] == 0 and Path(proof.get('batch_root', '')).resolve() == root.resolve()
            and isinstance(proof.get('method'), str) and bool(proof['method'].strip()), 'user_stop_process_proof_invalid')
    rows = [r for r in bundle['receipts'].values() if r['origin_root'] == str(root)]
    pending = [r for r in rows if not r['terminal']]
    require(len(pending) == 1 and pending[0]['transport'] is None, 'user_stop_native_gap_changed')
    row = pending[0]
    require(row['reserve']['ordinal'] == max(r['reserve']['ordinal'] for r in rows), 'user_stop_not_last_reservation')
    entries = note.get('unsettled')
    require(isinstance(entries, list) and len(entries) == 1, 'user_stop_note_gap_invalid')
    entry = entries[0]
    require(entry.get('id') == row['receipt_id'] == note.get('native_gap_receipt_id')
            and entry.get('reservation_hash') == fingerprint(row['reserve']), 'user_stop_reservation_binding')
    require(entry.get('transport_present') is False and entry.get('terminal_present') is False
            and entry.get('supplier_outcome') == 'unknown' and entry.get('usage') == 'unknown'
            and entry.get('physical_dispatch_confirmation') == 'unknown; reservation conservatively counted',
            'user_stop_unknown_not_preserved')
    actual = {'reserved_calls': len(rows), 'settled_calls': sum(bool(r['terminal']) for r in rows),
              'successful_calls': sum((r['terminal'] or {}).get('status') == 'completed' for r in rows),
              'known_failed_calls': sum((r['terminal'] or {}).get('status') == 'failed' for r in rows),
              'native_audit_gap': 1, 'conservative_combined_reservations': len(bundle['receipts']),
              'combined_settled_successes': sum((r['terminal'] or {}).get('status') == 'completed' for r in bundle['receipts'].values()),
              'combined_settled_failures': sum((r['terminal'] or {}).get('status') == 'failed' for r in bundle['receipts'].values()),
              'combined_unsettled': sum(not r['terminal'] for r in bundle['receipts'].values())}
    require(all(type(note.get(k)) is int and note[k] == v for k, v in actual.items()), 'user_stop_counts_changed')
    require(state.get('status') == 'stopped' and state.get('reason') == 'user_cancelled'
            and state.get('not_original_runner_finally_state') is True
            and state.get('reconciled_by') == 'manual_external_termination'
            and state.get('reconciliation') == path.name and state.get('native_audit_gap') == 1
            and state.get('provider_outcome_unknown') == [row['receipt_id']], 'user_stop_external_state_invalid')
    return {'validated': True, 'local_process_stopped': True, 'provider_outcome': 'unknown',
            'native_audit_gap': 1, 'receipt_id': row['receipt_id'], 'qualified_id': row['qualified_id'],
            'reservation_hash': fingerprint(row['reserve']), 'no_synthetic_native_terminal': True,
            'reconciliation_sha256': digest_file(path),
            'authorization_sha256': digest_file(root.parent / 'user-stop-authorization.json')}


def wire_body(reserve, contract):
    protocol = reserve.get('wire_protocol', LEGACY)
    if protocol == RESPONSES:
        body = build_responses_request(reserve['model'], reserve['messages'], contract)[0]
        require(body == reserve.get('wire_request'), 'responses_wire_changed')
    elif protocol == ANTHROPIC:
        body = build_messages_request(reserve['model'], reserve['messages'], messages_contract())[0]
        require(body == reserve.get('wire_request'), 'prior_messages_wire_changed')
    else:
        require(protocol == LEGACY, 'unknown_wire_protocol')
        body = build_legacy_request(reserve['model'], reserve['messages'])[0]
    require(fingerprint(body) == reserve['request_hash'], 'wire_hash_changed')
    return body, protocol


def response_rows(bundle):
    rows = []
    for ident, value in sorted(bundle['results'].items()):
        actual = reading.canonical(bundle, ident)
        call = reading.receipt(bundle, 'results', actual['id'], actual['calls'][-1])
        terminal, transport, reserve = call['terminal'], call['transport'], call['reserve']
        require(terminal and terminal['status'] == 'completed' and transport, 'successful_receipt_missing')
        require(reserve['request_hash'] == value['request_hash'] and reserve['messages'] == value['messages'], 'actual_input_changed')
        require(transport['raw'] == value['reply'], 'actual_reply_changed')
        require(fingerprint(value['messages']) == actual['manifest']['input_snapshot_hash'], 'manifest_changed')
        body, protocol = wire_body(reserve, bundle['transport_contract'])
        if protocol == RESPONSES:
            recorded = actual['manifest'].get('provider_request_contract') or {}
            require(recorded.get('profile') == RESPONSES and recorded.get('wire_hash') == reserve['request_hash']
                    and recorded.get('logical_input_hash') == fingerprint(value['messages']),
                    'manifest_wire_contract_changed')
        parameters = {k: v for k, v in body.items() if k not in ('messages', 'system', 'input')}
        parameters['wire_protocol'] = protocol
        diagnostics = transport.get('diagnostics') or {}
        metadata = transport.get('provider_response_metadata') or transport.get('response_metadata') or {}
        rows.append({'id': ident, 'canonical_id': actual['id'], 'group': value['group'], 'condition': value['condition'],
                     'status': value['status'], 'origin_root': str(bundle['origins'][('results', ident)]),
                     'wire_protocol': protocol, 'current_protocol_cohort': protocol == RESPONSES,
                     'request_hash': value['request_hash'], 'message_hash': fingerprint(value['messages']),
                     'model': metadata.get('model', diagnostics.get('response_model')),
                     'backend': metadata.get('system_fingerprint', diagnostics.get('system_fingerprint')),
                     'response_id': metadata.get('response_id', diagnostics.get('response_id')),
                     'response_status': metadata.get('status', diagnostics.get('response_status')),
                     'reply': reading.answer(value), 'raw_output_hash': reading.output_hash(value['reply']),
                     'projection': value['view']['projection'], 'material': value['view']['character'],
                     'qualified_calls': [] if value['status'] == 'aliased' else
                         [reading.receipt(bundle, 'results', ident, x)['qualified_id'] for x in value['calls']],
                     'physical_attempts': 0 if value['status'] == 'aliased' else len(value['calls']),
                     'model_parameters': parameters})
    return rows


def usage_summary(bundle):
    scopes = {}
    for scope, origin in (('original', bundle['parent']), ('first_continuation', bundle['previous']),
                          ('messages_recovery', bundle['previous_messages']),
                          ('responses_recovery', bundle['root']), ('combined', None)):
        rows = [r for r in bundle['receipts'].values() if origin is None or r['origin_root'] == str(origin)]
        usage = {'known_calls': 0, 'known_total_calls': 0, 'complete_usage_calls': 0,
                 'partial_usage_calls': 0, 'unknown_calls': 0, 'total_missing_calls': 0,
                 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                 'reported_subtotals_differ_calls': 0}
        slots = defaultdict(list)
        for row in rows:
            value = (row['transport'] or {}).get('usage')
            fields = {k: value[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                      if isinstance(value, dict) and type(value.get(k)) is int and value[k] >= 0}
            if not fields: usage['unknown_calls'] += 1
            else:
                usage['known_calls'] += 1
                usage['complete_usage_calls' if len(fields) == 3 else 'partial_usage_calls'] += 1
                for key, number in fields.items(): usage[key] += number
                if len(fields) == 3 and fields['prompt_tokens'] + fields['completion_tokens'] != fields['total_tokens']:
                    usage['reported_subtotals_differ_calls'] += 1
            if 'total_tokens' in fields: usage['known_total_calls'] += 1
            else: usage['total_missing_calls'] += 1
            slots[(row['origin_root'], row['reserve']['slot'])].append(row)
        successful = [v for v in slots.values() if any((r['terminal'] or {}).get('status') == 'completed' for r in v)]
        scopes[scope] = {'physical_calls': len(rows),
            'reserved_attempts': len(rows),
            'confirmed_transport_outcomes': sum(r['transport'] is not None for r in rows),
            'provider_outcome_unknown_calls': sum(r['transport'] is None for r in rows),
            'native_audit_gap_calls': sum(not r['terminal'] for r in rows),
            'locally_reconciled_pending_calls': sum(bool(bundle.get('user_cancellation'))
                and r['qualified_id'] == bundle['user_cancellation']['qualified_id'] for r in rows),
            'completed_calls': sum((r['terminal'] or {}).get('status') == 'completed' for r in rows),
            'failed_calls': sum((r['terminal'] or {}).get('status') == 'failed' for r in rows),
            'unsettled_calls': sum(not r['terminal'] for r in rows),
            'real_physical_calls': sum(r['reserve']['real'] is True for r in rows),
            'simulation_physical_calls': sum(r['reserve']['real'] is False for r in rows),
            'first_attempt_successful_positions': sum(len(v) == 1 for v in successful),
            'native_retry_recovered_positions': sum(len(v) > 1 for v in successful),
            'native_retry_attempts': sum(max(0, len(v) - 1) for v in slots.values()),
            'usage': usage, 'money': 'unknown_not_estimated'}
    failures = [{'qualified_id': r['qualified_id'], 'origin_root': r['origin_root'],
                 'slot': r['reserve']['slot'], 'classification_code': r['terminal']['runtime_attempt'].get('error_code'),
                 'actual_http_status': ((r['transport'] or {}).get('diagnostics') or {}).get('http_status'),
                 'provider_error': (r['transport'] or {}).get('provider_error'),
                 'response_metadata': (r['transport'] or {}).get('provider_response_metadata'),
                 'elapsed_seconds': (r['transport'] or {}).get('elapsed_seconds')}
                for r in bundle['receipts'].values() if (r['terminal'] or {}).get('status') == 'failed']
    return {'scopes': scopes, 'failed_physical_calls': failures, 'prior_failures_retained': True,
            'physical_calls_field_semantics': 'Conservative reserved attempts, not a claim that every reservation has a confirmed supplier outcome.',
            'user_cancellation': bundle.get('user_cancellation'),
            'new_window_manually_authorized': True, 'gateway_supplier_limits': 'unknown',
            'client_limits': 'no max_tokens/max_output_tokens fields or local timeout; gateway may still impose limits',
            'bubble_contract_limits_preserved': True,
            'usage_field_rules': 'known_calls means any reported count; known_total_calls specifically requires reported total_tokens. Never synthesize or rebalance totals; cache/reasoning are subdivisions.',
            'routing': 'configured grok-4.7 gateway; not official xAI or Anthropic direct',
            'auxiliary_shared_cost_counted_once': True, 'controlled_raw_is_not_production_latency': True}


def comparisons(bundle, rows, assessments, pair_readings):
    values = reading.comparisons(bundle, rows, assessments, pair_readings)
    indexed = {r['id']: r for r in rows}
    for row in values:
        if not row['complete']: continue
        left, right = (indexed[row['group'] + '-' + c] for c in (row['left'], row['right']))
        same_protocol = left['wire_protocol'] == right['wire_protocol']
        row.update(left_protocol=left['wire_protocol'], right_protocol=right['wire_protocol'],
                   same_wire_protocol=same_protocol, cohort=left['wire_protocol'] if same_protocol else 'mixed_protocol',
                   cross_cohort_transport_benefit_claim=False)
        auxiliary = bundle['auxiliary'][row['group']]
        aux_call = reading.receipt(bundle, 'auxiliary', row['group'], auxiliary['calls'][-1])
        transport = aux_call['transport'] or {}
        metadata = transport.get('provider_response_metadata') or transport.get('response_metadata') or {}
        diagnostics = transport.get('diagnostics') or {}
        row.update(shared_auxiliary_model=metadata.get('model', diagnostics.get('response_model')),
                   shared_auxiliary_backend=metadata.get('system_fingerprint', diagnostics.get('system_fingerprint')),
                   shared_auxiliary_response_id=metadata.get('response_id', diagnostics.get('response_id')),
                   shared_auxiliary_response_status=metadata.get('status', diagnostics.get('response_status')),
                   shared_auxiliary_protocol=aux_call['reserve'].get('wire_protocol', LEGACY))
        if not same_protocol:
            row.update(explained_consumption_only=False, attribution='protocol_confound_no_spec_gain')
        row['eligible_consumption_observation'] = bool(same_protocol and row['explained_consumption_only']
                                                       and not row['identical_request'])
    return values


def append_user_review(root, rows, *, after_mapping_revealed=False, raw_answer=None):
    root = confined(root); bundle = load_bundle(root)
    plan = reading.review_plan(bundle)
    reading.resolve_user_review(rows, plan, bundle['results'])
    folder = root / 'analysis'; folder.mkdir(exist_ok=True)
    existing = sorted(folder.glob('user-review-[0-9][0-9][0-9].json'))
    previous = read(existing[-1]).get('rows', []) if existing else []
    effective = {r['number']: r for r in previous}; effective.update({r['number']: r for r in rows})
    merged = [effective[n] for n in sorted(effective)]
    reading.resolve_user_review(merged, plan, bundle['results'])
    path = folder / f'user-review-{len(existing) + 1:03d}.json'
    require(not path.exists(), 'review_append_collision')
    atomic_json(path, {'rows': merged, 'submitted_rows': rows, 'raw_answer': raw_answer,
        'after_mapping_revealed': after_mapping_revealed, 'independent_new_blind_review': False,
        'supersedes': existing[-1].name if existing else None})
    return path


def import_authored(root):
    """Copy only sealed prior readings once, before a new reader appends."""
    root = confined(root); bundle = load_bundle(root)
    seal = read(PREVIOUS_MESSAGES / 'closure.json')
    folder = root / 'analysis'; folder.mkdir(exist_ok=True)
    provenance = []
    for name, validator in (('authored-main.json', reading.validate_main),
                            ('authored-auxiliary.json', reading.validate_auxiliary)):
        relative = 'real-v1/analysis/' + name
        source = PREVIOUS_MESSAGES / relative
        expected = seal['evidence_hashes'][relative]
        require(digest_file(source) == expected, 'prior_authored_changed')
        validator(read(source), bundle)
        target = folder / name
        if target.exists(): require(digest_file(target) == expected, 'authored_import_would_overwrite')
        else:
            with target.open('xb') as handle: handle.write(source.read_bytes())
        provenance.append({'source': str(source), 'sha256': expected, 'destination': name,
                           'historical_only': True, 'not_a_new_model_call': True})
    path = folder / 'authored-imports.json'
    value = {'source_seal': str(PREVIOUS_MESSAGES / 'closure.json'),
             'source_seal_sha256': digest_file(PREVIOUS_MESSAGES / 'closure.json'), 'files': provenance}
    if path.exists(): require(read(path) == value, 'authored_import_provenance_changed')
    else: atomic_json(path, value)
    return value


def render(root):
    root = confined(root); bundle = load_bundle(root)
    folder = root / 'analysis'; folder.mkdir(exist_ok=True)
    main_document = read(folder / 'authored-main.json') if (folder / 'authored-main.json').exists() else {
        'version': reading.ASSESSMENT_VERSION, 'assessments': [], 'comparisons': []}
    authored = reading.validate_main(main_document, bundle)
    auxiliary = reading.validate_auxiliary(read(folder / 'authored-auxiliary.json'), bundle) if (folder / 'authored-auxiliary.json').exists() else {}
    rows = response_rows(bundle)
    pair_readings = main_document.get('comparisons', [])
    pairs = comparisons(bundle, rows, authored, pair_readings)
    mapping = reading.review_plan(bundle)
    reviews = sorted(folder.glob('user-review-[0-9][0-9][0-9].json'))
    user_document = read(reviews[-1]) if reviews else {'rows': []}
    resolved = reading.resolve_user_review(user_document['rows'], mapping, bundle['results'])
    coverage = reading.review_coverage(mapping, resolved)
    indexed = {r['id']: r for r in rows}
    for item in resolved:
        item['wire_protocol'] = indexed[item['candidate']]['wire_protocol']
    unique = [r for r in rows if r['status'] == 'completed']
    state = read(root / 'state.json') if (root / 'state.json').exists() else {
        'status': 'running_uncheckpointed' if (root / 'run.started.json').exists() else 'not_started'}
    unsettled = sum(not r['terminal'] for r in bundle['receipts'].values())
    engineering = len(rows) == 76 and len(bundle['auxiliary']) == 24 and not unsettled and state['status'] == 'completed'
    full = [r for r in rows if r['condition'] == 'full']
    full_tasks = [reading.task_error(authored.get(r['canonical_id'])) for r in full]
    controls = [r for r in rows if r['group'].split('-')[0] in ('G05', 'G06')]
    control_tasks = [reading.task_error(authored.get(r['canonical_id'])) for r in controls]
    pairs_read = all(p.get('deduplicated') or p.get('pair_read') for p in pairs if p['complete'])
    complete = engineering and len(authored) == len(unique) and len(auxiliary) == 24 and coverage['complete'] and pairs_read
    user_counts = Counter(r['user_candidate_verdict'] for r in resolved)
    positive = [p for p in pairs if p['kind'] == 'primary' and p.get('eligible_consumption_observation') and p['verdict'] == 'win']
    signal = bool(bundle['real'] and complete and len(full_tasks) == 24 and all(v is False for v in full_tasks)
                  and len(control_tasks) == 4 and all(v is False for v in control_tasks)
                  and user_counts['win'] > user_counts['loss'] and positive)
    strata = {}
    for protocol in (LEGACY, ANTHROPIC, RESPONSES):
        rr = [r for r in rows if r['wire_protocol'] == protocol]
        pp = [p for p in pairs if p.get('cohort') == protocol]
        uu = [u for u in resolved if u['wire_protocol'] == protocol]
        unique_ids = {r['canonical_id'] for r in rr}
        assessed = [authored[n] for n in unique_ids if n in authored]
        conditions = {}
        for condition in ('raw', 'essential', 'full'):
            cr = [r for r in rr if r['condition'] == condition]
            actual_ids = {r['canonical_id'] for r in cr}
            tasks = [reading.task_error(authored.get(n)) for n in actual_ids]
            conditions[condition] = {'logical_conditions': len(cr), 'distinct_actual_outputs': len(actual_ids),
                'task_or_boundary_errors': sum(v is True for v in tasks),
                'unread_or_unable': sum(v is None for v in tasks),
                'deduplicated_outputs_are_not_independent': True}
        aa = [a for group, a in auxiliary.items()
              if reading.receipt(bundle, 'auxiliary', group, bundle['auxiliary'][group]['calls'][-1])['reserve'].get('wire_protocol', LEGACY) == protocol]
        strata[protocol] = {'logical_conditions': len(rr), 'unique_outputs': sum(r['status'] == 'completed' for r in rr),
            'aliases': sum(r['status'] == 'aliased' for r in rr),
            'primary_verdicts': dict(Counter(p['verdict'] for p in pp if p['kind'] == 'primary')),
            'user_verdicts': dict(Counter(u['user_candidate_verdict'] for u in uu)),
            'new_direct_controls': sum(r['wire_protocol'] == RESPONSES for r in controls) if protocol == RESPONSES else 0,
            'conditions': conditions, 'auxiliary_outputs_read': len(aa),
            'auxiliary_semantic_items': dict(Counter(i['status'] for a in aa for i in a['critical_items'])),
            'dimension_means_unique_outputs': {d: statistics.mean(a['dimensions'][d]['score'] for a in assessed)
                                              if assessed else None for d in reading.DIMENSIONS}}
    summary = {'version': VERSION, 'real': bundle['real'], 'execution_state': state['status'],
        'stage_closed_by_user': bool(bundle.get('user_cancellation')),
        'local_process_stopped': bool(bundle.get('user_cancellation')),
        'known_native_gap': sum(not r['terminal'] for r in bundle['receipts'].values()),
        'provider_outcome_unknown_calls': sum(r['transport'] is None for r in bundle['receipts'].values()),
        'no_further_generation': bool(bundle.get('user_cancellation')),
        'engineering_complete': engineering, 'quality_analysis_complete': complete, 'research_signal': signal,
        'logical_main_conditions': len(rows), 'planned_main_conditions': 76, 'main_conditions_pending': 76 - len(rows),
        'unique_main_outputs': len(unique), 'aliases': len(rows) - len(unique), 'auxiliary_outputs': len(bundle['auxiliary']),
        'machine_unique_main_read': len(authored), 'machine_main_read_pending': len(unique) - len(authored),
        'machine_auxiliary_read': len(auxiliary), 'whole_context_pair_readings': len(pair_readings),
        'whole_context_pair_read_complete': pairs_read,
        'auxiliary_semantic_items': dict(Counter(i['status'] for a in auxiliary.values() for i in a['critical_items'])),
        'auxiliary_coverage_categories': list(reading.AUXILIARY_COVERAGE),
        'full_task_boundary_observed': len(full_tasks), 'full_task_boundary_planned': 24,
        'full_task_boundary_errors': sum(v is True for v in full_tasks),
        'full_task_boundary_unread_or_unable': 24 - len(full_tasks) + sum(v is None for v in full_tasks),
        'control_task_boundary_errors': sum(v is True for v in control_tasks),
        'controls_are_historical_only': all(r['wire_protocol'] == LEGACY for r in controls),
        'responses_direct_controls_performed': sum(r['wire_protocol'] == RESPONSES for r in controls),
        'protocol_strata': strata, 'cross_protocol_transport_benefit_inferred': False,
        'user_fixed_groups': 12, 'user_groups_ready': sum(p['complete'] for p in mapping),
        'user_vote_required': sum(p['vote_required'] for p in mapping), 'user_review_received': coverage['received'],
        'user_review_identical_input_exempt': coverage['exempt_identical_input'],
        'user_review_plan_coverage': coverage['covered'], 'user_review_complete': coverage['complete'],
        'user_verdicts': dict(user_counts), 'user_review_after_mapping_revealed': user_document.get('after_mapping_revealed', False),
        'comparisons': {k: dict(Counter(p['verdict'] for p in pairs if p['kind'] == k)) for k in ('primary', 'secondary', 'exploratory')},
        'unexplained_complete_pairs': sum(p['complete'] and not p.get('explained_consumption_only') for p in pairs),
        'conclusion': 'quality_analysis_pending' if not complete else 'supports_next_research_only' if signal else 'no_stable_research_signal',
        'real_quality_verified': bool(bundle['real'] and complete), 'release_approved': False,
        'naturalness_method': 'authored whole-context reading with exact quotes; not keyword/length/score differences',
        'supplier_limits': 'unknown; omitted client cap is not a promise of unlimited supplier output',
        'dimension_means_unique_outputs': {d: statistics.mean(a['dimensions'][d]['score'] for a in authored.values()) if authored else None for d in reading.DIMENSIONS}}
    for name, value in (('responses', rows), ('comparisons', pairs), ('usage', usage_summary(bundle)),
                        ('summary', summary), ('user-review-resolved', resolved),
                        ('review-mapping-private', {'fixed_before_run': True, 'disclose_after_user_response': True, 'groups': mapping})):
        atomic_json(folder / (name + '.json'), value)
    anonymous = reading.render_review(bundle, mapping)
    anonymous = anonymous.replace('这是机器开发实验的用户偏好复核',
        '本材料包含原协议历史组与新Responses组，各组内三条件共享协议；不据跨组差异评价协议收益。\n\n这是机器开发实验的用户偏好复核', 1)
    (folder / 'CR3-A.1-12组匿名审核.md').write_text(anonymous, encoding='utf-8')
    lines = ['# 实际唯一主回复阅读索引', '', '真实输出' if bundle['real'] else '模拟记录仅验证工具；真实效果未验证', '']
    for row in unique:
        lines.extend(['## ' + row['id'], '', '协议：' + row['wire_protocol'], ''])
        reading._fenced(lines, row['reply'])
    (folder / 'all-unique-main-reading.md').write_text('\n'.join(lines), encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('report',))
    parser.add_argument('--root', type=Path, default=OUT / 'real-v1')
    args = parser.parse_args()
    print(encoded(render(args.root)))
