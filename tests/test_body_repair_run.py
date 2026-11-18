"""Research journal regression checks with numeric local markers only."""
import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_store import fingerprint
from persona_runtime.provider_compat import build_request
from experiments.body_repair_run import APPROVAL_VERSION, VERSION, RepairTransport, execute, load_results, slot_ledger
from experiments.corpus_reply_study import save_once
from experiments.corpus_reply_trial import public_model


class RepairRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'research'
        self.manifest = self.root / 'manifest.private.json'
        self.approval = self.root / 'approval.private.json'
        self.model = public_model('deepseek-v4-pro', 1.0)
        self.sent = []
        self.key_reads = 0
        self.callback = None

    def freeze(self, n=2, reused=(), **changes):
        positions = []
        for i in range(1, n + 1):
            req = build_request(self.model, [{'role': 'user', 'content': str(i)}])[0]
            positions.append({'position': i, 'model': self.model, 'request': req, 'request_hash': fingerprint(req),
                              'reuse': {'state': 'completed', 'body': '2', 'raw_hash': '00'} if i in reused else None})
        sha = save_once(self.manifest, {'version': VERSION, 'positions': positions})
        save_once(self.approval, {'version': APPROVAL_VERSION, 'manifest_sha256': sha,
                                  'manifest_path': str(self.manifest), 'author_message': '0',
                                  'max_dispatches': None, 'retries': 0} | changes)

    def credentials(self):
        self.key_reads += 1
        return {'base_url': self.model['base_url'], 'encrypted_key': '00'}

    def preflight(self, *args):
        pass

    async def complete(self, model, request, *, save_raw):
        n = int(request['messages'][0]['content'])
        self.sent.append(n)
        if self.callback:
            self.callback(n, save_raw)
        raw = json.dumps({'model': model['name'], 'choices': [{'message': {'content': str(n)}, 'finish_reason': 'stop'}]}).encode()
        save_raw(raw, http_status=200, diagnostics={})
        return {}

    def run_study(self, **kwargs):
        with redirect_stdout(StringIO()):
            return asyncio.run(execute(self.manifest, self.approval, client=self, load_credentials=self.credentials, **kwargs))

    def test_reused_and_successful_receipts_do_not_redispatch(self):
        self.freeze(reused=[2])
        first = self.run_study()
        self.assertEqual(first, load_results(self.manifest, self.approval))
        self.assertEqual(first, self.run_study())
        self.assertEqual(self.sent, [1])
        self.assertEqual(self.key_reads, 1)
        self.assertEqual(first['calls'][0]['body'], '1')
        self.assertTrue(first['calls'][1]['reused'])
        self.assertFalse(slot_ledger(self.root, 2).path.exists())

    def test_lost_original_db_blocks_instead_of_restarting(self):
        self.freeze()
        self.run_study()
        slot_ledger(self.root, 1).path.unlink()
        with self.assertRaisesRegex(CorpusError, 'missing_original_journal'):
            self.run_study()
        self.assertEqual(self.sent, [1, 2])

    def test_unknown_does_not_replay_or_suspend_an_independent_position(self):
        self.freeze()
        def uncertain(n, save):
            if n == 1:
                raise TimeoutError()
        self.callback = uncertain
        first = self.run_study()
        self.assertEqual([r['state'] for r in first['calls']], ['unknown', 'completed'])
        self.assertEqual(first, self.run_study())
        self.assertEqual(self.sent, [1, 2])

    def test_complete_raw_is_recovered_even_if_transport_exits_later(self):
        self.freeze(n=1)
        def crash(n, save):
            raw = json.dumps({'model': self.model['name'], 'choices': [{'message': {'content':'1'}, 'finish_reason':'stop'}]}).encode()
            save(raw, http_status=200, diagnostics={})
            raise asyncio.CancelledError()
        self.callback = crash
        with self.assertRaises(asyncio.CancelledError):
            self.run_study()
        self.assertEqual(self.run_study()['calls'][0]['state'], 'completed')
        self.assertEqual(self.sent, [1])

    def test_revocation_stops_at_next_request(self):
        self.freeze()
        def guard(row):
            if row['position'] == 2:
                raise CorpusError('900')
        with self.assertRaisesRegex(CorpusError, '900'):
            self.run_study(per_position_guard=guard)
        self.assertEqual(self.sent, [1])
        self.assertEqual([r['state'] for r in load_results(self.manifest, self.approval)['calls']], ['completed','prepared'])

    def test_stale_scope_and_provider_credential_in_payload_block_before_send(self):
        self.freeze(max_dispatches=80)
        with self.assertRaisesRegex(CorpusError, 'scope_mismatch'):
            self.run_study()
        self.assertEqual(self.key_reads, 0)
        with patch('experiments.body_repair_run.CorpusLearningHTTPClient.preflight', return_value=('', '', '', '111')):
            with self.assertRaisesRegex(CorpusError, 'credential_in_payload'):
                RepairTransport.preflight({}, {'messages': [{'content':'111'}]})

    def test_more_than_eighty_distinct_positions_can_run(self):
        self.freeze(n=81)
        self.assertEqual(self.run_study()['attempts'], 81)
        self.assertEqual(self.sent, list(range(1, 82)))


if __name__ == '__main__':
    unittest.main()
