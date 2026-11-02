import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.chat import ModelError
from persona_runtime.config import atomic_json
from persona_runtime.generation_retry import audited_complete, GenerationRetriesExhausted
from persona_runtime.reply_batch import validate_reply, expression_snapshot
from scripts import dialogue_diagnosis_real as real
from scripts import dialogue_diagnosis_analysis as analysis
from scripts.dialogue_refactor import offline


class PhysicalReceipts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=real.HOME / 'tmp', prefix='cr1-real-unit-')
        self.ledger = real.Receipts(Path(self.temp.name) / 'receipts')
        self.model = {'name': 'grok-4.7', 'base_url': 'https://example.invalid/v1', 'max_tokens': 512, 'timeout': 1, 'encrypted_key': ''}
        self.messages = [{'role': 'user', 'content': '人工测试'}]
        self.state = {}
        self.raw = encoded({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [{'content': '999', 'content_kind': 'chat', 'cancel_if_new_input': False}]})

    def tearDown(self): self.temp.cleanup()

    async def call(self, transport):
        client = real.TrackedClient(transport, self.ledger, 'slot', 'main', False, lambda: self.state,
                                   lambda: None, self.messages)
        def save(value):
            self.state = deepcopy(value)
            self.ledger.sync('slot', self.state)
        with offline():
            return await audited_complete(client, self.model, self.messages,
                validate=lambda raw: validate_reply(raw, 'natural_structured', expression_snapshot('natural_structured')),
                save=save, before_attempt=lambda _: None, sleep=AsyncMock())

    async def test_protocol_failure_then_recovery_is_not_successful_replay(self):
        await self.call(real.SyntheticTransport(self.raw, errors=['not JSON']))
        rows = self.ledger.rows()
        self.assertEqual([r['terminal']['status'] for r in rows], ['failed', 'completed'])
        self.assertEqual([r['transport']['status'] for r in rows], ['returned', 'returned'])
        self.assertEqual(len({r['request_hash'] for r in rows}), 1)
        with self.assertRaisesRegex(ValueError, 'cr1_real_success_never_replay'):
            self.ledger.reserve('slot', 'main', self.model, self.messages, False)

    async def test_four_network_failures_stop_and_unknown_usage_stays_unknown(self):
        with self.assertRaises(GenerationRetriesExhausted):
            await self.call(real.SyntheticTransport(self.raw, errors=[ModelError('provider_timeout')] * 4))
        rows = self.ledger.rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['terminal']['status'] == 'failed' and r['transport']['usage'] is None for r in rows))

    async def test_valid_wrong_content_does_not_retry(self):
        await self.call(real.SyntheticTransport(self.raw))
        self.assertEqual(len(self.ledger.rows()), 1)

    async def test_cancel_records_interrupted_and_no_next_dispatch(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.call(real.SyntheticTransport(self.raw, errors=[asyncio.CancelledError()]))
        self.assertEqual(self.ledger.rows()[0]['terminal']['status'], 'interrupted')
        with self.assertRaisesRegex(ValueError, 'cr1_unsettled_real_receipt'):
            self.ledger.reserve('next', 'main', self.model, self.messages, False)

    async def test_terminal_write_failure_leaves_gap_and_does_not_repeat(self):
        original = real.once
        def broken(path, value):
            if '.terminal.' in str(path): raise OSError('disk failure')
            return original(path, value)
        with patch.object(real, 'once', side_effect=broken):
            with self.assertRaises(OSError): await self.call(real.SyntheticTransport(self.raw))
        self.assertEqual(len(self.ledger.rows()), 1)
        self.assertNotIn('terminal', self.ledger.rows()[0])

    def test_six_hundred_second_physical_failure_stops(self):
        row = self.ledger.reserve('slot', 'main', self.model, self.messages, False)
        self.ledger.transport(row, {'status': 'error', 'elapsed_seconds': 601})
        with self.assertRaisesRegex(ValueError, 'cr1_physical_failure_over_600s'):
            self.ledger.sync('slot', {'generation_retry': {'attempts': [{'status': 'failed', 'request_hash': row['request_hash']}]}})

    def test_public_record_removes_serialized_credential_field(self):
        value = real.public({'model_snapshot': encoded({'name': 'model', 'encrypted_key': 'ENCRYPTED_CANARY'}),
                             'nested': {'encrypted_key': 'ENCRYPTED_CANARY'}})
        self.assertNotIn('ENCRYPTED_CANARY', encoded(value))
        self.assertNotIn('encrypted_key', encoded(value))


class Reports(unittest.TestCase):
    def test_new_reference_member_link_is_metadata_but_text_is_not(self):
        refs = {'selections': [{'input_event_id': 'old', 'target': {'id': 'source', 'kind': 'event'}}],
                'turns': [{'text': '原文'}]}
        payload = [{'role': 'system', 'content': '固定提示'}, {'role': 'user', 'content': encoded({'references': '引用：' + encoded(refs)})}]
        changed = deepcopy(refs); changed['selections'][0]['input_event_id'] = 'new'
        new = deepcopy(payload); new[1]['content'] = encoded({'references': '引用：' + encoded(changed)})
        self.assertEqual(real.selector_material_control(payload), real.selector_material_control(new))
        changed['turns'][0]['text'] = '变化的原文'; new[1]['content'] = encoded({'references': '引用：' + encoded(changed)})
        self.assertNotEqual(real.selector_material_control(payload), real.selector_material_control(new))

    def test_scene_bootstrap_does_not_treat_repeated_rows_as_new_scenes(self):
        values = [{'scene': 'one', 'delta': 1}, {'scene': 'two', 'delta': -1}]
        self.assertEqual(analysis.scene_interval(values), analysis.scene_interval(values * 4))

    def test_review_selection_is_deterministic_and_does_not_fabricate_negative(self):
        pairs = []
        cases = {f'D{i}': {'spec': {'avoid': []}, 'category': 'realization' if i == 4 else 'context'} for i in range(1, 6)}
        for comparison in ('B1-B0', 'B2-B1', 'B2-B0'):
            for i in range(1, 6):
                pairs.append({'id': f'{comparison}/{i}', 'comparison': comparison, 'scene': f'D{i}', 'delta': i,
                              'candidate_task_or_boundary_error': False, 'control_task_or_boundary_error': False})
        selected = analysis.review_subset(pairs, cases)
        self.assertEqual(selected, analysis.review_subset(list(reversed(pairs)), cases))
        self.assertEqual(len(selected), 12)
        self.assertTrue(all(p['delta'] > 0 for p in selected))
        for comp in ('B1-B0', 'B2-B1', 'B2-B0'):
            self.assertEqual(len({p['scene'] for p in selected if p['comparison'] == comp}), 4)

    def test_missing_machine_assessments_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, 'cr1_assessment_coverage'):
            analysis.validate_assessments([], {'position': {}}, {}, {})


if __name__ == '__main__': unittest.main()
