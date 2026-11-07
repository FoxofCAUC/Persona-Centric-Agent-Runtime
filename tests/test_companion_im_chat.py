"""v8 normal admission and IM wording; artificial data and local clients only."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from experiments import companion_c2_chat as trial
from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose, checked, freeze_model, read, GENERATION_PROFILE
from persona_runtime.companion_im import GUIDANCE, INSTRUCTION, INSTRUCTION_VERSION, render_blocks
from persona_runtime.companion_prompt_light import INSTRUCTION as PREVIOUS_INSTRUCTION
from persona_runtime.config import ConfigStore
from persona_runtime.context_m4 import build_context
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.prompt_assembly import PromptBlock
from persona_runtime.provider_compat import build_request, request_timeout
from persona_runtime.reply_batch import expression_snapshot
from tests import test_companion_c2_chat as fixture
from tests.prompt_baseline_fixture import capture


def builder(mode, diagnostics=None):
    def render(engine, db, turn, **kwargs):
        row = deepcopy(turn)
        policy = {'companion': choose(mode), 'character_retrieval': {'mode': 'hybrid'},
                  'disposition_mode': 'baseline', 'situation_observation_mode': 'disabled'}
        row['context_policy_snapshot'] = json.dumps(policy)
        row['model_snapshot'] = json.dumps(freeze_model(json.loads(row['model_snapshot']), policy))
        result = build_context(engine, db, row, character=kwargs['character'])
        if diagnostics is not None:
            diagnostics.append(result[1]['prompt_assembly'])
        return result
    return render


class IMChat(fixture.C2ChatTests):
    im_style = True
    model_name = 'deepseek-flash'

    def runtime(self, model):
        model.native = True
        return super().runtime(model)

    def test_profile_and_one_shot_mode_are_explicit(self):
        self.assertEqual(self.owner['version'], trial.IM_VERSION)
        self.assertEqual(self.manifest.parent.parent.name, trial.IM_NAMESPACE)
        self.assertEqual(self.plan['profile'], choose('minimal_im'))
        self.assertEqual(self.plan['approved_versions'], self.owner['approved_versions'])
        for mode in ('native_text', 'prompt_light', 'integration', 'persona_purpose', 'context_rules_off'):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeErrorBase, 'conflicting_trial_modes'):
                trial.prepare(self.binding, self.source, im_style=True, **{mode: True})
        with self.assertRaisesRegex(RuntimeErrorBase, 'legacy_reopen_forbidden'):
            trial.begin(self.manifest, reopen=True)

    def test_normal_and_audited_service_match_and_keep_native_wire(self):
        calls = []
        for audited in (True, False):
            if not audited:
                self.manifest = trial.prepare(self.binding, self.source, im_style=True)
                self.owner, self.plan = trial.verify(self.manifest)
                self.paths = RuntimePaths(Path(self.owner['home']))
            model = fixture.FixtureClient()
            runtime = self.runtime(model)
            with nullcontext() if audited else patch.object(trial, 'install', return_value=None):
                with fixture.NetworkFence() as fence, runtime:
                    fence.port = runtime.port
                    client = LocalClient(ConfigStore(self.paths))
                    client.require_capability('companion_im_text_v1')
                    result = self.wait_turn(client, self.submit(client, 'Artificial IM share.'))
                    self.assertEqual(result['status'], 'completed', result['error_code'])
                    actual = runtime.app['engine'].get(result['id'])
                    self.assertEqual(actual['reply'], 'Artificial native reply.\n\n```python\nprint(168)\n```')
            config, messages = model.calls[0]
            self.assertTrue(messages[0]['content'].startswith(INSTRUCTION))
            self.assertEqual(messages[0]['content'].count(GUIDANCE), 1)
            wire, transport = build_request(config, messages)
            self.assertEqual(wire['thinking'], {'type': 'disabled'})
            for field in ('response_format', 'max_tokens', 'max_completion_tokens', 'reasoning_effort', 'temperature', 'top_p', 'seed'):
                self.assertNotIn(field, wire)
            self.assertTrue(wire['stream'])
            self.assertIsNone(request_timeout(config))
            self.assertEqual(transport['response_format'], 'text')
            if audited:
                report = trial.audit(self.manifest)
                self.assertEqual(report['rows'][0]['material_usage']['expression_renderer'], INSTRUCTION_VERSION)
                self.assertTrue(report['rows'][0]['manifest_matches'])
                self.assertTrue(report['rows'][0]['runtime_audit_matches'])
                self.assertFalse(any(report['auxiliary_calls'].values()))
            calls.append(model.calls[0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(build_request(*calls[0]), build_request(*calls[1]))

    def test_punctuation_numbers_urls_and_long_task_text_are_not_cleaned(self):
        text = '值是3.14，文件a.py，网址https://example.invalid/a.b。\n问号？省略号……\n> 原文。\n```python\nprint("done.")\n```\n' + '完整解释。' * 80
        class Exact(fixture.FixtureClient):
            async def complete(self, model, messages):
                self.calls.append((model, messages))
                return text, {'total_tokens': 3}
        model = Exact()
        runtime = self.runtime(model)
        with fixture.NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            row = self.wait_turn(client, self.submit(client, 'Artificial complete task.'))
            self.assertEqual(row['status'], 'completed', row['error_code'])
            actual = runtime.app['engine'].get(row['id'])
            self.assertEqual(actual['reply'], text)
            self.assertEqual(actual['reply_batch']['segment_count'], 1)


class InputAndVersion(fixture.unittest.TestCase):
    def test_only_the_exact_guidance_changes_in_final_model_input(self):
        case = {'mode': 'context_full', 'expression': 3}
        previous = capture(case, builder('minimal_native'))
        diagnostics = []
        current = capture(case, builder('minimal_im', diagnostics))
        self.assertNotIn('error_code', current)
        self.assertEqual(previous['messages'][1:], current['messages'][1:])
        self.assertEqual(current['messages'][0]['content'].replace(GUIDANCE, '', 1), previous['messages'][0]['content'])
        self.assertEqual(previous['details']['character'], current['details']['character'])
        self.assertFalse(current['details']['character']['example_ids'])
        assembly = diagnostics[0]
        self.assertEqual(assembly['input_snapshot_hash'], fingerprint(current['messages']))
        self.assertEqual(assembly['expression_renderer'], INSTRUCTION_VERSION)
        self.assertEqual(assembly['format_renderer'], 'native-companion-text-v1')
        self.assertTrue(all(b['characters'] == 0 for b in assembly['blocks'] if b['name'] == 'reply_protocol'))
        self.assertNotIn('behavior_intent', current['messages'][0]['content'])
        self.assertEqual(INSTRUCTION.replace(GUIDANCE, '', 1), PREVIOUS_INSTRUCTION)

    def test_wrong_instruction_unknown_version_and_snapshot_mismatch_fail(self):
        profile = choose('minimal_im')
        for changed in (profile | {'instruction_version': 'companion-experience-instruction-v1'},
                        profile | {'version': 'companion-runtime-unknown'}):
            with self.assertRaises(RuntimeErrorBase):
                checked(changed)
        turn = {'model_snapshot': {'_companion_profile': profile, '_generation_profile': GENERATION_PROFILE},
                'context_policy_snapshot': {'companion': choose('minimal_native'), 'character_retrieval': {'mode': 'hybrid'}},
                'reply_mode_snapshot': 'context_full', 'expression_snapshot': expression_snapshot('context_full'), 'channel': 'desktop'}
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_snapshot_mismatch'):
            read(turn)
        turn['context_policy_snapshot']['companion'] = profile
        turn['reply_mode_snapshot'] = 'natural_structured'
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_format_conflict'):
            read(turn)
        with self.assertRaisesRegex(RuntimeErrorBase, 'companion_im_instruction_boundary_changed'):
            render_blocks([PromptBlock('persona_expression', 'unknown', 'test', 'test')])


class IMDesktop(fixture.unittest.TestCase):
    from tests import test_companion_workbench_desktop as desktop_fixture
    setUp = desktop_fixture.CompanionDesktopTests.setUp
    window = desktop_fixture.CompanionDesktopTests.window
    doCleanups = desktop_fixture.CompanionDesktopTests.doCleanups
    ready = desktop_fixture.CompanionDesktopTests.ready

    def test_actual_im_controls_and_old_service_guard(self):
        value = self.desktop_fixture.policy(True)
        value['companion'] = choose('minimal_im')
        data = json.loads(value['data_json']); data['companion'] = value['companion']
        value.update(data_json=json.dumps(data), reply_mode='context_full')
        w = self.ready(value)
        w.remember_companion_policy(value)
        self.assertEqual(w.ctx_companion_enable.cget('text'), '已启用伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')), 'disabled')
        self.assertIn('即时通讯', w.chat_mode_text.get())
        self.assertEqual(str(w.ctx_character_combo.cget('state')), 'disabled')
        client = LocalClient(w.config)
        with patch.object(client, 'call', return_value={'capabilities': ['companion_native_text_v1']}) as request:
            with self.assertRaisesRegex(RuntimeErrorBase, 'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_im')
        self.assertEqual(request.call_count, 1)
