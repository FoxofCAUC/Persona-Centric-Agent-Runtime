"""Native text and final example exclusion through normal admission and lifecycle."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import json

from experiments import companion_c2_chat as trial
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request, request_timeout
from persona_runtime.companion import checked, choose
from persona_runtime.reply_batch import expression_snapshot
from persona_runtime.companion_native import consume, FORMAT_VERSION
from persona_runtime.companion_prompt_light import INSTRUCTION, CONTRACT
from tests import test_companion_c2_chat as fixture


class NativeChat(fixture.C2ChatTests):
    native_text = True

    def runtime(self, model):
        model.native = True
        return super().runtime(model)

    def test_profile_and_no_reopen_or_mixed_modes(self):
        self.assertEqual(self.owner['version'], trial.NATIVE_VERSION)
        self.assertEqual(self.plan['profile'], choose('minimal_native'))
        for mode in ('prompt_light', 'integration', 'persona_purpose', 'context_rules_off'):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeErrorBase, 'conflicting_trial_modes'):
                trial.prepare(self.binding, self.source, native_text=True, **{mode: True})
        with self.assertRaises(RuntimeErrorBase):
            checked(self.plan['profile'] | {'format_contract': 'unknown'})
        with self.assertRaisesRegex(RuntimeErrorBase, 'legacy_reopen_forbidden'):
            trial.begin(self.manifest, reopen=True)

    def test_plain_service_matches_audited_service_and_plain_reply_is_unchanged(self):
        calls = []
        for audited in (True, False):
            if not audited:
                self.manifest = trial.prepare(self.binding, self.source, native_text=True)
                self.owner, self.plan = trial.verify(self.manifest)
                self.paths = RuntimePaths(Path(self.owner['home']))
            model = fixture.FixtureClient()
            runtime = self.runtime(model)
            with nullcontext() if audited else patch.object(trial, 'install', return_value=None):
                with fixture.NetworkFence() as fence, runtime:
                    fence.port = runtime.port
                    client = LocalClient(ConfigStore(self.paths))
                    client.require_capability('companion_native_text_v1')
                    row = self.wait_turn(client, self.submit(client, 'Artificial native chat.'))
                    self.assertEqual(row['status'], 'completed', row['error_code'])
                    result = runtime.app['engine'].get(row['id'])
                    self.assertEqual(result['reply'], 'Artificial native reply.\n\n```python\nprint(168)\n```')
                    self.assertEqual(result['reply_batch']['segment_count'], 1)
            config, messages = model.calls[0]
            self.assertTrue(messages[0]['content'].startswith(INSTRUCTION))
            self.assertNotIn(CONTRACT, messages[0]['content'])
            self.assertNotIn('behavior_intent', messages[0]['content'])
            self.assertEqual(config['_reply_mode'], 'context_full')
            wire, transport = build_request(config, messages)
            self.assertNotIn('response_format', wire)
            self.assertNotIn('max_tokens', wire)
            self.assertNotIn('max_completion_tokens', wire)
            self.assertTrue(wire['stream'])
            self.assertIsNone(request_timeout(config))
            self.assertEqual(transport['response_format'], 'text')
            # The artificial provider is unknown; a Grok route still keeps low.
            grok_wire, _ = build_request(config | {'name': 'grok-4.7'}, messages)
            self.assertEqual(grok_wire['reasoning_effort'], 'low')
            with self.assertRaisesRegex(ValueError, 'companion_native_wire_conflict'):
                build_request(config | {'_reply_mode': 'natural_structured',
                                        '_expression_snapshot': expression_snapshot('natural_structured')}, messages)
            if audited:
                report = trial.audit(self.manifest)
                self.assertEqual(report['rows'][0]['material_usage']['format_renderer'], FORMAT_VERSION)
                self.assertTrue(report['rows'][0]['manifest_matches'])
                self.assertFalse(any(report['auxiliary_calls'].values()))
            calls.append(model.calls[0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(build_request(*calls[0]), build_request(*calls[1]))

    def test_examples_only_excluded_without_rewriting_other_parts(self):
        parts = [{'kind': 'anchor', 'id': 'identity.gender', 'text': 'unchanged anchor'},
                 {'kind': 'example', 'id': 'example-1', 'text': 'Artificial dialogue demonstration'},
                 {'kind': 'fact', 'id': 'age.years', 'text': 'unchanged fact'}]
        kept, record = consume(parts)
        self.assertEqual(kept, [parts[0], parts[2]])
        self.assertEqual(record['excluded_parts'], [{'kind': 'example', 'id': 'example-1',
                                                    'reason': 'native_dialogue_examples_off'}])
        self.assertEqual(len(parts), 3)

    def test_structured_snapshot_cannot_be_used_as_native(self):
        from persona_runtime.companion import read, GENERATION_PROFILE
        from persona_runtime.reply_batch import expression_snapshot
        profile = choose('minimal_native')
        turn = {'model_snapshot': {'_companion_profile': profile, '_generation_profile': GENERATION_PROFILE},
                'context_policy_snapshot': {'companion': profile, 'character_retrieval': {'mode': 'hybrid'}},
                'reply_mode_snapshot': 'natural_structured',
                'expression_snapshot': expression_snapshot('natural_structured'), 'channel': 'desktop'}
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_format_conflict'):
            read(turn)


class ActualInputs(fixture.unittest.TestCase):
    def test_same_budget_material_history_except_examples_and_protocol(self):
        from tests.prompt_baseline_fixture import capture
        from persona_runtime.context_m4 import build_context
        from persona_runtime.companion import freeze_model
        from copy import deepcopy
        def builder(mode):
            def render(engine, db, turn, **kwargs):
                row = deepcopy(turn)
                policy = {'companion': choose(mode), 'character_retrieval': {'mode': 'hybrid'},
                          'disposition_mode': 'baseline', 'situation_observation_mode': 'disabled'}
                row['context_policy_snapshot'] = json.dumps(policy)
                row['model_snapshot'] = json.dumps(freeze_model(json.loads(row['model_snapshot']), policy))
                return build_context(engine, db, row, character=kwargs['character'])
            return render
        # The frozen artificial R0 material includes an eligible example.
        original = capture({'mode': 'natural_structured', 'expression': 3}, builder('minimal_experience'))
        native = capture({'mode': 'context_full', 'expression': 3}, builder('minimal_native'))
        self.assertNotIn('error_code', native)
        self.assertTrue(original['details']['character']['example_ids'])
        self.assertEqual(native['details']['character']['example_ids'], [])
        self.assertTrue(any(r.get('reason') == 'native_dialogue_examples_off'
                            for r in native['details']['character']['removed']))
        self.assertEqual(original['messages'][1:], native['messages'][1:])
        self.assertNotIn('behavior_intent', native['messages'][0]['content'])
        self.assertNotIn('"kind": "example"', native['messages'][0]['content'])
        self.assertIn('人物资料', native['messages'][0]['content'])


class NativeDesktop(fixture.unittest.TestCase):
    from tests import test_companion_workbench_desktop as desktop_fixture
    setUp = desktop_fixture.CompanionDesktopTests.setUp
    window = desktop_fixture.CompanionDesktopTests.window
    doCleanups = desktop_fixture.CompanionDesktopTests.doCleanups
    ready = desktop_fixture.CompanionDesktopTests.ready

    def test_actual_native_policy_is_named_and_conflicting_controls_disabled(self):
        value = self.desktop_fixture.policy(True)
        value['companion'] = choose('minimal_native')
        data = json.loads(value['data_json'])
        data['companion'] = value['companion']
        value.update(data_json=json.dumps(data), reply_mode='context_full')
        w = self.ready(value)
        w.remember_companion_policy(value)
        self.assertEqual(w.ctx_companion_enable.cget('text'), '升级伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')), 'normal')
        self.assertIn('无示范纯文本', w.chat_mode_text.get())
        self.assertEqual(str(w.ctx_character_combo.cget('state')), 'disabled')
