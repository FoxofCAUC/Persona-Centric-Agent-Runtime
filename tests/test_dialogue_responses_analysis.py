"""Artificial reports verify Messages provenance without supplier access."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.provider_compat import build_request
from scripts import dialogue_responses_analysis as analysis
from scripts.dialogue_responses_transport import build_responses_request, transport_contract
from scripts.dialogue_anthropic_transport import build_messages_request as build_prior_messages
from tests.test_dialogue_communication_continuation_analysis import bundle as old_bundle, result, protocol


MODEL = {'name': 'grok-4.7', 'max_tokens': 8192, 'timeout': 180}


def reserve(ident, slot, messages, *, responses_protocol=False):
    body = (build_responses_request(MODEL, messages)[0] if responses_protocol else build_request(MODEL, messages)[0])
    value = {'id': ident, 'ordinal': int(ident) if ident.isdecimal() else 1, 'slot': slot,
             'real': True, 'simulation': False, 'model': deepcopy(MODEL), 'messages': messages,
             'input_hash': fingerprint(messages), 'request_hash': fingerprint(body)}
    if responses_protocol: value.update(wire_protocol=analysis.RESPONSES, wire_request=body)
    return value


def current_bundle():
    value = old_bundle(); value['previous'] = Path('/first/real-v1'); value['previous_messages'] = Path('/messages/real-v1')
    value['transport_contract'] = transport_contract()
    value['receipts']['aux']['reserve'] = {'slot': 'G07-process-r1/joint', 'ordinal': 1, 'real': True,
                                         'wire_protocol': analysis.RESPONSES}
    value['receipts']['aux']['terminal'] = {'status': 'completed'}
    for number, (ident, row) in enumerate(value['results'].items(), 1):
        r = reserve(f'{number:04d}', ident + '/main', row['messages'], responses_protocol=True)
        row['request_hash'] = r['request_hash']; row['calls'] = [r['id']]
        row['manifest']['provider_request_contract'] = {'profile': analysis.RESPONSES,
            'wire_hash': r['request_hash'], 'logical_input_hash': fingerprint(row['messages'])}
        value['receipts'][ident] = {'origin_root': str(value['root']), 'receipt_id': r['id'],
            'qualified_id': 'new::' + r['id'], 'reserve': r, 'terminal': {'status': 'completed'},
            'transport': {'raw': row['reply'], 'usage': {'total_tokens': 2},
                          'diagnostics': {'response_model': 'grok-4.7', 'system_fingerprint': 'fp_same'}}}
    return value


class ResponsesWireAnalysisTests(unittest.TestCase):
    def test_deadline_and_legacy_cap_do_not_change_actual_wire_parameters(self):
        b = current_bundle()
        first = analysis.response_rows(b)
        for row in b['receipts'].values():
            if 'model' in row['reserve']:
                row['reserve']['model'].update(timeout=999, max_tokens=999999, _request_deadline=123456)
        second = analysis.response_rows(b)
        self.assertEqual([r['model_parameters'] for r in first], [r['model_parameters'] for r in second])
        self.assertNotIn('system', first[0]['model_parameters'])
        self.assertNotIn('messages', first[0]['model_parameters'])
        self.assertNotIn('input', first[0]['model_parameters'])
        self.assertFalse(first[0]['model_parameters']['store'])
        self.assertEqual(first[0]['wire_protocol'], analysis.RESPONSES)

    def test_true_payload_change_and_false_manifest_profile_rejected(self):
        b = current_bundle(); call = next(v for v in b['receipts'].values() if 'wire_request' in v['reserve'])
        call['reserve']['wire_request']['temperature'] = 0.1
        with self.assertRaisesRegex(ValueError, 'responses_wire_changed'): analysis.response_rows(b)
        b = current_bundle(); next(iter(b['results'].values()))['manifest']['provider_request_contract']['profile'] = analysis.LEGACY
        with self.assertRaisesRegex(ValueError, 'manifest_wire_contract_changed'): analysis.response_rows(b)

    def test_actual_temperature_change_is_unexplained(self):
        b = current_bundle(); rows = analysis.response_rows(b)
        rows[0]['model_parameters']['temperature'] = 0.1
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertFalse(primary['explained_consumption_only'])
        self.assertEqual(primary['attribution'], 'unexplained_difference')

    def test_same_responses_protocol_only_projection_is_explained(self):
        b = current_bundle(); rows = analysis.response_rows(b)
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertTrue(primary['explained_consumption_only'])
        self.assertTrue(primary['same_wire_protocol'])
        self.assertTrue(primary['eligible_consumption_observation'])

    def test_independent_public_metadata_preserves_unknown_backend_identifiers(self):
        b = current_bundle()
        for row in b['receipts'].values():
            if 'wire_request' in row['reserve']:
                row['transport']['provider_response_metadata'] = {'model': 'unexpected-route-backend',
                    'system_fingerprint': 'opaque-backend-17', 'status': 'completed', 'response_id': 'resp_public'}
        rows = analysis.response_rows(b)
        self.assertEqual(rows[0]['model'], 'unexpected-route-backend')
        self.assertEqual(rows[0]['backend'], 'opaque-backend-17')
        self.assertEqual(rows[0]['response_id'], 'resp_public')

    def test_auxiliary_metadata_is_not_lost_to_legacy_grok_filter(self):
        b = current_bundle()
        b['receipts']['aux']['transport']['provider_response_metadata'] = {
            'model': 'different-aux-route', 'system_fingerprint': 'opaque-aux-17', 'status': 'completed'}
        primary = analysis.comparisons(b, analysis.response_rows(b), {}, [])[0]
        self.assertEqual(primary['shared_auxiliary_model'], 'different-aux-route')
        self.assertEqual(primary['shared_auxiliary_backend'], 'opaque-aux-17')

    def test_partial_usage_totals_not_invented_and_reported_subtotals_not_rebalanced(self):
        b = current_bundle(); mains = [r for r in b['receipts'].values() if 'wire_request' in r['reserve']]
        mains[0]['transport']['usage'] = {'prompt_tokens': 8, 'completion_tokens': 2}
        mains[1]['transport']['usage'] = {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 10,
                                        'completion_tokens_details': {'reasoning_tokens': 2}}
        usage = analysis.usage_summary(b)['scopes']['combined']['usage']
        self.assertEqual(usage['known_total_calls'], 1)
        self.assertEqual(usage['partial_usage_calls'], 1)
        self.assertEqual(usage['unknown_calls'], 1)
        self.assertEqual(usage['total_missing_calls'], 2)
        self.assertEqual(usage['total_tokens'], 10)
        self.assertEqual(usage['prompt_tokens'], 13)
        self.assertEqual(usage['reported_subtotals_differ_calls'], 1)

    def test_semantic_failure_class_does_not_rewrite_actual_http200(self):
        b = current_bundle()
        b['receipts']['rejected'] = {'origin_root': str(b['root']), 'receipt_id': '9999', 'qualified_id': 'new::9999',
            'reserve': {'slot': 'failed/joint', 'ordinal': 99, 'real': True},
            'terminal': {'status': 'failed', 'runtime_attempt': {'error_code': 'provider_http_401'}},
            'transport': {'usage': None, 'diagnostics': {'http_status': 200},
                          'provider_error': {'code': 'invalid_api_key'}, 'elapsed_seconds': 1}}
        failure = analysis.usage_summary(b)['failed_physical_calls'][0]
        self.assertEqual(failure['classification_code'], 'provider_http_401')
        self.assertEqual(failure['actual_http_status'], 200)

    def test_cross_protocol_never_counts_as_spec_influence(self):
        b = current_bundle(); rows = analysis.response_rows(b)
        rows[0]['wire_protocol'] = analysis.LEGACY
        rows[0]['model_parameters']['wire_protocol'] = analysis.LEGACY
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertFalse(primary['same_wire_protocol'])
        self.assertFalse(primary['eligible_consumption_observation'])
        self.assertEqual(primary['attribution'], 'protocol_confound_no_spec_gain')

    def test_alias_not_independent_and_same_input_requires_no_user_opinion(self):
        b = current_bundle(); raw = b['results']['G07-process-r1-raw']
        alias = deepcopy(raw) | {'id': 'G07-process-r1-full', 'condition': 'full', 'status': 'aliased',
            'canonical_id': raw['id'], 'actual_manifest_from': raw['id']}
        b['results'][alias['id']] = alias
        rows = analysis.response_rows(b)
        self.assertEqual(sum(r['physical_attempts'] for r in rows), 1)
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertTrue(primary['deduplicated'])
        plan = analysis.reading.review_plan(b)
        resolved = analysis.reading.resolve_user_review([], plan, b['results'])
        self.assertEqual(resolved[0]['user_candidate_verdict'], 'no_vote_identical_input')
        self.assertIsNone(resolved[0]['user_choice'])
        self.assertFalse(resolved[0]['is_user_opinion'])


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='D:/Persona-Centric Agent Runtime/tmp', prefix='cr3-messages-report-')
        self.top = Path(self.tmp.name)
        self.original, self.previous, self.messages, self.out = (self.top / n for n in ('original', 'previous', 'messages', 'new'))
        self.old, self.first, self.message_real, self.new = (p / 'real-v1' for p in (self.original, self.previous, self.messages, self.out))
        for p in (self.old, self.first, self.message_real, self.new): p.mkdir(parents=True)
        self.write(self.original / 'closure.json', {'sealed': True})
        self.write(self.previous / 'closure.json', {'sealed': True})
        self.write(self.messages / 'closure.json', {'sealed': True})
        materials = old_bundle()['materials']; proto = protocol()
        for root in (self.old, self.new):
            self.write(root / 'materials.json', materials); self.write(root / 'protocol.json', proto)
        for root in (self.old, self.first, self.message_real, self.new):
            self.write(root / 'prepared.json', {'real': True, 'batch': root.parent.name,
                'positions': [{'id': 'G07-process-r1', 'conditions': ['raw', 'essential', 'full']}]})
        self.write(self.new / 'transport-contract.json', transport_contract())
        self.write(self.new / 'imports.json', {'parent_root': str(self.old), 'history_roots': [str(self.old), str(self.first), str(self.message_real)],
            'prior_packages': [{'root': str(p), 'closure_path': str(p / 'closure.json'),
                                'closure_sha256': analysis.digest_file(p / 'closure.json')} for p in (self.original, self.previous, self.messages)],
            'results': {}, 'auxiliary': {}})
        self.write(self.new / 'state.json', {'status': 'stopped'})
        self.patches = [patch.object(analysis, 'OUT', self.out), patch.object(analysis, 'ORIGINAL', self.original),
                        patch.object(analysis, 'CONTINUATION', self.previous), patch.object(analysis, 'PREVIOUS_MESSAGES', self.messages)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def receipt(self, root, number, completed):
        ident = f'{number:04d}'
        r = reserve(ident, 'G11-process-r1/joint', [{'role': 'user', 'content': '人工输入'}], responses_protocol=root == self.new)
        if root == self.message_real:
            body = build_prior_messages(MODEL, r['messages'])[0]
            r.update(wire_protocol=analysis.ANTHROPIC, wire_request=body, request_hash=fingerprint(body))
        t = {'status': 'completed' if completed else 'failed', 'reservation_hash': fingerprint(r),
             'runtime_attempt': {'request_hash': r['request_hash']}}
        self.write(root / 'receipts' / (ident + '.reserve.json'), r)
        self.write(root / 'receipts' / (ident + '.terminal.json'), t)
        self.write(root / 'receipts' / (ident + '.transport.json'), {'usage': {'total_tokens': 10} if completed else None})

    def cancellation_fixture(self):
        for n in range(1, 4): self.receipt(self.new, n, False)
        missing = reserve('0004', 'G11-process-r1/joint', [{'role': 'user', 'content': '人工输入'}], responses_protocol=True)
        self.write(self.new / 'receipts/0004.reserve.json', missing)
        settled = sum(len(list((root / 'receipts').glob('*.terminal.json'))) for root in (self.old, self.first, self.message_real, self.new))
        note = {'version': 'responses-user-cancel-v1', 'status': 'provider_outcome_unknown',
            'user_requested_stop': True, 'local_process_not_running': True, 'local_executor_process_absent': True,
            'native_receipts_preserved': True, 'no_synthetic_native_terminal': True, 'further_dispatch_prohibited': True,
            'process_check': 'exact artificial batch process query returned zero matches',
            'unsettled': [{'id': '0004', 'reservation_hash': fingerprint(missing), 'transport_present': False,
                'terminal_present': False, 'supplier_outcome': 'unknown', 'usage': 'unknown',
                'physical_dispatch_confirmation': 'unknown; reservation conservatively counted'}],
            'native_gap_receipt_id': '0004', 'reserved_calls': 4, 'settled_calls': 3,
            'successful_calls': 0, 'known_failed_calls': 3, 'native_audit_gap': 1,
            'conservative_combined_reservations': settled + 1, 'combined_settled_successes': 0,
            'combined_settled_failures': settled, 'combined_unsettled': 1}
        self.write(self.new / 'user-cancellation-reconciliation.json', note)
        self.write(self.new / 'state.json', {'status': 'stopped', 'reason': 'user_cancelled',
            'not_original_runner_finally_state': True, 'reconciled_by': 'manual_external_termination',
            'reconciliation': 'user-cancellation-reconciliation.json', 'native_audit_gap': 1,
            'provider_outcome_unknown': ['0004']})
        self.write(self.out / 'user-stop-authorization.json', {'request': 'user_cancelled', 'user_message': '停止测试',
            'local_process_proof': {'status': 'not_running', 'matched_processes': 0,
                                    'batch_root': str(self.new), 'method': 'exact artificial process query'}})
        return note

    def test_four_origins_retain_23_old_calls_and_failure_unknown_usage(self):
        for n in range(1, 16): self.receipt(self.old, n, n <= 10)
        for n in range(1, 5): self.receipt(self.first, n, False)
        for n in range(1, 5): self.receipt(self.message_real, n, False)
        self.receipt(self.new, 1, True)
        b = analysis.load_bundle(self.new); usage = analysis.usage_summary(b)
        self.assertEqual(len(b['receipts']), 24)
        self.assertEqual(usage['scopes']['combined']['physical_calls'], 24)
        self.assertEqual(usage['scopes']['original']['physical_calls'], 15)
        self.assertEqual(usage['scopes']['first_continuation']['failed_calls'], 4)
        self.assertEqual(usage['scopes']['messages_recovery']['failed_calls'], 4)
        self.assertEqual(usage['scopes']['combined']['usage']['unknown_calls'], 13)
        self.assertEqual(usage['scopes']['combined']['usage']['total_tokens'], 110)
        self.assertEqual(sum(r['receipt_id'] == '0001' for r in b['receipts'].values()), 4)

    def test_user_stop_unknown_is_not_failed_complete_or_wrong_origin(self):
        self.receipt(self.old, 4, False)
        self.cancellation_fixture()
        b = analysis.load_bundle(self.new); usage = analysis.usage_summary(b)
        combined = usage['scopes']['combined']
        self.assertEqual(combined['reserved_attempts'], 5)
        self.assertEqual(combined['confirmed_transport_outcomes'], 4)
        self.assertEqual(combined['failed_calls'], 4)
        self.assertEqual(combined['provider_outcome_unknown_calls'], 1)
        self.assertEqual(combined['native_audit_gap_calls'], 1)
        self.assertEqual(combined['locally_reconciled_pending_calls'], 1)
        self.assertTrue(b['user_cancellation']['qualified_id'].startswith(str(self.new)))
        value = analysis.render(self.new)
        self.assertTrue(value['stage_closed_by_user'])
        self.assertTrue(value['local_process_stopped'])
        self.assertFalse(value['quality_analysis_complete'])
        self.assertFalse((self.new / 'receipts/0004.terminal.json').exists())
        self.assertFalse((self.new / 'receipts/0004.transport.json').exists())

    def test_user_stop_reconciliation_hash_tamper_rejected(self):
        note = self.cancellation_fixture(); note['unsettled'][0]['reservation_hash'] = 'tampered'
        self.write(self.new / 'user-cancellation-reconciliation.json', note)
        with self.assertRaisesRegex(ValueError, 'user_stop_reservation_binding'): analysis.load_bundle(self.new)

    def test_user_stop_process_and_scope_proof_not_assumed(self):
        note = self.cancellation_fixture()
        path = self.out / 'user-stop-authorization.json'; proof = analysis.read(path)
        proof['local_process_proof']['batch_root'] = str(self.old)
        self.write(path, proof)
        with self.assertRaisesRegex(ValueError, 'user_stop_process_proof_invalid'): analysis.load_bundle(self.new)
        proof['local_process_proof']['batch_root'] = str(self.new); self.write(path, proof)
        note['local_process_not_running'] = False
        self.write(self.new / 'user-cancellation-reconciliation.json', note)
        with self.assertRaisesRegex(ValueError, 'user_stop_proof_incomplete'): analysis.load_bundle(self.new)

    def test_incomplete_batch_cannot_be_quality_pass_or_generate_user_answers(self):
        before = {p: p.read_bytes() for p in self.original.rglob('*') if p.is_file()}
        v = analysis.render(self.new)
        self.assertFalse(v['engineering_complete'])
        self.assertFalse(v['quality_analysis_complete'])
        self.assertFalse(v['research_signal'])
        self.assertEqual(v['user_review_received'], 0)
        self.assertEqual(v['main_conditions_pending'], 76)
        self.assertEqual(v['responses_direct_controls_performed'], 0)
        self.assertFalse(v['cross_protocol_transport_benefit_inferred'])
        self.assertEqual(v['protocol_strata'][analysis.RESPONSES]['conditions']['full']['logical_conditions'], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.original.rglob('*') if p.is_file()})

    def test_sealed_authored_import_verifies_hash_and_does_not_overwrite_new_reading(self):
        relative = ('real-v1/analysis/authored-main.json', 'real-v1/analysis/authored-auxiliary.json')
        for name in relative:
            self.write(self.messages / name, {'version': analysis.reading.ASSESSMENT_VERSION, 'assessments': []})
        self.write(self.messages / 'closure.json', {'evidence_hashes': {n: analysis.digest_file(self.messages / n) for n in relative}})
        imports = analysis.read(self.new / 'imports.json')
        imports['prior_packages'][2]['closure_sha256'] = analysis.digest_file(self.messages / 'closure.json')
        self.write(self.new / 'imports.json', imports)
        result = analysis.import_authored(self.new)
        self.assertEqual(len(result['files']), 2)
        source = self.messages / relative[0]; target = self.new / 'analysis/authored-main.json'
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.write(target, {'new_reader_wrote': True})
        with self.assertRaisesRegex(ValueError, 'would_overwrite'): analysis.import_authored(self.new)

    def test_authored_import_rejects_changed_old_content(self):
        for name in ('authored-main.json', 'authored-auxiliary.json'):
            self.write(self.message_real / 'analysis' / name, {'version': analysis.reading.ASSESSMENT_VERSION, 'assessments': []})
        paths = ['real-v1/analysis/' + name for name in ('authored-main.json', 'authored-auxiliary.json')]
        self.write(self.messages / 'closure.json', {'evidence_hashes': {n: analysis.digest_file(self.messages / n) for n in paths}})
        imports = analysis.read(self.new / 'imports.json')
        imports['prior_packages'][2]['closure_sha256'] = analysis.digest_file(self.messages / 'closure.json')
        self.write(self.new / 'imports.json', imports)
        self.write(self.message_real / 'analysis/authored-main.json', {'assessments': []})
        with self.assertRaisesRegex(ValueError, 'prior_authored_changed'): analysis.import_authored(self.new)

    def test_import_changed_or_success_replayed_rejected(self):
        value = result('G07-process-r1-raw')
        source = self.old / 'results' / (value['id'] + '.json'); self.write(source, value)
        imports = analysis.read(self.new / 'imports.json')
        imports['results'][value['id']] = {'path': str(source), 'sha256': analysis.digest_file(source)}
        self.write(self.new / 'imports.json', imports)
        analysis.load_bundle(self.new)
        self.write(self.new / 'results' / source.name, value)
        with self.assertRaisesRegex(ValueError, 'successful_position_replayed'): analysis.load_bundle(self.new)
        (self.new / 'results' / source.name).unlink()
        self.write(source, value | {'reply': 'changed'})
        with self.assertRaisesRegex(ValueError, 'import_changed'): analysis.load_bundle(self.new)

    def test_wrong_history_or_sealed_output_rejected(self):
        imports = analysis.read(self.new / 'imports.json'); imports['history_roots'].reverse()
        self.write(self.new / 'imports.json', imports)
        with self.assertRaisesRegex(ValueError, 'history_roots_changed'): analysis.load_bundle(self.new)
        self.write(self.out / 'closure.json', {'sealed': True})
        with self.assertRaisesRegex(ValueError, 'sealed'): analysis.confined(self.new)
        with self.assertRaisesRegex(ValueError, 'outside'): analysis.confined(self.old)


if __name__ == '__main__': unittest.main()
