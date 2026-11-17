"""Selector contracts and single-attempt protection; protocol markers only."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.corpus_semantic_selector import (VERSION, approved_manifest, finalize_selection,
    parse_selected_ids, run_approved)
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_learning_transport import CorpusCallFailure
from persona_runtime.provider_compat import build_request
from experiments.corpus_reply_trial import public_model
from experiments.corpus_reply_study import save_once


class SelectionParsing(unittest.TestCase):
    def test_empty_is_valid_and_unknown_duplicate_or_noninteger_ids_are_not(self):
        self.assertEqual(parse_selected_ids('{"selected_ids":[]}', {1, 2}), [])
        self.assertEqual(parse_selected_ids('{"selected_ids":[2,1]}', {1, 2}), [2, 1])
        for body in ['{"selected_ids":[7]}', '{"selected_ids":[true]}', '{"selected_ids":[1,1]}',
                     '{"selected_ids":["1"]}', '{"selected_ids":[1,2,3,4,5,6]}', '[]', 'BODY-ID']:
            with self.assertRaises(CorpusError):parse_selected_ids(body, set(range(1, 7)))

    def test_complete_http_receipt_and_actual_usage_are_parsed_before_ids(self):
        raw = json.dumps({'model': 'MODEL-ID', 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                          'choices': [{'finish_reason': 'stop', 'message': {'content': '{"selected_ids":[1]}'}}]}).encode()
        result = finalize_selection({'position': 1, 'candidate_numbers': {'1': 'ASSET-ID'}}, raw,
                                    {'raw_sha256': digest(raw), 'http_status': 200})
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['selected_proposal_ids'], ['ASSET-ID'])
        self.assertEqual(result['usage']['total_tokens'], 15)


class BoundedSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pcar-selector-', dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        plan = {'corpus_root': str(self.root / 'corpus'), 'learning_root': str(self.root / 'learning')}
        save_once(self.root / 'plan.private.json', plan)
        packets = []
        for n in range(1, 17):
            model = public_model('deepseek-flash', .2)
            model.update(max_tokens=512, _json_output=True)
            request = build_request(model, [{'role': 'system', 'content': 'JSON-ID-' + str(n)}])[0]
            packet = {'position': n, 'candidate_numbers': {'1': 'ASSET-ID'}, 'model': model,
                      'request': request, 'request_hash': digest(str(n).encode())}
            file = self.root / f'{n:02}.json'
            save_once(file, packet)
            packets.append({'position': n, 'path': file.name, 'sha256': digest(file.read_bytes())})
        manifest = {'version': VERSION, 'study_root': str(self.root), 'packets': packets}
        save_once(self.root / 'selector-manifest.private.json', manifest)
        self.approval = self.root / 'author-use.json'
        save_once(self.approval, {'version': 'semantic-selector-author-use-v1', 'manifest_sha256': digest((self.root / 'selector-manifest.private.json').read_bytes()),
                                 'max_dispatches': 16, 'allow_extra_selection_model': True,
                                 'model_use_approved': True, 'reply_regeneration_authorized': False})
        self.credentials = {'base_url': 'https://api.deepseek.com', 'encrypted_key': 'KEY-REFERENCE-ID'}

    def run_study(self, client):
        with patch('experiments.corpus_semantic_selector.authority_guard', return_value=lambda *_: None):
            return asyncio.run(run_approved(self.root, self.approval, self.credentials, client))

    def client(self, unknown=False):
        class Client:
            def __init__(self):self.calls = 0
            def preflight(self, *_):pass
            async def complete(self, model, request, *, save_raw):
                self.calls += 1
                if unknown:raise CorpusCallFailure('UNKNOWN-ID', {'request_headers_sent': True})
                raw = json.dumps({'model': model['name'], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{"selected_ids":[1]}'}}]}).encode()
                save_raw(raw, http_status=200, diagnostics={'request_headers_sent': True})
                return {}
        return Client()

    def test_all_sixteen_positions_complete_once_and_are_never_replayed(self):
        client = self.client()
        result = self.run_study(client)
        self.assertEqual(result['selector_calls'], 16)
        self.assertEqual(client.calls, 16)
        self.run_study(client)
        self.assertEqual(client.calls, 16)

    def test_unknown_stops_later_calls_and_is_not_replayed(self):
        client = self.client(True)
        self.assertEqual(self.run_study(client)['results'][0]['state'], 'unknown')
        self.run_study(client)
        self.assertEqual(client.calls, 1)

    def test_missing_new_approval_does_not_inherit_old_prompt_permissions(self):
        value = json.loads(self.approval.read_text(encoding='utf-8'))
        value['allow_extra_selection_model'] = False
        self.approval.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'new_author_approval_required'):
            approved_manifest(self.root, self.approval)

    def test_packet_change_is_rejected_before_transport(self):
        (self.root / '01.json').write_bytes(b'{}')
        client = self.client()
        with self.assertRaisesRegex(CorpusError, 'packet_changed'):self.run_study(client)
        self.assertEqual(client.calls, 0)

    def test_deleted_call_directory_cannot_reset_successful_attempt(self):
        client = self.client()
        self.run_study(client)
        (self.root / 'selector-calls/01/attempt.private.json').unlink()
        with self.assertRaisesRegex(CorpusError, 'original_attempt_required'):self.run_study(client)
        self.assertEqual(client.calls, 16)

    def test_approval_cannot_extend_sixteen_position_budget(self):
        value = json.loads(self.approval.read_text(encoding='utf-8'))
        value['max_dispatches'] = 17
        self.approval.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(CorpusError):approved_manifest(self.root, self.approval)


if __name__ == '__main__':unittest.main()
