"""Moect format and storage tests use marker values, never synthetic chat prose."""
from copy import deepcopy
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import CorpusStore
from persona_runtime.corpus_moect import parse_private_export, render_markdown, speaker_token
from persona_runtime.corpus_ingest import MoectCorpusStore
from persona_runtime.corpus_review import export_review
from scripts.pcar_corpus_ingest import main as ingest_main


def message(number, *, kind='text', own=False, body=None):
    epoch = 1773000000 + number
    types = {'text': 1, 'quote': 49, 'system': 10000, 'image': 3, 'chatrecord': 49}
    return {'local_id': number, 'server_id': str(9223372036854775800 + number), 'shard': 1,
            'type': types[kind], 'raw_type': types[kind],
            'create_time': epoch,
            'time_str': datetime.fromtimestamp(epoch, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
            'sender_wxid': '' if kind == 'system' else ('SOURCE-A' if own else 'SOURCE-B'),
            'sender': 'A' if own else 'B', 'is_sender': own, 'session': 'SOURCE-B',
            'text': body if body is not None else 'ID-' + str(number), 'kind': kind,
            'extra': {}, 'resources': []}


def document():
    first = message(1, own=True, body='ID-001\r\nID-002\u3002<script>MARK-001</script>')
    reply = message(2, kind='quote', body='ID-003')
    reply['raw_type'] = (57 << 32) | 49
    reply['extra'] = {'appmsg_type': 57, 'title': reply['text'],
                      'reply': {'server_id': first['server_id']}}
    reply['quote'] = {'server_id': first['server_id'], 'sender': first['sender'],
                      'text': first['text'], 'kind': 'text', 'resources': []}
    forwarded = message(5, kind='chatrecord')
    forwarded['forward'] = {'title': 'META-001', 'messages': [
        {'sender': 'SOURCE-C', 'time_str': 'TIME-C', 'text': 'ID-FOREIGN', 'resources': []}],
        'truncated': False}
    records = [first, reply, message(3, kind='system'), message(4, kind='image'), forwarded]
    return {'metadata': {'application': 'Moect', 'version': '1.3.0', 'session': 'SOURCE-B',
                         'name': 'DATASET-001', 'time_zone': 'Asia/Shanghai', 'demo': False,
                         'export_options': {'quick_export': True}},
            'messages': records, 'count': len(records)}


def pair(data=None):
    data = data or document()
    return json.dumps(data, ensure_ascii=False).encode('utf-8'), render_markdown(data).encode('utf-8')


class MoectFormat(unittest.TestCase):
    def parse(self, data=None, md=None):
        raw, expected = pair(data)
        return parse_private_export(raw, expected if md is None else md,
                                    identity_key=b'K' * 32, dataset_id='DATASET-001')

    def test_pair_preserves_text_spans_and_large_message_ids(self):
        result = self.parse()
        original = document()
        markdown = render_markdown(original)
        self.assertEqual(result['summary']['records'], 5)
        self.assertEqual(result['summary']['text_candidates'], 2)
        self.assertEqual(result['summary']['candidate_speakers'], {'self': 1, 'peer': 1})
        for row, source in zip(result['rows'], original['messages']):
            self.assertEqual(row['text'], source['text'])
            self.assertEqual(row['platform_message_id'], source['server_id'])
            self.assertEqual(markdown[row['md_text_start']:row['md_text_end']], row['text'])
        self.assertFalse(result['summary']['learning_eligible'])

    def test_quotes_and_forwarded_messages_are_not_duplicate_targets(self):
        result = self.parse()
        self.assertEqual(result['relations'][0]['target_id'], result['rows'][0]['id'])
        self.assertEqual(result['relations'][0]['state'], 'resolved')
        self.assertEqual(result['summary']['forward_containers'], 1)
        self.assertFalse(any(r['text'] == 'ID-FOREIGN' for r in result['rows']))
        self.assertEqual(result['summary']['excluded_counts'], {'system_message': 1, 'non_original_text': 2})

    def test_missing_reply_target_is_preserved_as_unresolved(self):
        data = document()
        data['messages'][1]['extra']['reply']['server_id'] = 'ABSENT-ID'
        data['messages'][1]['quote']['server_id'] = 'ABSENT-ID'
        result = self.parse(data)
        self.assertEqual(result['summary']['unresolved_replies'], 1)
        self.assertIsNone(result['relations'][0]['target_id'])

    def test_unknown_application_text_and_empty_quote_title_are_not_targets(self):
        data = document()
        data['messages'][0]['type'] = 49
        data['messages'][0]['raw_type'] = (1 << 32) | 49
        data['messages'][1]['extra']['title'] = ''
        result = self.parse(data)
        self.assertEqual(result['summary']['text_candidates'], 0)
        self.assertEqual(result['summary']['excluded_counts']['application_text_unconfirmed'], 1)

    def test_pair_mismatch_does_not_accept_missing_or_rewritten_lines(self):
        _, md = pair()
        with self.assertRaisesRegex(CorpusError, 'pair_mismatch'):
            self.parse(md=md.replace(b'ID-003', b'ID-004'))

    def test_demo_group_wrong_version_and_nonquick_export_are_rejected(self):
        for changes, error in [({'demo': True}, 'contract_unsupported'),
                               ({'session': 'ID@chatroom'}, 'private_session_required'),
                               ({'version': '1.3.1'}, 'contract_unsupported'),
                               ({'export_options': {'quick_export': False}}, 'contract_unsupported')]:
            data = document()
            data['metadata'].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(CorpusError, error):
                self.parse(data)

    def test_duplicate_json_keys_and_count_mismatch_are_rejected(self):
        raw, md = pair()
        duplicate = raw.replace(b'"count": 5', b'"count": 5, "count": 5')
        with self.assertRaisesRegex(CorpusError, 'duplicate_json_key'):
            parse_private_export(duplicate, md, identity_key=b'K'*32, dataset_id='ID')
        data = document()
        data['count'] += 1
        with self.assertRaisesRegex(CorpusError, 'record_count_invalid'):
            self.parse(data)

    def test_duplicate_platform_ids_mixed_sessions_and_wrong_time_are_rejected(self):
        for change, error in [
            (lambda d: d['messages'][1].update(server_id=d['messages'][0]['server_id']), 'identity_conflict'),
            (lambda d: d['messages'][1].update(session='SOURCE-C'), 'identity_conflict'),
            (lambda d: d['messages'][1].update(time_str='TIME-INVALID'), 'timezone_mismatch'),
            (lambda d: d['messages'][1].update(create_time=1), 'record_order_changed'),
            (lambda d: d['messages'][1].update(server_id=9007199254740993), 'text_field_invalid'),
        ]:
            data = document()
            change(data)
            with self.subTest(error=error), self.assertRaisesRegex(CorpusError, error):
                self.parse(data)

    def test_unconfirmed_third_speaker_and_conflicting_reply_are_rejected(self):
        data = document()
        data['messages'][1]['sender_wxid'] = 'SOURCE-C'
        with self.assertRaisesRegex(CorpusError, 'speakers_ambiguous'):
            self.parse(data)
        data = document()
        data['messages'][1]['quote']['server_id'] = 'OTHER-ID'
        with self.assertRaisesRegex(CorpusError, 'reply_evidence_conflict'):
            self.parse(data)

    def test_hmac_identity_depends_on_dataset_and_local_secret(self):
        one = speaker_token(b'A' * 32, 'DATASET-001', 'SOURCE-A')
        self.assertEqual(one, speaker_token(b'A' * 32, 'DATASET-001', 'SOURCE-A'))
        self.assertNotEqual(one, speaker_token(b'B' * 32, 'DATASET-001', 'SOURCE-A'))
        self.assertNotEqual(one, speaker_token(b'A' * 32, 'DATASET-002', 'SOURCE-A'))


class MoectStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pcar-moect-', dir=os.environ.get('PCAR_CORPUS_TEST_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / 'corpus'
        self.store = MoectCorpusStore(self.root)
        self.store.initialize()
        self.dataset = self.store.create_dataset('DATASET-001', 'CREATE-001')['id']
        raw, md = pair()
        self.imports = []
        sources = []
        for kind, content in [('json', raw), ('md', md)]:
            path = self.directory / ('source.' + kind)
            path.write_bytes(content)
            preview = self.store.preview(self.dataset, path, 'PREVIEW-' + kind)
            saved = self.store.retain_original(preview['id'], preview['manifest_hash'], 'COMMIT-' + kind)
            self.imports.append(saved['id'])
            sources.append({'kind': kind, 'sha256': digest(content)})
        self.grant = self.directory / 'grant.json'
        self.grant.write_text(json.dumps({'version': 'author-wechat-material-grant-v1',
            'conversation_kind': 'private', 'training_input_authorized': True, 'speaker_scope': 'both',
            'speaker_confirmation': 'author_confirmed_both_as_human_expression_sources',
            'model_request_version_approved': False, 'sources': sources}), encoding='utf-8')

    def parse(self, operation='PARSE-001'):
        return self.store.parse_pair(self.dataset, *self.imports, self.grant, operation)

    def test_upgrade_preserves_existing_foundation_files_and_operations(self):
        root = self.directory / 'old-corpus'
        old = CorpusStore(root)
        first = old.initialize()['corpus_id']
        dataset = old.create_dataset('DATASET-OLD', 'CREATE-OLD')['id']
        upgraded = MoectCorpusStore(root)
        state = upgraded.initialize()
        self.assertEqual(state['schema'], 2)
        self.assertEqual(state['corpus_id'], first)
        self.assertEqual(upgraded.inspect(dataset)['datasets'][0]['label'], 'DATASET-OLD')
        with self.assertRaisesRegex(CorpusError, 'database_incompatible'):
            old.inspect()

    def test_foreign_version_one_database_is_not_modified_or_given_an_identity_key(self):
        root = self.directory/'foreign'
        root.mkdir()
        path = root/'corpus.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE events(id TEXT)')
            db.execute('PRAGMA user_version=1')
        before = digest(path.read_bytes())
        with self.assertRaisesRegex(CorpusError, 'database_incompatible'):
            MoectCorpusStore(root).initialize()
        self.assertEqual(digest(path.read_bytes()), before)
        self.assertFalse((root/'identity.key').exists())

    def test_idempotent_parse_and_material_grant_never_enable_model_input(self):
        one = self.parse()
        self.assertEqual(self.parse(), one)
        self.assertEqual(self.parse('PARSE-002')['id'], one['id'])
        self.assertTrue(one['training_input_authorized'])
        self.assertFalse(one['model_input_approved'])
        self.assertTrue(self.store.verify_parse(one['id'])['passed'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_parses').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_messages').fetchone()[0], 5)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as db:
                db.execute('UPDATE corpus_material_grants SET model_input_approved=1')

    def test_cross_dataset_binding_wrong_file_hash_and_unconfirmed_scope_are_refused(self):
        other = self.store.create_dataset('DATASET-002', 'CREATE-002')['id']
        with self.assertRaisesRegex(CorpusError, 'import_scope_invalid'):
            self.store.parse_pair(other, *self.imports, self.grant, 'PARSE-OTHER')
        grant = json.loads(self.grant.read_text(encoding='utf-8'))
        original = deepcopy(grant)
        grant['sources'][0]['sha256'] = '0' * 64
        self.grant.write_text(json.dumps(grant), encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'author_files_unconfirmed'):
            self.parse()
        grant = original | {'speaker_scope': 'unknown'}
        self.grant.write_text(json.dumps(grant), encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'author_scope_unconfirmed'):
            self.parse()

    def test_normalized_text_relation_and_identity_tampering_are_detected(self):
        result = self.parse()
        with self.store.connection(write=True) as db:
            db.execute('UPDATE corpus_messages SET role=\'peer\' WHERE parse_id=? AND ordinal=0', (result['id'],))
        with self.assertRaisesRegex(CorpusError, 'message_evidence_changed'):
            self.store.verify_parse(result['id'])
        with self.store.connection(write=True) as db:
            db.execute('UPDATE corpus_messages SET role=\'self\' WHERE parse_id=? AND ordinal=0', (result['id'],))
            db.execute('UPDATE corpus_relations SET external_message_id=\'OTHER-ID\' WHERE kind=\'reply\'')
        with self.assertRaisesRegex(CorpusError, 'relation_evidence_changed'):
            self.store.verify_parse(result['id'])

    def test_missing_or_changed_identity_key_is_not_recreated(self):
        result = self.parse()
        (self.root / 'identity.key').write_bytes(b'X' * 32)
        with self.assertRaisesRegex(CorpusError, 'identity_key_changed'):
            self.store.initialize()
        with self.assertRaisesRegex(CorpusError, 'identity_key_changed'):
            self.store.verify_parse(result['id'])

    def test_revocation_changes_effective_authorization_and_blocks_new_parse(self):
        result = self.parse()
        self.store.revoke_dataset(self.dataset, 1, 'REVOKE-001')
        self.assertFalse(self.store.parse_status(result['id'])['training_input_authorized'])
        self.assertEqual(self.parse()['dataset_state'], 'revoked')
        with self.assertRaisesRegex(CorpusError, 'dataset_revoked'):
            self.parse('PARSE-NEW')

    def test_local_review_is_escaped_contains_no_remote_assets_and_preserves_md(self):
        result = self.parse()
        output = self.directory / 'review'
        exported = export_review(self.store, result['id'], output)
        self.assertEqual(exported['records'], 5)
        self.assertEqual((output/'source.md').read_bytes(), pair()[1])
        html = (output/'records-1.html').read_text(encoding='utf-8')
        self.assertNotIn('<script', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;MARK-001&lt;/script&gt;', html)
        self.assertIn('ID-001', html)
        with self.assertRaisesRegex(CorpusError, 'empty_target_required'):
            export_review(self.store, result['id'], output)

    def test_parse_and_review_do_not_use_network(self):
        with patch('socket.socket', side_effect=AssertionError('NETWORK-001')):
            result = self.parse()
            self.store.verify_parse(result['id'])
            export_review(self.store, result['id'], self.directory/'review')

    def test_failed_parse_transaction_can_resume_without_partial_rows_or_identity_map(self):
        original_save = self.store._save
        def fail(db, operation_id, action, request, result):
            if action == 'moect_parse':
                raise OSError('STATE-001')
            return original_save(db, operation_id, action, request, result)
        with patch.object(self.store, '_save', side_effect=fail):
            with self.assertRaises(OSError):
                self.parse()
        self.assertEqual(self.store.inspect()['parsed_batches'], 0)
        self.assertEqual(list(self.root.glob('*.identities.json')), [])
        result = self.parse()
        self.assertTrue(self.store.verify_parse(result['id'])['passed'])

    def test_identity_map_modification_is_detected(self):
        result = self.parse()
        (self.root/(result['id']+'.identities.json')).write_bytes(b'ID-CHANGED')
        with self.assertRaisesRegex(CorpusError, 'identity_map_changed'):
            self.store.verify_parse(result['id'])

    def test_cli_status_omits_message_body_and_has_no_model_approval(self):
        result = self.parse()
        output = io.StringIO()
        with redirect_stdout(output):
            code = ingest_main(['--corpus',str(self.root),'parse-status','--parse-id',result['id']])
        self.assertEqual(code, 0)
        status = json.loads(output.getvalue())
        self.assertTrue(status['training_input_authorized'])
        self.assertFalse(status['model_input_approved'])
        self.assertNotIn('MARK-001', output.getvalue())
        self.assertNotIn('ID-FOREIGN', output.getvalue())


if __name__ == '__main__':
    unittest.main()
