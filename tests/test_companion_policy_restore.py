"""Artificial D-drive policy round trips; no provider or external network."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.character_policy import choose as character_choice
from persona_runtime.companion import RESTORE_KEY, RESTORE_VERSION, choose, read, restore_available
from persona_runtime.ingress import PARAMS, encoded
from persona_runtime.models import identifier, RuntimeErrorBase
from persona_runtime.reply_batch import expression_snapshot
from tests import test_m753 as fixture


class CompanionPolicyRestore(unittest.IsolatedAsyncioTestCase):
    raw = fixture.IntegrationTests.raw
    turn = fixture.IntegrationTests.turn
    save = fixture.IntegrationTests.save

    def setUp(self):
        fixture.IntegrationTests.setUp(self)
        for target in ('socket.socket.connect', 'socket.create_connection',
                       'asyncio.BaseEventLoop.create_connection',
                       'persona_runtime.chat.ChatCompletionsClient.complete'):
            blocker = patch(target, side_effect=AssertionError('offline-only test'))
            blocker.start(); self.addCleanup(blocker.stop)
        self.model.complete = AsyncMock(side_effect=AssertionError('no generation expected'))

    def current(self):
        return self.ingress.current_policy(self.pid, 'desktop')

    def enable(self, **extra):
        policy = self.current()
        args = dict(persona_id=self.pid, channel='desktop', mode='natural_batch',
            expected_version=policy['id'], operation_id=identifier(),
            companion_mode='minimal_structural', conversation_policy_version=2,
            expression_policy_version=3, reply_mode='natural_structured')
        args.update(extra)
        return self.ingress.update_policy(**args)

    def disable_args(self, **extra):
        policy = self.current()
        return dict(persona_id=self.pid, channel='desktop', mode=policy['mode'],
            expected_version=policy['id'], operation_id=identifier(),
            companion_mode='disabled', **extra)

    def install_data(self, data, *, mode=None, reply_mode=None):
        policy = self.current()
        with self.store.connection(write=True) as conn:
            conn.execute('UPDATE interaction_policies SET data_json=?,mode=?,reply_mode=? WHERE id=?',
                (encoded(data), mode or policy['mode'], reply_mode or policy['reply_mode'], policy['id']))
        return self.current()

    def normalized(self, policy):
        data = json.loads(policy['data_json'])
        if isinstance(data.get('expression'), dict):
            data['expression'].pop('policy_id', None)
        return policy['mode'], policy['reply_mode'], data

    async def test_c2_upgrade_preserves_first_restore_and_is_idempotent(self):
        original=self.current()
        old=self.enable()
        point=json.loads(old['data_json'])[RESTORE_KEY]
        upgraded=self.enable(companion_mode='minimal_chat')
        self.assertEqual(upgraded['companion'],choose('minimal_chat'))
        self.assertEqual(json.loads(upgraded['data_json'])[RESTORE_KEY],point)
        repeated=self.enable(companion_mode='minimal_chat')
        self.assertEqual(repeated['id'],upgraded['id'])
        restored=self.ingress.update_policy(**self.disable_args())
        self.assertEqual(self.normalized(restored),self.normalized(original))
        self.model.complete.assert_not_awaited()

    async def test_c2_refuses_upgrade_without_original_restore(self):
        old=self.enable()
        data=json.loads(old['data_json']);data.pop(RESTORE_KEY)
        before=self.install_data(data)
        with self.assertRaisesRegex(RuntimeErrorBase,'companion_restore_missing'):
            self.enable(companion_mode='minimal_chat')
        self.assertEqual(self.current()['id'],before['id'])
        self.model.complete.assert_not_awaited()

    async def test_exact_old_behavior_restored_without_current_selector_or_version_upgrade(self):
        original = self.current()
        selector = character_choice('hybrid_llm')
        selector['selector_contract']['prompt'] = 'character-selector-v1'
        data = {**PARAMS, 'context': {'context_mode': 'legacy_recent', 'cross_session': True,
            'character_retrieval': selector, 'disposition_mode': 'shadow',
            'situation_observation_mode': 'on_demand'},
            'expression': expression_snapshot('natural_structured', original['id'], version=1)}
        original = self.install_data(data, reply_mode='natural_structured')
        active = self.enable()
        point = json.loads(active['data_json'])[RESTORE_KEY]
        self.assertEqual(point, {'version': RESTORE_VERSION, 'mode': original['mode'],
            'reply_mode': original['reply_mode'], 'data': data})
        with patch('persona_runtime.character_policy.choose', side_effect=AssertionError('do not rechoose')):
            restored = self.ingress.update_policy(**self.disable_args())
        self.assertEqual(self.normalized(restored), self.normalized(original))
        self.assertEqual(restored['expression_policy']['version'], 1)
        self.assertEqual(restored['expression_policy']['policy_id'], restored['id'])
        self.assertEqual(restored['buffer_policy']['schema_version'], 1)
        self.assertNotEqual(restored['id'], original['id'])
        self.assertNotIn('companion', restored)
        self.assertNotIn(RESTORE_KEY, json.loads(restored['data_json']))
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT data_json FROM interaction_policies WHERE id=?',
                (original['id'],)).fetchone()[0], original['data_json'])

    async def test_absent_policy_and_absent_expression_restore_as_equivalent_new_entity(self):
        with self.store.connection(write=True) as conn:
            conn.execute("DELETE FROM interaction_heads WHERE persona_id=? AND channel='desktop'", (self.pid,))
            conn.execute("DELETE FROM interaction_policies WHERE persona_id=? AND channel='desktop'", (self.pid,))
        original = self.current()
        self.assertIsNone(original['id'])
        self.assertNotIn('expression', json.loads(original['data_json']))
        self.enable()
        restored = self.ingress.update_policy(**self.disable_args())
        self.assertEqual(self.normalized(restored), self.normalized(original))
        self.assertIsNotNone(restored['id'])
        turn = await self.turn('人工恢复后的输入')
        self.assertIsNone(read(self.raw(turn['id'])))

    async def test_expression_v2_is_not_upgraded_on_restore(self):
        old = self.current(); data = json.loads(old['data_json'])
        data['expression'] = expression_snapshot('natural_structured', old['id'], version=2)
        original = self.install_data(data, reply_mode='natural_structured')
        self.enable()
        restored = self.ingress.update_policy(**self.disable_args())
        self.assertEqual(self.normalized(restored), self.normalized(original))

    async def test_repeat_enable_none_and_current_preserve_first_restore_point(self):
        first = self.enable(); point = json.loads(first['data_json'])[RESTORE_KEY]
        repeated = self.enable()
        self.assertEqual(first['id'], repeated['id'])
        changed = self.ingress.update_policy(self.pid, 'desktop', first['mode'], first['id'],
            identifier(), cross_session=True)
        self.assertEqual(changed['companion'], choose('minimal_structural'))
        current = self.ingress.update_policy(self.pid, 'desktop', changed['mode'], changed['id'],
            identifier(), companion_mode='current')
        self.assertEqual(current['companion'], choose('current'))
        self.assertEqual(json.loads(current['data_json'])[RESTORE_KEY], point)
        self.assertTrue(restore_available(current))

    async def test_disable_replay_returns_saved_result_after_restore_point_removed(self):
        self.enable(); args = self.disable_args()
        restored = self.ingress.update_policy(**args)
        self.assertEqual(self.ingress.update_policy(**args), restored)
        with self.assertRaisesRegex(RuntimeErrorBase, 'submission_conflict'):
            self.ingress.update_policy(**(args | {'pending_action': 'cancel'}))
        with self.assertRaisesRegex(RuntimeErrorBase, 'version_conflict'):
            self.ingress.update_policy(**(args | {'operation_id': identifier()}))

    async def test_each_new_enable_cycle_captures_current_noncompanion_behavior(self):
        self.enable(); restored = self.ingress.update_policy(**self.disable_args())
        changed = self.ingress.update_policy(self.pid, 'desktop', restored['mode'], restored['id'],
            identifier(), cross_session=True)
        active = self.enable()
        self.assertEqual(json.loads(active['data_json'])[RESTORE_KEY]['data'], json.loads(changed['data_json']))

    async def test_old_experimental_policy_without_restore_point_refuses_auto_restore(self):
        self.enable(); data = json.loads(self.current()['data_json']); data.pop(RESTORE_KEY)
        original = self.install_data(data)
        self.assertFalse(restore_available(original))
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_restore_missing'):
            self.ingress.update_policy(**self.disable_args())
        self.assertEqual(self.current()['id'], original['id'])
        self.enable()  # Re-enabling an old experiment must not invent its original settings.
        self.assertNotIn(RESTORE_KEY, json.loads(self.current()['data_json']))

    async def test_missing_invalid_and_nested_restore_points_fail_without_changes(self):
        active = self.enable(); original = json.loads(active['data_json'])
        for mutation in ('version', 'nested', 'character', 'expression'):
            data = deepcopy(original); point = data[RESTORE_KEY]
            if mutation == 'version': point['version'] = 'unknown'
            elif mutation == 'nested': point['data'][RESTORE_KEY] = {}
            elif mutation == 'character': point['data']['context']['character_retrieval'] = {'mode': 'bad'}
            else: point['data']['expression'] = {'version': 99}
            self.install_data(data)
            with self.subTest(mutation=mutation):
                self.assertFalse(restore_available(self.current()))
                with self.assertRaisesRegex(RuntimeErrorBase, 'companion_restore_invalid'):
                    self.ingress.update_policy(**self.disable_args())
                self.assertEqual(self.current()['id'], active['id'])

    async def test_exit_rejects_mixed_behavior_fields_selection_and_stale_version(self):
        self.enable(); before = self.current()
        for field, value in {'reply_mode': 'legacy_full', 'context_mode': 'legacy_recent',
                'cross_session': False, 'disposition_mode': 'baseline', 'situation_observation_mode': 'disabled',
                'character_retrieval_mode': 'hybrid', 'conversation_policy_version': 2,
                'expression_policy_version': 3}.items():
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeErrorBase, 'companion_restore_conflict'):
                self.ingress.update_policy(**self.disable_args(**{field: value}))
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_restore_conflict'):
            self.ingress.update_policy(**(self.disable_args() | {'mode': 'legacy_per_message'}))
        with self.assertRaisesRegex(RuntimeErrorBase, 'persona_selection_conflict'):
            self.ingress.update_policy(**self.disable_args(expected_selected='different-persona'))
        with self.assertRaisesRegex(RuntimeErrorBase, 'version_conflict'):
            self.ingress.update_policy(**(self.disable_args() | {'expected_version': 'stale'}))
        self.assertEqual(self.current()['id'], before['id'])

    async def test_disable_seals_pending_with_accepted_candidate_and_preserves_queued_snapshot(self):
        self.enable()
        with patch.object(self.engine, '_schedule'):
            receipt = self.ingress.accept({'text': '人工收集中消息', 'request_id': identifier(), 'channel': 'desktop'})
            with self.assertRaisesRegex(RuntimeErrorBase, 'pending_batch_resolution_required'):
                self.ingress.update_policy(**self.disable_args())
            self.ingress.update_policy(**self.disable_args(pending_action='seal'))
        raw = self.raw(self.ingress.get(receipt['batch_id'])['turn_id'])
        self.assertEqual(read(raw), choose('minimal_structural'))
        snapshot = raw['model_snapshot']; context = raw['context_policy_snapshot']
        with patch.object(self.engine, '_schedule'):
            self.enable()
        self.assertEqual(self.raw(raw['id'])['model_snapshot'], snapshot)
        self.assertEqual(self.raw(raw['id'])['context_policy_snapshot'], context)
        self.assertNotIn(RESTORE_KEY, json.loads(context))
        self.model.complete.assert_not_awaited()

    async def test_disable_cancel_pending_keeps_input_and_creates_no_turn(self):
        self.enable()
        with patch.object(self.engine, '_schedule'):
            receipt = self.ingress.accept({'text': '人工待取消消息', 'request_id': identifier(), 'channel': 'desktop'})
            self.ingress.update_policy(**self.disable_args(pending_action='cancel'))
        batch = self.ingress.get(receipt['batch_id'])
        self.assertEqual(batch['state'], 'cancelled')
        self.assertIsNone(batch['turn_id'])
        self.assertEqual(batch['members'][0]['text'], '人工待取消消息')

    async def test_operation_write_failure_rolls_back_restore_and_pending_resolution(self):
        active = self.enable()
        with patch.object(self.engine, '_schedule'):
            receipt = self.ingress.accept({'text': '人工原子性输入', 'request_id': identifier(), 'channel': 'desktop'})
            with patch.object(self.ingress, '_remember', side_effect=RuntimeError('injected write failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected write failure'):
                    self.ingress.update_policy(**self.disable_args(pending_action='cancel'))
        self.assertEqual(self.current()['id'], active['id'])
        self.assertEqual(self.ingress.get(receipt['batch_id'])['state'], 'collecting')

    async def test_restore_assist_and_live_rechecks_admission_before_any_batch_change(self):
        self.enable(); original = json.loads(self.current()['data_json'])
        for mode in ('assist', 'live'):
            data = deepcopy(original); data[RESTORE_KEY]['data']['context']['disposition_mode'] = mode
            before = self.install_data(data)
            with patch('persona_runtime.decision_capsule.require_admission',
                       side_effect=RuntimeErrorBase('test_admission_denied')) as check:
                with self.assertRaisesRegex(RuntimeErrorBase, 'test_admission_denied'):
                    self.ingress.update_policy(**self.disable_args())
                self.assertTrue(any(call.args == (self.engine, self.pid, mode) for call in check.call_args_list))
            self.assertEqual(self.current()['id'], before['id'])
        with patch('persona_runtime.decision_capsule.require_admission', return_value=None):
            restored = self.ingress.update_policy(**self.disable_args())
        self.assertEqual(restored['disposition_mode'], 'live')


if __name__ == '__main__':
    unittest.main()
