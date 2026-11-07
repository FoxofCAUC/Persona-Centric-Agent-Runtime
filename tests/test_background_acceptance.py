"""Failure receipts and no-replay gates for the optional background plugin."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins.background_acceptance import runner, materials
from persona_runtime.config import atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import DEFAULT_HOME

class BackgroundAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp')
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_started_run_refuses_even_when_no_result_was_saved(self):
        atomic_json(self.out / 'started.json', {'possible_unknown_dispatch': True})
        with patch.object(runner, 'verify', return_value=({'home': str(self.out)}, {'simulated': True})), \
             patch.object(runner, 'spawn_worker') as spawn:
            with self.assertRaisesRegex(RuntimeErrorBase, 'already_started_no_replay'):
                runner.execute(self.out)
            spawn.assert_not_called()

    def test_failed_call_retains_receipt_and_unknown_usage_without_regeneration(self):
        atomic_json(self.out / 'active.json', {'id': 'T001', 'kind': 'chat'})
        class Failing:
            calls = 0
            async def complete(self, model, messages):
                self.calls += 1
                raise RuntimeErrorBase('provider_network_error')
        client = runner.RecordedClient(self.out, False)
        client.inner = Failing()
        with patch.object(runner, 'verify'), patch.object(runner, 'build_request', return_value=({'messages': []}, {})):
            with self.assertRaisesRegex(RuntimeErrorBase, 'provider_network_error'):
                asyncio.run(client.complete({}, [{'role': 'user', 'content': '人工消息'}]))
        rows = [runner.read(p) for p in (self.out / 'calls/T001').glob('*.json')]
        self.assertEqual(client.inner.calls, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertIsNone(rows[0]['usage'])

    def test_exact_recall_probes_do_not_repeat_their_answer_between_seed_and_probe(self):
        rows = materials.dialogue()
        self.assertEqual(len(rows), 100)
        for n, p in materials.PROBES.items():
            self.assertGreater(n - p['source'], p['threshold'])
            answers = p.get('all', []) + p.get('any', [])
            # The strict nonce probes provide the strongest leakage check.
            if n in (14, 80):
                for answer in answers:
                    self.assertIn(answer, rows[p['source'] - 1]['text'])
                    self.assertNotIn(answer, '\n'.join(r['text'] for r in rows[p['source']:n]))

    def test_wrong_answer_and_superseded_plan_remain_failures(self):
        self.assertFalse(materials.evaluate(80, '白帆回湾')['answer_check_passed'])
        self.assertTrue(materials.evaluate(80, '白帆入湾')['answer_check_passed'])
        self.assertFalse(materials.evaluate(93, '东馆石版')['answer_check_passed'])
        self.assertTrue(materials.evaluate(93, '西馆玻璃艺术')['answer_check_passed'])

    def test_known_rejected_memory_output_is_recorded_without_accepting_or_retrying(self):
        atomic_json(self.out / 'calls/M050/one.json', {'status': 'completed'})
        class API:
            actions = []
            def manage(self, action, **kwargs):
                self.actions.append(action)
                if action == 'organization.preview':
                    return {'rows': list(range(41, 51))}
                if action == 'organization.submit': return {'id': 'one'}
                if action == 'organization.show':
                    return {'id': 'one', 'status': 'failed', 'error_code': 'organization_invalid_output'}
                raise AssertionError(action)
        api = API()
        runner.organize(api, self.out, 'session', 50)
        self.assertEqual(api.actions.count('organization.submit'), 1)
        self.assertNotIn('candidate.accept', api.actions)
        self.assertEqual(runner.read(self.out / 'organization/M050-result.json')['task']['status'], 'failed')

    def test_continuation_keeps_original_prefix_and_owner_immutable(self):
        atomic_json(self.out / 'owner.json', {'home': str(self.out), 'code_hashes': {}})
        atomic_json(self.out / 'progress.json', {'status':'stopped', 'error':'background_acceptance_organization_failed',
                                               'completed_rounds':50, 'opening':2})
        atomic_json(self.out / 'organization/M050-result.json',
                    {'task': {'status':'failed', 'error_code':'organization_invalid_output'}})
        atomic_json(self.out / 'openings/2/exit-verified.json', {'snapshot':{}})
        atomic_json(self.out / 'calls/M050/one.json', {'status':'completed'})
        for n in range(1,51):
            atomic_json(self.out / 'results' / f'T{n:03}.json', {'number':n})
        before = runner.sha(self.out / 'owner.json')
        with patch.object(runner, 'snapshot', return_value={}):
            runner.continue_plan(self.out)
        c = runner.read(self.out / 'continuation.json')
        self.assertEqual(runner.sha(self.out / 'owner.json'), before)
        self.assertEqual(c['completed_rounds'], 50)
        self.assertIn('results/T050.json', c['preserved'])
        self.assertNotIn('intents/T051.json', c['preserved'])

if __name__ == '__main__':
    unittest.main()
