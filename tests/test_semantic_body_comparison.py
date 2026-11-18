"""Receipt reuse, scope and recovery contracts. Inputs are local engineering markers."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_store import fingerprint
from persona_runtime.provider_compat import build_request
from experiments.corpus_reply_study import save_once
from experiments.corpus_reply_trial import TrialLedger, public_model
from experiments.semantic_body_comparison import POSITIONS, VERSION, file_hash, reuse_slot
from experiments.semantic_body_report import pair
from experiments.semantic_body_run import execute, funded_positions


class ReuseTests(unittest.TestCase):
    def test_same_empty_request_prefers_fixed_control_even_when_both_match(self):
        q = {'messages': ['ID-1'], 'model': 'model-1'}
        self.assertEqual(reuse_slot([], q, {'D': q, 'C': q}), 'C')
        with self.assertRaises(CorpusError):
            reuse_slot([], q, {'D': q, 'C': q | {'model': 'model-2'}})

    def test_nonempty_reuses_only_the_full_ordered_wire(self):
        q = {'messages': ['FIRST-ID', 'SECOND-ID'], 'temperature': 1}
        prior = {'C': {}, 'D': q}
        self.assertEqual(reuse_slot(['ID-A'], deepcopy(q), prior), 'D')
        self.assertIsNone(reuse_slot(['ID-A'], q | {'temperature': .3}, prior))
        self.assertIsNone(reuse_slot(['ID-A'], q | {'messages': list(reversed(q['messages']))}, prior))

    def test_blind_comparison_treats_reuse_as_identity_and_failures_as_unjudgeable(self):
        item = {'state': 'completed', 'body': 'TEXT-ID', 'request_hash': 'WIRE-C'}
        case = {'bodies': {'C': item, 'B': item | {'body': 'BASE-ID', 'request_hash': 'WIRE-B'}, 'S': item}}
        self.assertTrue(pair(case, 'C', 'left')['same_request'])
        self.assertTrue(pair(case, 'C', 'right')['same'])
        self.assertEqual(pair(case, 'B', 'right')['right'], 'TEXT-ID')
        case['bodies']['S'] = item | {'body': None, 'state': 'unknown'}
        self.assertFalse(pair(case, 'C', 'left')['ready'])
        self.assertFalse(pair(case, 'C', 'left')['same'])


class Sources:
    def __init__(self, output, decision_path):
        self.output, self.decision_path = output, decision_path
        self.decision_hash = file_hash(decision_path)
        self.model = public_model('deepseek-v4-pro', 1.)
        self.revoked = False

    def position_packet(self, n):
        if self.revoked:
            raise CorpusError('TEST-AUTHORITY-REVOKED')
        req = build_request(self.model, [{'role': 'user', 'content': f'MARKER-{n}'}])[0]
        return {'position': n, 'reuse': None if n == 2 else 'C',
                'references': {'U': {'raw_hash': 'HASH-U'}}, 'selected_ids': ['ID-A'] if n == 2 else [],
                'request_hash': fingerprint(req), 'model': self.model, 'request': req}


class BodyTrialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.getenv('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        decision = base / 'decision.json'
        save_once(decision, {'review': 'MARKER'})
        self.src = Sources(base / 'body-comparison-v1', decision)
        self.root = self.src.output
        cases = []
        for n in sorted(POSITIONS):
            p = self.src.position_packet(n)
            path = f'requests/{n:02}.private.json'
            cases.append({k: p[k] for k in ('position', 'reuse', 'selected_ids', 'request_hash', 'references')} |
                         {'file': path, 'sha256': save_once(self.root / path, p)})
        self.plan = {'version': VERSION, 'positions': cases, 'max_new_pro_requests': 1,
                     'decision_path': str(decision), 'selector_approval_sha256': self.src.decision_hash,
                     'model': self.src.model, 'blind_key_sha256': save_once(self.root / 'blind-key.private.json', {'key': 'ID'})}
        self.manifest_hash = save_once(self.root / 'manifest.private.json', self.plan)
        self.approval_path = self.root / 'approval.json'
        self.approval = {'version': 'semantic-body-use-v1', 'scope': 'changed_pro_bodies_only',
                         'model': 'deepseek-v4-pro', 'manifest_sha256': self.manifest_hash,
                         'approved_positions': [2], 'max_dispatches': 1, 'retries': 0,
                         'body_generation_approved': True, 'author_message': 'AUTH-MARKER'}
        save_once(self.approval_path, self.approval)
        patcher = patch('experiments.semantic_body_run.ReviewedSources', return_value=self.src)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0
        self.credentials_read = 0

    def preflight(self, *_):
        pass

    async def complete(self, model, request, *, save_raw):
        self.calls += 1
        raw = json.dumps({'choices': [{'message': {'content': 'BODY-ID'}, 'finish_reason': 'stop'}],
                          'model': model['name']}).encode()
        save_raw(raw, http_status=200, diagnostics={})
        return {}

    def run_trial(self):
        def credentials():
            self.credentials_read += 1
            return {'base_url': self.src.model['base_url'], 'encrypted_key': 'LOCAL-ID'}
        with redirect_stdout(StringIO()):
            return asyncio.run(execute(self.src.decision_path, self.approval_path,
                                       client=self, load_credentials=credentials))

    def test_new_budget_binds_changed_positions_and_wire_manifest(self):
        for change in ({'max_dispatches': 2}, {'approved_positions': [1]},
                       {'manifest_sha256': 'OTHER'}, {'body_generation_approved': False}):
            with self.assertRaises(CorpusError):
                funded_positions(self.plan, self.approval | change, self.manifest_hash)
        with self.assertRaises(CorpusError):
            funded_positions(self.plan, {'version': 'semantic-selector-author-use-v1'}, self.manifest_hash)

    def test_reused_positions_have_no_spendable_slot_and_restart_uses_no_key(self):
        first = self.run_trial()
        self.assertEqual(first['attempts'], 1)
        self.assertEqual([r['slot'] for r in first['calls']], ['02.semantic'])
        self.assertEqual(first, self.run_trial())
        self.assertEqual((self.calls, self.credentials_read), (1, 1))
        ledger = TrialLedger(self.root / 'receipts')
        with self.assertRaises(sqlite3.IntegrityError), ledger.connection(write=True) as db:
            db.execute("""INSERT INTO trial_calls(slot,position,kind,state,model_json)
                          VALUES('01.semantic',1,'D','prepared','{}')""")

    def test_changed_inputs_and_withdrawn_sources_stop_before_credentials(self):
        self.src.revoked = True
        with self.assertRaises(CorpusError):
            self.run_trial()
        self.src.revoked = False
        (self.root / self.plan['positions'][1]['file']).write_bytes(b'{}')
        with self.assertRaises(CorpusError):
            self.run_trial()
        self.assertEqual(self.credentials_read, 0)

    def test_lost_ledger_does_not_reset_approval_or_replay(self):
        self.run_trial()
        (self.root / 'receipts/trial.sqlite3').unlink()
        with self.assertRaisesRegex(CorpusError, 'original_body_ledger_required'):
            self.run_trial()
        self.assertEqual(self.calls, 1)

    def test_uncertain_transport_stops_and_stays_unknown_on_restart(self):
        async def interrupted(*args, **kwargs):
            self.calls += 1
            raise TimeoutError()
        self.complete = interrupted
        first = self.run_trial()
        self.assertEqual(first['calls'][0]['state'], 'unknown')
        self.assertEqual(self.run_trial(), first)
        self.assertEqual(self.calls, 1)


if __name__ == '__main__':
    unittest.main()
