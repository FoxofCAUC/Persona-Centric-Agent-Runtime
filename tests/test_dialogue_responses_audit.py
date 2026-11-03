"""Artificial seal/receipt tampering; no runtime clients or external calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import dialogue_responses_audit as audit


class ResponsesAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='responses-seal-', dir='D:/Persona-Centric Agent Runtime/tmp')
        self.root = Path(self.tmp.name)
        self.real = self.root / 'real-v1'
        for name in ('receipts', 'results', 'auxiliary'):
            (self.real / name).mkdir(parents=True, exist_ok=True)
        self.write('state.json', {'status': 'stopped', 'real': True})
        self.messages = [{'role': 'system', 'content': '人工系统材料'}, {'role': 'user', 'content': '人工当前输入'}]
        self.payload = {'model': 'grok-4.7', 'input': deepcopy(self.messages), 'store': False, 'stream': False}
        wire = audit.fingerprint(self.payload)
        self.r = {'id': '0001', 'slot': 'g-raw/main', 'kind': 'main', 'real': True, 'simulation': False,
                  'protocol': audit.PROFILE, 'wire_protocol': audit.PROFILE,
                  'model': {'name': 'grok-4.7', 'timeout': 180, 'max_tokens': 8192},
                  'messages': self.messages, 'wire_request': self.payload,
                  'input_hash': audit.fingerprint(self.messages), 'request_hash': wire,
                  'effective_timeout_seconds': None, 'effective_output_max_tokens': None}
        self.t = {'real': True, 'simulation': False, 'status': 'completed',
                  'reservation_hash': audit.fingerprint(self.r),
                  'runtime_attempt': {'status': 'completed', 'request_hash': wire,
                                      'timeout_seconds': None, 'local_timeout_policy': 'none'}}
        self.transport = {'real': True, 'raw': '人工完整回复', 'status': 'returned'}
        self.value = {'id': 'g-raw', 'group': 'g', 'status': 'completed', 'real': True, 'simulation': False,
                      'protocol': audit.PROFILE, 'messages': self.messages, 'request_hash': wire,
                      'reply': self.transport['raw'], 'calls': ['0001'],
                      'manifest': {'input_snapshot_hash': audit.fingerprint(self.messages),
                                   'provider_request_contract': {'profile': audit.PROFILE, 'wire_hash': wire,
                                        'logical_input_hash': audit.fingerprint(self.messages),
                                        'max_tokens_mode': 'omit', 'max_output_tokens_mode': 'omit',
                                        'timeout_policy': 'none', 'store': False, 'stream': False,
                                        'effective_max_tokens': None, 'effective_timeout_seconds': None}}}
        self.save_receipt()
        self.write('results/g-raw.json', self.value)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        p = self.real / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def save_receipt(self):
        self.write('receipts/0001.reserve.json', self.r)
        self.write('receipts/0001.terminal.json', self.t)
        self.write('receipts/0001.transport.json', self.transport)

    def rebind(self):
        self.r['request_hash'] = audit.fingerprint(self.r['wire_request'])
        self.t['runtime_attempt']['request_hash'] = self.r['request_hash']
        self.t['reservation_hash'] = audit.fingerprint(self.r)
        self.save_receipt()

    def seal(self, final=False):
        with zipfile.ZipFile(self.root / 'source.zip', 'w') as archive:
            archive.writestr('synthetic.py', b'pass\n')
        seal = {'stage': audit.STAGE, 'stage_complete': False, 'user_review': 'pending',
                'source_archive': 'source.zip', 'source_hashes': {'synthetic.py': audit.digest(b'pass\n')},
                'origin_seals': [], 'evidence_hashes': {
                    p.relative_to(self.root).as_posix(): audit.digest(p.read_bytes())
                    for p in self.root.rglob('*') if p.is_file()}}
        path = self.root / ('closure.json' if final else 'checkpoint.json')
        path.write_text(json.dumps(seal), encoding='utf-8')
        return seal, path

    def verified(self, worktree=False):
        with patch.object(audit, 'verify_origins', return_value={'original': {'passed': True}, 'previous': {'passed': True}, 'messages': {'passed': True}}):
            return audit.verify(self.root, worktree)

    def user_stop_gap(self):
        (self.real / 'results/g-raw.json').unlink()
        for number in range(1, 5):
            reserve, terminal = deepcopy(self.r), deepcopy(self.t)
            reserve.update(id=f'{number:04d}', ordinal=number, kind='joint', slot='g/joint')
            self.write(f'receipts/{number:04d}.reserve.json', reserve)
            if number < 4:
                terminal['status'] = terminal['runtime_attempt']['status'] = 'failed'
                terminal['reservation_hash'] = audit.fingerprint(reserve)
                self.write(f'receipts/{number:04d}.terminal.json', terminal)
                self.write(f'receipts/{number:04d}.transport.json', {'real': True, 'status': 'error', 'raw': None})
        self.write('state.json', {'status': 'stopped', 'real': True, 'reason': 'user_cancelled', 'physical_calls': 4,
                   'external_reconciliation': True, 'not_original_runner_finally_state': True,
                   'reconciled_by': 'manual_external_termination', 'reconciliation': 'user-cancellation-reconciliation.json',
                   'native_audit_gap': 1, 'provider_outcome_unknown': ['0004']})
        reservation_hash = audit.fingerprint(reserve)
        document = {'version': 'responses-user-cancel-v1', 'status': 'provider_outcome_unknown',
                    'request': 'user_cancelled', 'user_requested_stop': True, 'receipt_id': '0004',
                    'native_gap_receipt_id': '0004', 'reservation_hash': reservation_hash, 'slot': 'g/joint',
                    'native_audit_gap': 1, 'native_terminal_missing': True, 'transport_receipt_missing': True,
                    'native_receipts_preserved': True, 'no_synthetic_native_terminal': True, 'native_state_not_synthesized': True,
                    'provider_outcome_unknown': True, 'usage_unknown': True, 'last_usage_unknown': True,
                    'further_dispatch_prohibited': True, 'reserved_calls': 4, 'settled_calls': 3,
                    'known_failed_calls': 3, 'successful_calls': 0,
                    'unsettled': [{'id': '0004', 'reservation_hash': reservation_hash, 'terminal_present': False,
                                   'transport_present': False, 'supplier_outcome': 'unknown', 'usage': 'unknown'}],
                    'local_process_not_running': True, 'local_executor_process_absent': True,
                    'unresolved_local_processes': 0, 'process_check': 'synthetic exact owned-process check found zero',
                    'local_process_proof': {'session_id': '25417', 'owned_python_process_matches': []}}
        self.write('user-cancellation-reconciliation.json', document)
        authorization = {'request': 'user_cancelled', 'user_message': '停止此人工测试，不再生成。',
                         'recorded_at': '2026-11-03T12:00:00+00:00',
                         'local_process_proof': {'status': 'not_running', 'batch_root': str(self.real),
                                                'matched_processes': 0, 'session_id': 25417,
                                                'checked_at': '2026-11-03T12:00:00+00:00',
                                                'method': 'Synthetic unit fixture; no process or provider contacted.'}}
        (self.root / 'user-stop-authorization.json').write_text(json.dumps(authorization), encoding='utf-8')
        return document

    def test_stopped_partial_batch_is_read_only_not_stage_completion(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = audit.batch(self.real)
        self.assertTrue(result['audit_passed'])
        self.assertFalse(result['batch_complete'])
        self.assertEqual(result['unique_main'], 1)
        self.assertEqual(result['generation_calls'], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_no_saved_result_required_after_first_helper_failure(self):
        self.r.update(kind='joint', slot='g/joint')
        self.t['status'] = self.t['runtime_attempt']['status'] = 'failed'
        self.rebind()
        (self.real / 'results/g-raw.json').unlink()
        result = audit.batch(self.real)
        self.assertEqual(result['logical_conditions'], 0)
        self.assertEqual(result['failed_calls'], 1)

    def test_unsettled_receipt_rejected(self):
        (self.real / 'receipts/0001.terminal.json').unlink()
        with self.assertRaisesRegex(ValueError, 'unsettled_receipt'):
            audit.batch(self.real)

    def test_reservation_binding_rejected_after_change(self):
        self.r['model']['timeout'] = 181
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, 'reservation_binding'):
            audit.batch(self.real)

    def test_output_limit_returned_rejected_even_when_hashes_rebound(self):
        for field in ('max_tokens', 'max_output_tokens'):
            with self.subTest(field=field):
                self.payload.pop('max_tokens', None)
                self.payload.pop('max_output_tokens', None)
                self.payload[field] = 8192
                self.rebind()
                with self.assertRaisesRegex(ValueError, 'output_limit_returned'):
                    audit.batch(self.real)

    def test_missing_no_limit_declaration_is_not_inferred(self):
        del self.r['effective_output_max_tokens']
        self.rebind()
        with self.assertRaisesRegex(ValueError, 'output_limit_returned'):
            audit.batch(self.real)

    def test_old_outer_timer_rejected(self):
        self.t['runtime_attempt']['timeout_seconds'] = 240
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, 'local_timeout_returned'):
            audit.batch(self.real)

    def test_injected_deadline_rejected(self):
        self.r['model']['_request_deadline'] = 100000
        self.rebind()
        with self.assertRaisesRegex(ValueError, 'deadline_returned'):
            audit.batch(self.real)

    def test_mock_receipt_cannot_become_real(self):
        self.transport['real'] = False
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, 'mock_mixed_with_real'):
            audit.batch(self.real)

    def test_actual_wire_mutation_not_hidden_by_reservation_rebinding(self):
        original = deepcopy(self.payload)
        for case, code in (('content', 'wire_input_changed'), ('system_order', 'wire_input_changed'),
                           ('store_true', 'wire_store_changed'), ('store_missing', 'wire_store_changed'),
                           ('extra_instructions', 'hidden_instructions_added')):
            with self.subTest(case=case):
                self.payload.clear()
                self.payload.update(deepcopy(original))
                if case == 'content':
                    self.payload['input'][1]['content'] = 'changed synthetic input'
                elif case == 'system_order':
                    self.payload['input'].reverse()
                elif case == 'store_true':
                    self.payload['store'] = True
                elif case == 'store_missing':
                    self.payload.pop('store')
                else:
                    self.payload['instructions'] = 'unregistered synthetic instruction'
                self.rebind()
                with self.assertRaisesRegex(ValueError, code):
                    audit.batch(self.real)

    def test_manifest_actual_wire_hash_must_match(self):
        self.value['manifest']['provider_request_contract']['wire_hash'] = 'wrong'
        self.write('results/g-raw.json', self.value)
        with self.assertRaisesRegex(ValueError, 'manifest_wire_changed'):
            audit.batch(self.real)

    def test_valid_alias_is_not_another_actual_call_or_manifest(self):
        alias = deepcopy(self.value)
        alias.update(id='g-full', status='aliased', canonical_id='g-raw', actual_manifest_from='g-raw')
        alias.pop('manifest')
        alias.pop('calls')
        self.write('results/g-full.json', alias)
        result = audit.batch(self.real)
        self.assertEqual(result['physical_calls'], 1)
        self.assertEqual(result['aliases'], 1)
        self.assertEqual(result['unique_main'], 1)

    def test_alias_fake_manifest_and_cross_group_rejected(self):
        alias = deepcopy(self.value)
        alias.update(id='g-full', status='aliased', canonical_id='g-raw', actual_manifest_from='g-raw')
        self.write('results/g-full.json', alias)
        with self.assertRaisesRegex(ValueError, 'alias_fake_evidence'):
            audit.batch(self.real)
        alias.pop('manifest')
        alias.pop('calls')
        alias['group'] = 'other'
        self.write('results/g-full.json', alias)
        with self.assertRaisesRegex(ValueError, 'invalid_alias'):
            audit.batch(self.real)

    def test_successful_position_not_replayed(self):
        r, t = deepcopy(self.r), deepcopy(self.t)
        r['id'] = '0002'
        t['reservation_hash'] = audit.fingerprint(r)
        self.write('receipts/0002.reserve.json', r)
        self.write('receipts/0002.terminal.json', t)
        self.write('receipts/0002.transport.json', self.transport)
        with self.assertRaisesRegex(ValueError, 'success_replayed'):
            audit.batch(self.real)

    def test_imported_success_is_not_dispatched_in_new_batch(self):
        previous = self.root / 'old/real-v1/results'
        previous.mkdir(parents=True)
        source = previous / 'g-raw.json'
        source.write_text('{}', encoding='utf-8')
        self.write('imports.json', {'parent_root': str(previous.parent),
                   'results': {'g-raw': {'path': str(source), 'sha256': audit.digest(source.read_bytes())}},
                   'auxiliary': {}})
        with patch.object(audit, 'ORIGINAL', previous.parent.parent):
            with self.assertRaisesRegex(ValueError, 'imported_success_replayed'):
                audit.batch(self.real)

    def test_fifth_physical_attempt_rejected(self):
        for n in range(1, 6):
            r, t = deepcopy(self.r), deepcopy(self.t)
            r['id'] = f'{n:04d}'
            t['status'] = t['runtime_attempt']['status'] = 'failed'
            t['reservation_hash'] = audit.fingerprint(r)
            self.write(f'receipts/{n:04d}.reserve.json', r)
            self.write(f'receipts/{n:04d}.terminal.json', t)
            self.write(f'receipts/{n:04d}.transport.json', self.transport)
        with self.assertRaisesRegex(ValueError, 'retry_changed_or_exceeded'):
            audit.batch(self.real)

    def test_checkpoint_verified_without_claiming_stage_complete(self):
        self.seal()
        result = self.verified()
        self.assertTrue(result['passed'])
        self.assertFalse(result['stage_complete'])
        self.assertEqual(result['new_physical_calls'], 1)

    def test_final_seal_rejects_added_evidence(self):
        self.seal(final=True)
        (self.root / 'added.txt').write_text('extra', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence_inventory_changed'):
            self.verified()

    def test_completion_requires_user_review_and_completed_execution(self):
        self.write('state.json', {'status': 'completed', 'real': True})
        seal, path = self.seal()
        seal['stage_complete'] = True
        path.write_text(json.dumps(seal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'premature_completion'):
            self.verified()

    def test_stage_complete_cannot_be_claimed_for_fewer_than_frozen_conditions(self):
        self.write('state.json', {'status': 'completed', 'real': True})
        seal, path = self.seal()
        seal.update(stage_complete=True, user_review='completed')
        path.write_text(json.dumps(seal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'completed_batch_missing_conditions'):
            self.verified()

    def test_source_archive_or_worktree_change_rejected(self):
        self.seal()
        workspace = self.root / 'synthetic-workspace'
        workspace.mkdir()
        source = workspace / 'synthetic.py'
        source.write_bytes(b'pass\n')
        with patch.object(audit, 'ROOT', workspace):
            self.assertTrue(self.verified(worktree=True)['passed'])
            source.write_bytes(b'changed\n')
            with self.assertRaisesRegex(ValueError, 'worktree_changed'):
                self.verified(worktree=True)

    def test_evidence_path_cannot_escape_package(self):
        seal, path = self.seal()
        seal['evidence_hashes']['../outside'] = 'bad'
        path.write_text(json.dumps(seal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside_path'):
            self.verified()

    def test_origin_hash_and_origin_scope_are_bound(self):
        original, previous, messages = self.root / 'origin', self.root / 'previous', self.root / 'messages'
        for p in (original, previous, messages):
            p.mkdir()
            (p / 'closure.json').write_bytes(b'{}')
        seals = [{'path': str(p / 'closure.json'), 'sha256': audit.digest(b'{}')} for p in (original, previous, messages)]
        with patch.object(audit, 'ORIGINAL', original), patch.object(audit, 'PREVIOUS', previous), patch.object(audit, 'MESSAGES', messages), patch('scripts.dialogue_communication_real_audit.verify', return_value={'passed': True}), patch('scripts.dialogue_communication_continuation_audit.verify', return_value={'passed': True}), patch('scripts.dialogue_messages_audit.verify', return_value={'passed': True}):
            self.assertEqual(len(audit.verify_origins(seals)), 3)
            with patch('scripts.dialogue_messages_audit.verify', return_value={'passed': False}):
                with self.assertRaisesRegex(ValueError, 'origin_not_verified'):
                    audit.verify_origins(seals)
            (previous / 'closure.json').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'origin_seal_changed'):
                audit.verify_origins(seals)

    def test_documented_user_interruption_preserves_unknown_without_synthetic_receipts(self):
        self.user_stop_gap()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = audit.batch(self.real)
        self.assertFalse(result['audit_passed'])
        self.assertFalse(result['native_audit_complete'])
        self.assertEqual(result['verification_status'], 'audited_with_documented_interruption')
        self.assertEqual((result['physical_calls'], result['completed_calls'], result['failed_calls'], result['unknown_calls']), (4, 0, 3, 1))
        self.assertEqual(result['native_audit_gaps'], 1)
        self.assertEqual(result['unresolved_local_processes'], 0)
        self.assertFalse((self.real / 'receipts/0004.terminal.json').exists())
        self.assertFalse((self.real / 'receipts/0004.transport.json').exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_user_interruption_hash_cannot_attach_to_another_reservation(self):
        document = self.user_stop_gap()
        document['reservation_hash'] = 'wrong'
        self.write('user-cancellation-reconciliation.json', document)
        with self.assertRaisesRegex(ValueError, 'interruption_reservation_changed'):
            audit.batch(self.real)

    def test_only_one_final_missing_pair_can_be_reconciled(self):
        self.user_stop_gap()
        (self.real / 'receipts/0002.terminal.json').unlink()
        with self.assertRaisesRegex(ValueError, 'unsettled_receipt'):
            audit.batch(self.real)

    def test_one_sided_missing_receipt_is_not_this_user_stop_gap(self):
        self.user_stop_gap()
        self.write('receipts/0004.transport.json', {'real': True, 'status': 'error', 'raw': None})
        with self.assertRaisesRegex(ValueError, 'unsettled_receipt'):
            audit.batch(self.real)

    def test_missing_user_authorization_and_live_process_are_rejected(self):
        self.user_stop_gap()
        path = self.root / 'user-stop-authorization.json'
        authorization = audit.read(path)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'interruption_authorization_missing'):
            audit.batch(self.real)
        authorization['local_process_proof']['matched_processes'] = 1
        path.write_text(json.dumps(authorization), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'interruption_process_not_proven'):
            audit.batch(self.real)

    def test_interruption_requires_explicit_external_user_stop_state(self):
        self.user_stop_gap()
        state = audit.read(self.real / 'state.json')
        state['external_reconciliation'] = False
        self.write('state.json', state)
        with self.assertRaisesRegex(ValueError, 'interruption_state_not_user_stop'):
            audit.batch(self.real)

    def test_reconciliation_cannot_call_unknown_failed_or_successful(self):
        document = self.user_stop_gap()
        document['known_failed_calls'] = 4
        self.write('user-cancellation-reconciliation.json', document)
        with self.assertRaisesRegex(ValueError, 'interruption_count_changed'):
            audit.batch(self.real)

    def test_documented_gap_seal_requires_user_stopped_and_never_stage_complete(self):
        self.user_stop_gap()
        seal, path = self.seal()
        with self.assertRaisesRegex(ValueError, 'interruption_seal_not_user_stopped'):
            self.verified()
        seal['status'] = 'user_stopped'
        path.write_text(json.dumps(seal), encoding='utf-8')
        result = self.verified()
        self.assertTrue(result['passed'])
        self.assertFalse(result['native_audit_complete'])
        self.assertFalse(result['audit_passed'])
        self.assertEqual(result['documented_unknown'], 1)
        seal.update(stage_complete=True, user_review='completed')
        path.write_text(json.dumps(seal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'interruption_seal_not_user_stopped'):
            self.verified()
            seals[1]['path'] = str(self.root / 'outside.json')
            with self.assertRaisesRegex(ValueError, 'origin_outside_path'):
                audit.verify_origins(seals)


if __name__ == '__main__':
    unittest.main()
