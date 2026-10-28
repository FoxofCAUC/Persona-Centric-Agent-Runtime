"""Offline acceptance-tool checks: no provider or platform communication."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.acceptance_m64 import Comparison, MODES, SCENARIOS, automatic_checks, schedule, shadow
from scripts.summarize_m64 import AXES, summarize
from persona_runtime.paths import RuntimePaths


class FakeClient:
    def __init__(self):
        self.sent = []
        self.sessions = []
        self.mode = None
        self.lost = False

    def manage(self, action, **args):
        if action == 'interaction.current': return {'id': 'policy'}
        if action == 'interaction.update': self.mode = args['reply_mode']; return {}
        if action == 'session.select':
            session = str(len(self.sessions)); self.sessions.append(session)
            return {'session_id': session}
        raise AssertionError(action)

    def submit_input(self, payload):
        self.sent.append((payload.copy(), self.mode))
        if self.lost: raise OSError('lost receipt')
        return {'turn_id': str(len(self.sent))}

    def lookup(self, payload):
        for i, (sent, _) in enumerate(self.sent):
            if sent['request_id'] == payload['request_id']: return {'turn_id': str(i+1)}
        return None

    def call(self, method, path):
        assert method == 'GET'
        mode = self.sent[int(path.rsplit('/', 1)[1])-1][1]
        return {'status': 'completed', 'attempt_count': 1, 'reply': '模拟回复',
                'reply_batch': {'mode': mode, 'segments': [{'text': '模拟回复'}]}}


class M64AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = RuntimePaths.from_environment().home/'tmp'
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def temporary(self):
        return tempfile.TemporaryDirectory(prefix='m64-', dir=self.temp_root)

    def fixture(self, directory):
        path = Path(directory)
        (path/'comparison.json').write_text(json.dumps({'persona_id': 'test', 'cases': schedule()}), encoding='utf-8')
        return path

    def test_complete_mock_run_isolated_and_resumable(self):
        with self.temporary() as directory:
            root = self.fixture(directory); client = FakeClient()
            runner = Comparison(client, root)
            self.assertEqual(runner.run(3), 3)
            runner = Comparison(client, root)
            self.assertEqual(runner.run(), 69)
            self.assertEqual(runner.run(), 0)
            self.assertEqual(len(client.sent), 72)
            self.assertEqual(len(set(p['session_id'] for p, _ in client.sent)), 72)
            self.assertEqual(len(set(p['request_id'] for p, _ in client.sent)), 72)
            self.assertEqual({m: sum(mode == m for _, mode in client.sent) for m in MODES}, dict.fromkeys(MODES, 24))
            self.assertNotIn('credential', (root/'comparison.json').read_text(encoding='utf-8'))

    def test_lost_receipt_uses_lookup_without_resubmit(self):
        with self.temporary() as directory:
            root = self.fixture(directory); client = FakeClient(); client.lost = True
            with self.assertRaises(OSError): Comparison(client, root).run(1)
            client.lost = False
            self.assertEqual(Comparison(client, root).run(1), 1)
            self.assertEqual(len(client.sent), 1)

    def test_unknown_intent_cannot_be_resubmitted(self):
        with self.temporary() as directory:
            root = self.fixture(directory); client = FakeClient()
            runner = Comparison(client, root); runner.data['cases'][0]['payload'] = {'request_id': 'unknown'}; runner.save()
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed intent'): runner.run(1)
            self.assertFalse(client.sent)

    def test_stop_after_five_failures_or_qq_loss(self):
        with self.temporary() as directory:
            root = self.fixture(directory); client = FakeClient()
            runner = Comparison(client, root); runner.data['consecutive_failures'] = 5
            self.assertEqual(runner.run(), 0)
            def offline(): raise RuntimeError('QQ interruption')
            with self.assertRaisesRegex(RuntimeError, 'QQ interruption'): Comparison(client, root, offline).run()
            self.assertFalse(client.sent)

    def test_fixed_schedule_and_balanced_categories(self):
        self.assertEqual(schedule(), schedule())
        self.assertEqual(len(SCENARIOS), 24)
        self.assertEqual(len(set(x['category'] for x in schedule())), 12)
        self.assertEqual(len(set(x['anonymous_id'] for x in schedule())), 72)
        for sid, _, text in SCENARIOS:
            self.assertEqual([r['text'] for r in schedule() if r['scenario'] == sid], [text]*3)

    def test_shadow_eligibility_and_equality(self):
        self.assertEqual(shadow([0, 1, 3], 5), [{'members': [0, 1, 3], 'deadline': 8}])
        self.assertEqual(shadow([0, 4, 8, 12], 5)[0]['deadline'], 15)
        self.assertEqual(len(shadow([0, 5], 5)), 2)
        self.assertEqual(len(shadow([0, 1, 3], 2)), 2)

    def test_failed_format_is_not_scored_as_success(self):
        checks = automatic_checks({'scenario': 'code1', 'mode': 'context_structured'},
            {'status': 'completed', 'attempt_count': 2, 'reply': '```python\n{"schema_version":1}',
             'reply_batch': {'mode': 'context_full', 'segments': []}})
        self.assertFalse(checks['single_attempt'])
        self.assertFalse(checks['no_internal_json'])
        self.assertFalse(checks['paired_code_fences'])
        self.assertFalse(checks['expected_mode'])
        self.assertFalse(checks['validated_reply_batch'])

    def test_summary_never_passes_without_all_reviews(self):
        self.assertEqual(summarize({'cases': []})['quality_gate'], 'pending')
        row = schedule()[0]
        row['turn'] = {'status': 'completed', 'attempt_count': 1, 'reply': 'example', 'reply_batch': {'mode': row['mode'], 'segments': [{'text': 'example'}]}}
        with self.assertRaisesRegex(ValueError, 'incomplete machine review'):
            summarize({'cases': [row]}, {row['anonymous_id']: {'scores': {}, 'reason': 'x', 'critical': False}})
        result = summarize({'cases': [row]}, {row['anonymous_id']: {'scores': dict.fromkeys(AXES, 2), 'reason': 'fixture', 'critical': True}})
        self.assertEqual(result['critical_defects'], [row['anonymous_id']])
        self.assertEqual(result['quality_gate'], 'pending')

    def test_python_code_is_parsed_not_executed(self):
        checks = automatic_checks({'scenario': 'code1', 'mode': 'legacy_full'},
            {'status': 'completed', 'attempt_count': 1, 'reply': '```python\ndef add(a, b):\n    return a+b\nassert add(2, 3) == 5\n```', 'reply_batch': {'mode': 'legacy_full'}})
        self.assertTrue(checks['python_syntax'])
        self.assertTrue(checks['complete_function_and_assert'])


if __name__ == '__main__': unittest.main()
