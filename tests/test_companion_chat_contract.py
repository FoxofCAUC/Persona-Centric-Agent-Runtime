"""C2 contract and normal-runtime regression checks with artificial material."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose, checked, read
from persona_runtime.companion_contract import structural_contract
from persona_runtime.companion_chat_contract import REPLACEMENTS, RENDERER, chat_contract
from persona_runtime.models import identifier, RuntimeErrorBase
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply, expression_snapshot
from tests import test_m753 as fixture


class Contract(unittest.TestCase):
    def test_only_listed_clauses_change(self):
        expected = structural_contract()
        for _, old, new in REPLACEMENTS:
            self.assertEqual(expected.count(old), 1)
            expected = expected.replace(old, new)
        self.assertEqual(chat_contract(), expected)
        self.assertIn('嗯、好等短回应也可以', expected)
        self.assertIn('当前有明确任务时', expected)
        self.assertIn('完成当前明确请求不可缺少的内容必须cancel_if_new_input=false', expected)
        for text in ('回答完整性', '补充或结束语', '8至35字', '8000', '12000'):
            self.assertNotIn(text, expected)
        with self.assertRaises(RuntimeErrorBase):
            chat_contract('changed upstream protocol')

    def test_profile_is_explicit_and_wrong_contract_rejected(self):
        self.assertEqual(choose('minimal_chat')['format_contract'], 'schema3-companion-v2')
        self.assertEqual(choose('minimal_structural')['version'], 'companion-runtime-v2')
        with self.assertRaises(RuntimeErrorBase):
            checked(choose('minimal_chat') | {'format_contract': 'schema3-structural-v1'})

    def test_short_long_code_and_cancel_metadata_use_existing_parser(self):
        for bodies in (['好'], ['一条自然接话', '另一条有关的想法'], ['详细解释' * 70]):
            raw = {'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
                {'content': text, 'content_kind': 'chat', 'cancel_if_new_input': False} for text in bodies]}
            batch = validate_reply(json.dumps(raw), 'natural_structured', expression_snapshot('natural_structured'))
            self.assertEqual([s.text for s in batch.segments], bodies)
        raw['messages'] = [{'content': '```python\nprint(1)\n```', 'content_kind': 'code', 'cancel_if_new_input': True}]
        batch = validate_reply(json.dumps(raw), 'natural_structured', expression_snapshot('natural_structured'))
        self.assertTrue(all(not s.cancel_if_new_input for s in batch.segments))


class Runtime(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.IntegrationTests.setUp
    save = fixture.IntegrationTests.save
    hybrid = fixture.IntegrationTests.hybrid
    policy = fixture.IntegrationTests.policy
    raw = fixture.IntegrationTests.raw
    turn = fixture.IntegrationTests.turn

    def enable(self, mode):
        old = self.ingress.current_policy(self.pid, 'desktop')
        return self.ingress.update_policy(self.pid, 'desktop', old['mode'], old['id'], identifier(),
                                         companion_mode=mode, reply_mode='natural_structured', expression_policy_version=3)

    async def test_replay_policy_switch_leaves_local_preparation_to_runner(self):
        from experiments.companion_c2 import enable
        callback = Mock(side_effect=AssertionError('competing_background_prepare'))
        self.store.character_committed = callback
        enable(self.engine, self.pid, 'candidate')
        callback.assert_not_called()
        self.assertIs(self.store.character_committed, callback)
        self.assertEqual(self.ingress.current_policy(self.pid, 'desktop')['companion'], choose('minimal_chat'))

    async def test_normal_admission_final_manifest_and_single_generation(self):
        self.enable('minimal_chat')
        turn = await self.turn('书店')
        raw = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
            {'content': '好', 'content_kind': 'chat', 'cancel_if_new_input': False}]})
        with patch.object(self.rt, '_hybrid', side_effect=self.hybrid), \
             patch.object(self.model, 'complete', new=AsyncMock(return_value=(raw, None))) as client:
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'], 'completed')
        self.assertEqual(client.await_count, 1)
        model, messages = client.call_args.args
        self.assertTrue(messages[0]['content'].endswith(chat_contract()))
        self.assertEqual(model['_companion_profile'], choose('minimal_chat'))
        with self.store.connection() as conn:
            manifest = json.loads(conn.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',
                                              (turn['id'],)).fetchone()[0])
        self.assertEqual(manifest['input_snapshot_hash'], fingerprint(messages))
        self.assertEqual(manifest['prompt_assembly']['effective']['format'], RENDERER)

    async def test_old_accepted_snapshot_survives_policy_switch(self):
        self.enable('minimal_structural')
        turn = await self.turn('书店')
        before = self.raw(turn['id'])
        self.enable('minimal_chat')
        self.assertEqual(self.raw(turn['id'])['model_snapshot'], before['model_snapshot'])
        self.assertEqual(read(before), choose('minimal_structural'))
        with patch.object(self.rt, '_hybrid', side_effect=self.hybrid):
            prepared = self.engine.cognitive.prepare(turn['id'])
        self.assertTrue(prepared['messages'][0]['content'].endswith(structural_contract()))

    async def test_only_contract_changes_even_under_tight_budget(self):
        self.enable('minimal_structural')
        turn = await self.turn('书店')
        with patch.object(self.rt, '_hybrid', side_effect=self.hybrid):
            prepared = self.engine.cognitive.prepare(turn['id'])
        original = self.raw(turn['id'])

        def render(mode, limit=None):
            row = deepcopy(original)
            model = json.loads(row['model_snapshot'])
            context = json.loads(row['context_policy_snapshot'])
            model['_companion_profile'] = context['companion'] = choose(mode)
            if limit is not None:
                model['max_input_chars'] = limit
            row.update(model_snapshot=json.dumps(model), context_policy_snapshot=json.dumps(context))
            with self.store.connection() as conn:
                evidence = self.engine.cognitive.dialogue.basic_evidence(conn, row)
                messages, details = self.engine.cognitive.dialogue.render(conn, row, evidence, prepared['character_prepared'])
            return messages, details, build_request(model, messages)[0]

        initial = render('minimal')[0]
        for limit in (None, sum(len(m['content']) for m in initial) - 1):
            old, od, ow = render('minimal_structural', limit)
            new, nd, nw = render('minimal_chat', limit)
            self.assertEqual(new[0]['content'], old[0]['content'][:-len(structural_contract())] + chat_contract())
            self.assertEqual(old[1:], new[1:])
            self.assertEqual(od['character'], nd['character'])
            self.assertEqual(od['removed'], nd['removed'])
            self.assertEqual({k: v for k, v in ow.items() if k != 'messages'},
                             {k: v for k, v in nw.items() if k != 'messages'})

    async def test_new_contract_rejects_wrong_expression_before_generation(self):
        self.enable('minimal_chat')
        turn = await self.turn('书店')
        with self.store.connection(write=True) as conn:
            conn.execute('UPDATE turns SET expression_snapshot=? WHERE id=?',
                         (json.dumps({'version': 2}), turn['id']))
        await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['error_code'], 'companion_format_conflict')
        self.assertFalse(self.model.calls)
