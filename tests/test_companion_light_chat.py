"""v6 through the owned normal Workbench; artificial data and local clients only."""
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from experiments import companion_c2_chat as trial
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request, request_timeout
from persona_runtime import companion_prompt_light as light
from tests import test_companion_c2_chat as fixture


class LightChat(fixture.C2ChatTests):
    prompt_light = True

    def test_light_binding_and_one_shot_mode_are_explicit(self):
        self.assertEqual(self.owner['version'], trial.LIGHT_VERSION)
        self.assertEqual(self.manifest.parent.parent.name, trial.LIGHT_NAMESPACE)
        self.assertEqual(self.plan['approved_versions'], self.owner['approved_versions'])
        self.assertEqual(self.plan['profile'], trial.choose('minimal_experience'))
        with self.assertRaisesRegex(RuntimeErrorBase, 'legacy_reopen_forbidden'):
            trial.begin(self.manifest, reopen=True)
        for mode in ('integration', 'persona_purpose', 'context_rules_off'):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeErrorBase, 'conflicting_trial_modes'):
                trial.prepare(self.binding, self.source, prompt_light=True, **{mode: True})

    def test_plain_and_audited_service_use_exact_light_messages_and_wire(self):
        calls = []
        for audited in (True, False):
            if not audited:
                self.manifest = trial.prepare(self.binding, self.source, prompt_light=True)
                self.owner, self.plan = trial.verify(self.manifest)
                self.paths = RuntimePaths(Path(self.owner['home']))
            model = fixture.FixtureClient()
            runtime = self.runtime(model)
            with nullcontext() if audited else patch.object(trial, 'install', return_value=None):
                with fixture.NetworkFence() as fence, runtime:
                    fence.port = runtime.port
                    client = LocalClient(ConfigStore(self.paths))
                    row = self.wait_turn(client, self.submit(client, '你喜欢什么样的休息方式？'))
                    self.assertEqual(row['status'], 'completed', row['error_code'])
            model_config, messages = model.calls[0]
            self.assertTrue(messages[0]['content'].startswith(light.INSTRUCTION))
            self.assertIn(light.CONTRACT, messages[0]['content'])
            self.assertNotIn(trial.choose('minimal_persona')['persona_renderer'], messages[0]['content'])
            wire, transport = build_request(model_config, messages)
            self.assertTrue(wire['stream'])
            self.assertIsNone(request_timeout(model_config))
            self.assertEqual(transport['timeout_policy'], 'none')
            self.assertNotIn('max_tokens', wire)
            self.assertNotIn('max_completion_tokens', wire)
            if audited:
                usage = trial.audit(self.manifest)['rows'][0]['material_usage']
                self.assertEqual(usage['status'], 'collected')
                self.assertEqual(usage['expression_renderer'], light.INSTRUCTION_VERSION)
                self.assertEqual(usage['format_renderer'], light.FORMAT_RENDERER)
                self.assertEqual(usage['s8']['status'], 'not_reviewed')
                self.assertIn('core.values_and_boundaries', usage['core_field_ids'])
                self.assertNotIn('value', str(usage['final_character_parts']))
            calls.append(model.calls[0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(build_request(*calls[0]), build_request(*calls[1]))

    def test_outdated_author_binding_stops_before_start(self):
        original = self.binding.read_bytes()
        self.binding.write_bytes(original + b'\n')
        with self.assertRaisesRegex(RuntimeErrorBase, 'approval_changed'):
            trial.verify(self.manifest)
        self.binding.write_bytes(original)
