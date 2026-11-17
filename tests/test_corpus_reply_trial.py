"""Engineering markers and provider stubs only; no newly authored LLM prose."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from experiments.corpus_reply_trial import (KINDS, TrialLedger, assemble_new, bind_canonical_ledger,
    parse_receipt, public_model, substitute)
from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_learning_transport import CorpusCallFailure
from persona_runtime.corpus_store import fingerprint
from persona_runtime.provider_compat import build_request


CREDENTIALS = {'base_url': 'https://api.deepseek.com', 'encrypted_key': 'KEY-ID-001'}


def response(stream=False, content='BODY-ID-001', finish='stop', done=True):
    usage = {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}
    if not stream:
        return json.dumps({'model': 'MODEL-ID-001', 'usage': usage, 'choices': [
            {'message': {'content': content}, 'finish_reason': finish}]}).encode()
    chunks = [
        {'model': 'MODEL-ID-001', 'choices': [{'delta': {'content': content[:3]}, 'finish_reason': None}]},
        {'model': 'MODEL-ID-001', 'usage': usage, 'choices': [{'delta': {'content': content[3:]}, 'finish_reason': finish}]}]
    return ('\n\n'.join('data: ' + json.dumps(c) for c in chunks) + ('\n\ndata: [DONE]\n\n' if done else '\n\n')).encode()


def plan():
    calls = []
    for p in range(1, 17):
        for kind in KINDS:
            model = public_model('deepseek-flash' if kind in ('A', 'U') else 'deepseek-v4-pro', .3 if kind == 'U' else 1.0)
            if kind in ('A', 'B'):
                model['_generation_profile'] = {'version': 'chat-stream-unlimited-v1'}
            request = None if kind in ('C', 'D') else build_request(model, [{'role': 'user', 'content': 'INPUT-ID-' + str(p)}])[0]
            calls.append({'slot': f'{p:02}.{kind}', 'position': p, 'kind': kind, 'model': model, 'request': request})
    return {'version': 'TEST-ID-001', 'calls': calls}


class Client:
    def __init__(self):
        self.calls = []
        self.mode = 'success'
        self.after_save = None

    def preflight(self, model, request):
        if build_request(model, request['messages'])[0] != request:
            raise ValueError('WIRE-ID-001')

    async def complete(self, model, request, *, save_raw):
        self.calls.append(fingerprint(request))
        if self.mode in ('unknown', 'unsent'):
            raise CorpusCallFailure('NETWORK-ID-001', {'request_headers_sent': self.mode != 'unsent', 'response_headers_received': False})
        status = 503 if self.mode == 'http' else 200
        raw = b'{"error":{"code":"HTTP-ID-001"}}' if status != 200 else response(request.get('stream', False))
        diagnostics = {'request_headers_sent': True, 'response_headers_received': True, 'http_status': status, 'elapsed_ms': 1}
        save_raw(raw, http_status=status, diagnostics=diagnostics)
        if self.after_save:
            self.after_save()
        return diagnostics


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pcar-reply-trial-', dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.client = Client()
        self.plan = plan()
        self.ledger = TrialLedger(self.root / 'ledger', self.client)
        self.ledger.initialize(self.plan)

    def run_slot(self, slot):
        return asyncio.run(self.ledger.perform(slot, CREDENTIALS))

    def test_completed_sse_request_is_not_replayed(self):
        row = self.run_slot('01.A')
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(json.loads(row['result_json'])['body'], 'BODY-ID-001')
        self.run_slot('01.A')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.ledger.status()['attempts'], 1)

    def test_unknown_and_explicit_unsent_attempts_are_not_replayed(self):
        self.client.mode = 'unknown'
        self.assertEqual(self.run_slot('01.A')['state'], 'unknown')
        self.client.mode = 'success'
        self.run_slot('01.A')
        self.client.mode = 'unsent'
        self.assertEqual(self.run_slot('01.B')['state'], 'failed')
        self.run_slot('01.B')
        self.assertEqual(len(self.client.calls), 2)

    def test_complete_http_error_receipt_is_preserved(self):
        self.client.mode = 'http'
        row = self.run_slot('01.A')
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['http_status'], 503)
        self.assertIsNotNone(row['raw_response'])

    def bind(self, slot, content='INPUT-ID-CD'):
        dependency = self.ledger.row('01.U')
        model = json.loads(self.ledger.row(slot)['model_json'])
        self.ledger.bind(slot, model, build_request(model, [{'role': 'user', 'content': content}])[0], dependency['raw_hash'])

    def test_c_and_d_share_one_completed_understanding_receipt(self):
        self.run_slot('01.U')
        self.bind('01.C')
        self.bind('01.D')
        self.assertEqual(self.ledger.row('01.C')['dependency_hash'], self.ledger.row('01.D')['dependency_hash'])
        self.run_slot('01.C')
        self.run_slot('01.D')
        self.assertEqual(len(self.client.calls), 3)

    def test_dependency_cannot_be_rebound_or_taken_from_other_position(self):
        with self.assertRaises(CorpusError):
            self.bind('01.C')
        self.run_slot('01.U')
        self.bind('01.C')
        with self.assertRaisesRegex(CorpusError, 'rebind'):
            self.bind('01.C', 'INPUT-ID-CHANGED')
        model = json.loads(self.ledger.row('02.C')['model_json'])
        with self.assertRaises(CorpusError):
            self.ledger.bind('02.C', model, build_request(model, [{'role': 'user', 'content': 'ID-002'}])[0], self.ledger.row('01.U')['raw_hash'])

    def test_first_request_and_raw_response_and_result_cannot_be_overwritten(self):
        self.run_slot('01.A')
        for field, value in [('request_json', '{}'), ('raw_response', b'REPLACED'), ('result_json', '{}')]:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.ledger.connection(write=True) as db:
                    db.execute('UPDATE trial_calls SET ' + field + '=? WHERE slot=?', (value, '01.A'))

    def test_recover_after_complete_receipt_does_not_call_transport(self):
        with self.ledger.connection(write=True) as db:
            db.execute("UPDATE trial_calls SET state='dispatched',attempt_id='ATTEMPT-ID-001',started_epoch=1,raw_response=?,raw_hash=?,http_status=200 WHERE slot='01.A'",
                       (response(True), __import__('hashlib').sha256(response(True)).hexdigest()))
        self.ledger.recover()
        self.assertEqual(self.ledger.row('01.A')['state'], 'completed')
        self.assertEqual(self.client.calls, [])

    def test_interrupted_dispatch_without_response_becomes_unknown(self):
        with self.ledger.connection(write=True) as db:
            db.execute("UPDATE trial_calls SET state='dispatched',attempt_id='ATTEMPT-ID-002',started_epoch=1 WHERE slot='01.A'")
        self.ledger.recover()
        self.assertEqual(self.ledger.row('01.A')['state'], 'unknown')
        self.run_slot('01.A')
        self.assertEqual(self.client.calls, [])

    def test_cancel_during_receipt_preserves_bytes_and_blocks_late_completion(self):
        self.client.after_save = self.ledger.cancel
        row = self.run_slot('01.A')
        self.assertEqual(row['state'], 'cancelled')
        self.assertIsNotNone(row['raw_response'])
        self.assertEqual(self.ledger.row('01.B')['state'], 'cancelled')
        self.run_slot('01.B')
        self.assertEqual(len(self.client.calls), 1)

    def test_source_guard_failure_prevents_any_transport(self):
        def denied(_):
            raise CorpusError('AUTH-ID-001')
        self.ledger.guard = denied
        with self.assertRaises(CorpusError):
            self.run_slot('01.A')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.ledger.status()['attempts'], 0)

    def test_fixed_eighty_slots_cannot_be_extended_or_renamed(self):
        bad = deepcopy(self.plan)
        bad['calls'].append(bad['calls'][0] | {'slot': '17.A', 'position': 17})
        with self.assertRaisesRegex(CorpusError, 'eighty'):
            TrialLedger(self.root / 'other').initialize(bad)
        with self.assertRaises(CorpusError):
            self.run_slot('17.A')

    def test_canonical_binding_refuses_alternate_root_or_deleted_ledger(self):
        approval = self.root / 'approval.json'
        approval.write_text('{}', encoding='utf-8')
        first = self.root / 'first'
        bind_canonical_ledger(first, approval, self.plan)
        with self.assertRaisesRegex(CorpusError, 'original_ledger'):
            bind_canonical_ledger(self.root / 'second', approval, self.plan)
        (first / 'ledger/trial.sqlite3').unlink()
        with self.assertRaisesRegex(CorpusError, 'original_ledger'):
            bind_canonical_ledger(first, approval, self.plan)


class AssemblyAndReceiptTests(unittest.TestCase):
    def test_inserted_literal_placeholder_is_not_expanded_again(self):
        self.assertEqual(substitute('{one}|{two}', {'one': '{two}', 'two': 'VALUE-ID'}), '{two}|VALUE-ID')

    def test_unknown_or_missing_template_material_is_rejected(self):
        with self.assertRaises(CorpusError):
            substitute('{one}', {'two': 'ID'})

    def test_sse_joins_exact_content_and_last_choice_usage(self):
        result = parse_receipt(response(True, '  BODY-ID\r\n'), True)
        self.assertTrue(result['valid'])
        self.assertEqual(result['body'], '  BODY-ID\r\n')
        self.assertEqual(result['usage']['total_tokens'], 20)

    def test_incomplete_stream_and_empty_or_limited_outputs_are_retained_as_invalid(self):
        result = parse_receipt(response(True, done=False), True)
        self.assertTrue(result['incomplete_stream'])
        self.assertFalse(result['valid'])
        self.assertFalse(parse_receipt(response(content=' '))['valid'])
        limited = parse_receipt(response(content='BODY-ID', finish='length'))
        self.assertFalse(limited['valid'])
        self.assertEqual(limited['body'], 'BODY-ID')

    def test_shared_understanding_and_only_asset_variable_changes_c_and_d(self):
        prompts = {'Flash-system.txt': 'SYS-U-ID', 'Pro-system.txt': 'SYS-CD-ID',
                   'Flash-user.template.txt': '{time_block}|{dialogue_prompt}|{extra_info_block}',
                   'Pro-user.template.txt': '{extra_info_block}|{expression_habits_block}|{time_block}|{dialogue_prompt}|{reply_target_block}|{planner_reasoning}'}
        binding = {'variables': {'time_block': {'prefix': 'TIME-ID:'},
            'dialogue_prompt': {'line_format': '{time}/{speaker}/{text}'},
            'expression_habits_block': {'prefix': 'ASSETS-ID:', 'item_format': '{situation}/{style}', 'suffix_if_present': '\n'},
            'reply_target_block': {'format': 'TARGET-ID:{current_user_text}'}, 'planner_reasoning': {'format': 'THOUGHT-ID:{thought}'}}}
        position = {'input_text': 'INPUT-ID-{planner_reasoning}', 'selection': {'selected_ids': ['ASSET-ID-001']}}
        assets = {'assets': [{'id': 'ASSET-ID-001', 'situation': 'SITUATION-ID', 'style': 'STYLE-ID', 'source_quote': 'QUOTE-NOT-ALLOWED'}]}
        with patch('experiments.corpus_reply_trial.reviewed_sender', return_value='SENDER-ID'):
            u = assemble_new(prompts, binding, position, '2026-01-01T00:00:00+00:00')
            c = assemble_new(prompts, binding, position, '2026-01-01T00:00:00+00:00', understanding='ACTUAL-U-ID')
            d = assemble_new(prompts, binding, position, '2026-01-01T00:00:00+00:00', understanding='ACTUAL-U-ID', assets=assets)
        self.assertEqual(c[0], d[0])
        self.assertIn('ACTUAL-U-ID', c[1]['content'])
        self.assertEqual(d[1]['content'].replace('ASSETS-ID:SITUATION-ID/STYLE-ID\n', ''), c[1]['content'])
        self.assertNotIn('QUOTE-NOT-ALLOWED', d[1]['content'])
        self.assertIn('INPUT-ID-{planner_reasoning}', d[1]['content'])
        self.assertEqual(len(u), 2)


class ReviewAccountingTests(unittest.TestCase):
    def test_html_and_plain_text_are_not_json_quoted(self):
        from experiments.corpus_reply_trial_report import save_text_once
        with tempfile.TemporaryDirectory(prefix='pcar-review-text-', dir=os.environ.get('PCAR_CORPUS_TEST_ROOT')) as temp:
            p = Path(temp).resolve() / 'index.html'
            value = '<!doctype html>\n<html><body>MARKER-ID-001</body></html>'
            save_text_once(p, value)
            self.assertEqual(p.read_bytes(), value.encode('utf-8'))
            save_text_once(p, value)
            with self.assertRaises(CorpusError):
                save_text_once(p, 'CHANGED-ID')

    def test_unknown_usage_estimate_stays_unknown(self):
        from experiments.corpus_reply_trial_report import estimated_peak
        self.assertIsNone(estimated_peak('B', None))
        self.assertIsNone(estimated_peak('D', {'total_tokens': 20}))

    def test_pro_peak_estimate_uses_reported_cache_and_output_counts(self):
        from decimal import Decimal
        from experiments.corpus_reply_trial_report import estimated_peak
        value = estimated_peak('D', {'prompt_tokens': 10, 'completion_tokens': 20,
                                     'prompt_cache_hit_tokens': 2, 'prompt_cache_miss_tokens': 8})
        self.assertEqual(Decimal(value), Decimal('0.0006126'))

    def test_review_key_is_frozen_before_results_and_reused(self):
        from experiments.corpus_reply_trial_report import freeze_review_key
        with tempfile.TemporaryDirectory(prefix='pcar-review-key-', dir=os.environ.get('PCAR_CORPUS_TEST_ROOT')) as temp:
            root = Path(temp).resolve()
            ledger = TrialLedger(root / 'ledger')
            ledger.initialize(plan())
            first = freeze_review_key(root)
            self.assertEqual(first, freeze_review_key(root))
            self.assertTrue(first['assignment_before_model_results'])
            self.assertTrue(all(set(v.values()) == set('ABCD') for v in first['positions'].values()))


if __name__ == '__main__':
    unittest.main()
