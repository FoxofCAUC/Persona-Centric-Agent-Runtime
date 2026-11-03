"""Artificial reports verify Messages provenance without supplier access."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.provider_compat import build_request
from scripts import dialogue_messages_analysis as analysis
from scripts.dialogue_anthropic_transport import build_messages_request, transport_contract
from tests.test_dialogue_communication_continuation_analysis import bundle as old_bundle, result, protocol


MODEL = {'name': 'grok-4.7', 'max_tokens': 8192, 'timeout': 180}


def reserve(ident, slot, messages, *, messages_protocol=False):
    body = (build_messages_request(MODEL, messages)[0] if messages_protocol else build_request(MODEL, messages)[0])
    value = {'id': ident, 'ordinal': int(ident) if ident.isdecimal() else 1, 'slot': slot,
             'real': True, 'simulation': False, 'model': deepcopy(MODEL), 'messages': messages,
             'input_hash': fingerprint(messages), 'request_hash': fingerprint(body)}
    if messages_protocol: value.update(wire_protocol=analysis.MESSAGES, wire_request=body)
    return value


def current_bundle():
    value = old_bundle(); value['previous'] = Path('/first/real-v1')
    value['transport_contract'] = transport_contract()
    value['receipts']['aux']['reserve'] = {'slot': 'G07-process-r1/joint', 'ordinal': 1, 'real': True}
    value['receipts']['aux']['terminal'] = {'status': 'completed'}
    for number, (ident, row) in enumerate(value['results'].items(), 1):
        r = reserve(f'{number:04d}', ident + '/main', row['messages'], messages_protocol=True)
        row['request_hash'] = r['request_hash']; row['calls'] = [r['id']]
        row['manifest']['provider_request_contract'] = {'profile': analysis.MESSAGES, 'wire_hash': r['request_hash']}
        value['receipts'][ident] = {'origin_root': str(value['root']), 'receipt_id': r['id'],
            'qualified_id': 'new::' + r['id'], 'reserve': r, 'terminal': {'status': 'completed'},
            'transport': {'raw': row['reply'], 'usage': {'total_tokens': 2},
                          'diagnostics': {'response_model': 'grok-4.7', 'system_fingerprint': 'fp_same'}}}
    return value


class MessagesWireAnalysisTests(unittest.TestCase):
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
        self.assertEqual(first[0]['wire_protocol'], analysis.MESSAGES)

    def test_true_payload_change_and_false_manifest_profile_rejected(self):
        b = current_bundle(); call = next(v for v in b['receipts'].values() if 'wire_request' in v['reserve'])
        call['reserve']['wire_request']['temperature'] = 0.1
        with self.assertRaisesRegex(ValueError, 'messages_wire_changed'): analysis.response_rows(b)
        b = current_bundle(); next(iter(b['results'].values()))['manifest']['provider_request_contract']['profile'] = analysis.LEGACY
        with self.assertRaisesRegex(ValueError, 'manifest_wire_contract_changed'): analysis.response_rows(b)

    def test_actual_temperature_change_is_unexplained(self):
        b = current_bundle(); rows = analysis.response_rows(b)
        rows[0]['model_parameters']['temperature'] = 0.1
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertFalse(primary['explained_consumption_only'])
        self.assertEqual(primary['attribution'], 'unexplained_difference')

    def test_same_messages_protocol_only_projection_is_explained(self):
        b = current_bundle(); rows = analysis.response_rows(b)
        primary = analysis.comparisons(b, rows, {}, [])[0]
        self.assertTrue(primary['explained_consumption_only'])
        self.assertTrue(primary['same_wire_protocol'])
        self.assertTrue(primary['eligible_consumption_observation'])

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
        self.original, self.previous, self.out = (self.top / n for n in ('original', 'previous', 'new'))
        self.old, self.first, self.new = (p / 'real-v1' for p in (self.original, self.previous, self.out))
        for p in (self.old, self.first, self.new): p.mkdir(parents=True)
        self.write(self.original / 'closure.json', {'sealed': True})
        self.write(self.previous / 'closure.json', {'sealed': True})
        materials = old_bundle()['materials']; proto = protocol()
        for root in (self.old, self.new):
            self.write(root / 'materials.json', materials); self.write(root / 'protocol.json', proto)
        for root in (self.old, self.first, self.new):
            self.write(root / 'prepared.json', {'real': True, 'batch': root.parent.name,
                'positions': [{'id': 'G07-process-r1', 'conditions': ['raw', 'essential', 'full']}]})
        self.write(self.new / 'transport-contract.json', transport_contract())
        self.write(self.new / 'imports.json', {'parent_root': str(self.old), 'history_roots': [str(self.old), str(self.first)],
            'prior_packages': [{'root': str(p), 'closure_path': str(p / 'closure.json'),
                                'closure_sha256': analysis.digest_file(p / 'closure.json')} for p in (self.original, self.previous)],
            'results': {}, 'auxiliary': {}})
        self.write(self.new / 'state.json', {'status': 'stopped'})
        self.patches = [patch.object(analysis, 'OUT', self.out), patch.object(analysis, 'ORIGINAL', self.original),
                        patch.object(analysis, 'CONTINUATION', self.previous)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def receipt(self, root, number, completed):
        ident = f'{number:04d}'
        r = reserve(ident, 'G11-process-r1/joint', [{'role': 'user', 'content': '人工输入'}], messages_protocol=root == self.new)
        t = {'status': 'completed' if completed else 'failed', 'reservation_hash': fingerprint(r),
             'runtime_attempt': {'request_hash': r['request_hash']}}
        self.write(root / 'receipts' / (ident + '.reserve.json'), r)
        self.write(root / 'receipts' / (ident + '.terminal.json'), t)
        self.write(root / 'receipts' / (ident + '.transport.json'), {'usage': {'total_tokens': 10} if completed else None})

    def test_three_origins_retain_19_old_calls_and_failure_unknown_usage(self):
        for n in range(1, 16): self.receipt(self.old, n, n <= 10)
        for n in range(1, 5): self.receipt(self.first, n, False)
        self.receipt(self.new, 1, True)
        b = analysis.load_bundle(self.new); usage = analysis.usage_summary(b)
        self.assertEqual(len(b['receipts']), 20)
        self.assertEqual(usage['scopes']['combined']['physical_calls'], 20)
        self.assertEqual(usage['scopes']['original']['physical_calls'], 15)
        self.assertEqual(usage['scopes']['first_continuation']['failed_calls'], 4)
        self.assertEqual(usage['scopes']['combined']['usage']['unknown_calls'], 9)
        self.assertEqual(usage['scopes']['combined']['usage']['total_tokens'], 110)
        self.assertEqual(sum(r['receipt_id'] == '0001' for r in b['receipts'].values()), 3)

    def test_incomplete_batch_cannot_be_quality_pass_or_generate_user_answers(self):
        before = {p: p.read_bytes() for p in self.original.rglob('*') if p.is_file()}
        v = analysis.render(self.new)
        self.assertFalse(v['engineering_complete'])
        self.assertFalse(v['quality_analysis_complete'])
        self.assertFalse(v['research_signal'])
        self.assertEqual(v['user_review_received'], 0)
        self.assertEqual(v['main_conditions_pending'], 76)
        self.assertEqual(v['messages_direct_controls_performed'], 0)
        self.assertFalse(v['cross_protocol_transport_benefit_inferred'])
        self.assertEqual(v['protocol_strata'][analysis.MESSAGES]['conditions']['full']['logical_conditions'], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.original.rglob('*') if p.is_file()})

    def test_sealed_authored_import_verifies_hash_and_does_not_overwrite_new_reading(self):
        relative = ('real-v1/analysis/authored-main.json', 'real-v1/analysis/authored-auxiliary.json')
        for name in relative:
            self.write(self.previous / name, {'version': analysis.reading.ASSESSMENT_VERSION, 'assessments': []})
        self.write(self.previous / 'closure.json', {'evidence_hashes': {n: analysis.digest_file(self.previous / n) for n in relative}})
        imports = analysis.read(self.new / 'imports.json')
        imports['prior_packages'][1]['closure_sha256'] = analysis.digest_file(self.previous / 'closure.json')
        self.write(self.new / 'imports.json', imports)
        result = analysis.import_authored(self.new)
        self.assertEqual(len(result['files']), 2)
        source = self.previous / relative[0]; target = self.new / 'analysis/authored-main.json'
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.write(target, {'new_reader_wrote': True})
        with self.assertRaisesRegex(ValueError, 'would_overwrite'): analysis.import_authored(self.new)

    def test_authored_import_rejects_changed_old_content(self):
        for name in ('authored-main.json', 'authored-auxiliary.json'):
            self.write(self.first / 'analysis' / name, {'version': analysis.reading.ASSESSMENT_VERSION, 'assessments': []})
        paths = ['real-v1/analysis/' + name for name in ('authored-main.json', 'authored-auxiliary.json')]
        self.write(self.previous / 'closure.json', {'evidence_hashes': {n: analysis.digest_file(self.previous / n) for n in paths}})
        imports = analysis.read(self.new / 'imports.json')
        imports['prior_packages'][1]['closure_sha256'] = analysis.digest_file(self.previous / 'closure.json')
        self.write(self.new / 'imports.json', imports)
        self.write(self.first / 'analysis/authored-main.json', {'assessments': []})
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
