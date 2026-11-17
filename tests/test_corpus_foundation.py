"""Local storage boundary tests; marker bytes only, never model material."""
import codecs
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.corpus_files import CorpusError, digest, inspect_bytes
from persona_runtime.corpus_store import CorpusStore
from scripts.pcar_corpus import main


class FileEnvelope(unittest.TestCase):
    def test_transport_keeps_crlf_and_unicode_without_chat_interpretation(self):
        body = 'ID-001\r\nID-002\nID-003\r\U0001f539\u3002\uff1f\uff01'
        raw = codecs.BOM_UTF8 + body.encode('utf-8')
        manifest = inspect_bytes(raw, '.md')
        self.assertEqual(manifest['sha256'], digest(raw))
        self.assertEqual(manifest['text_sha256'], digest(body.encode('utf-8')))
        self.assertEqual((manifest['crlf'], manifest['lf'], manifest['cr']), (1, 1, 1))
        self.assertEqual(manifest['unparsed_character_span'], [0, len(body)])
        self.assertIsNone(manifest['parser'])
        self.assertIsNone(manifest['message_count'])
        self.assertFalse(manifest['learning_eligible'])

    def test_utf16_bom_is_decoded_without_line_conversion(self):
        for bom, codec in [(codecs.BOM_UTF16_LE, 'utf-16-le'), (codecs.BOM_UTF16_BE, 'utf-16-be')]:
            raw = bom + 'ID-002\r\n'.encode(codec)
            manifest = inspect_bytes(raw, '.txt')
            self.assertEqual(manifest['encoding'], codec)
            self.assertEqual(manifest['text_sha256'], digest(b'ID-002\r\n'))

    def test_legacy_encoding_requires_explicit_confirmation(self):
        raw = b'\x81\x40'
        with self.assertRaisesRegex(CorpusError, 'encoding_confirmation_required'):
            inspect_bytes(raw, '.txt')
        self.assertEqual(inspect_bytes(raw, '.txt', 'gb18030')['encoding'], 'gb18030')

    def test_bom_mismatch_binary_empty_unsupported_and_capacity_are_rejected(self):
        for raw, suffix, encoding, error in [
            (b'', '.txt', None, 'file_empty'),
            (b'ID-001\x00', '.txt', None, 'control_content'),
            (b'ID-001', '.html', None, 'file_type_unsupported'),
            (codecs.BOM_UTF32_LE + b'\x00' * 4, '.txt', None, 'encoding_unsupported'),
            (codecs.BOM_UTF8 + b'ID-001', '.txt', 'gb18030', 'bom_conflict'),
            (b'ID-001', '.txt', 'latin-1', 'encoding_unsupported'),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(CorpusError, error):
                inspect_bytes(raw, suffix, encoding)
        with patch('persona_runtime.corpus_files.MAX_FILE_BYTES', 2):
            with self.assertRaisesRegex(CorpusError, 'file_capacity'):
                inspect_bytes(b'ID-001', '.txt')


class CorpusStorage(unittest.TestCase):
    def setUp(self):
        selected = os.environ.get('PCAR_CORPUS_TEST_ROOT')
        self.temp = tempfile.TemporaryDirectory(prefix='pcar-corpus-', dir=selected)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / 'corpus'
        self.store = CorpusStore(self.root)
        self.store.initialize()
        self.dataset = self.store.create_dataset('DATASET-001', 'CREATE-001')['id']
        self.file = self.directory / 'source.txt'
        self.raw = b'ID-001\r\nID-002\nID-002\n'
        self.file.write_bytes(self.raw)

    def preview(self, source=None, operation='PREVIEW-001', dataset_id=None):
        return self.store.preview(dataset_id or self.dataset, source or self.file, operation)

    def commit(self, preview=None, operation='COMMIT-001'):
        preview = preview or self.preview()
        return self.store.retain_original(preview['id'], preview['manifest_hash'], operation)

    def test_open_and_inspect_never_initialize_another_directory(self):
        path = self.directory / 'absent'
        other = CorpusStore(path)
        with self.assertRaisesRegex(CorpusError, 'not_initialized'):
            other.inspect()
        self.assertFalse(path.exists())
        one = self.store.inspect()
        self.assertEqual(self.store.initialize(), one)

    def test_source_repository_and_nonempty_targets_are_refused(self):
        with self.assertRaisesRegex(CorpusError, 'outside_source'):
            CorpusStore(Path(__file__).resolve().parents[1] / 'corpus-data')
        with self.assertRaisesRegex(CorpusError, 'absolute_path_required'):
            CorpusStore(Path('relative'))
        target = self.directory / 'foreign'
        target.mkdir()
        marker = target / 'runtime.sqlite3'
        marker.write_bytes(b'ID-001')
        with self.assertRaisesRegex(CorpusError, 'empty_target_required'):
            CorpusStore(target).initialize()
        self.assertEqual(marker.read_bytes(), b'ID-001')
        self.assertFalse((target / 'corpus.sqlite3').exists())

    def test_runtime_database_cannot_be_opened_as_corpus(self):
        target = self.directory / 'foreign'
        target.mkdir()
        database = target / 'corpus.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE events(id TEXT)')
            db.execute('PRAGMA user_version=27')
        before = digest(database.read_bytes())
        with self.assertRaisesRegex(CorpusError, 'database_incompatible'):
            CorpusStore(target).initialize()
        self.assertEqual(digest(database.read_bytes()), before)

    def test_schema_version_and_missing_tables_fail_closed(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(CorpusError, 'database_incompatible'):
            self.store.inspect()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('PRAGMA user_version=1')
            db.execute('DROP TABLE corpus_source_locations')
        with self.assertRaisesRegex(CorpusError, 'schema_incomplete'):
            self.store.inspect()

    def test_failed_new_initialization_closes_database_and_can_retry(self):
        target = self.directory / 'new-corpus'
        with patch('persona_runtime.corpus_store.SCHEMA', 'CREATE TABLE PARTIAL(id TEXT); INVALID;'):
            with self.assertRaises(sqlite3.Error):
                CorpusStore(target).initialize()
        self.assertFalse((target / 'corpus.sqlite3').exists())
        self.assertEqual(CorpusStore(target).initialize()['schema'], 1)

    def test_preview_retains_no_body_no_original_and_no_fake_message_count(self):
        value = self.preview()
        self.assertNotIn('ID-002', json.dumps(value))
        self.assertEqual(value['manifest']['sha256'], digest(self.raw))
        self.assertIsNone(value['manifest']['message_count'])
        self.assertEqual(self.store.inspect()['original_count'], 0)
        self.assertFalse((self.root / 'originals').exists())
        with self.store.connection() as db:
            self.assertNotIn('ID-002', db.execute('SELECT manifest_json FROM corpus_previews').fetchone()[0])

    def test_relative_missing_and_over_capacity_sources_make_no_preview(self):
        for source, error in [(Path('relative.txt'), 'absolute_path_required'),
                              (self.directory / 'missing.txt', 'source_unavailable')]:
            with self.assertRaisesRegex(CorpusError, error):
                self.preview(source)
        with patch('persona_runtime.corpus_files.MAX_FILE_BYTES', 2):
            with self.assertRaisesRegex(CorpusError, 'file_capacity'):
                self.preview()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_previews').fetchone()[0], 0)

    def test_dataset_and_preview_operations_are_idempotent_and_conflict_checked(self):
        self.assertEqual(self.store.create_dataset('DATASET-001', 'CREATE-001')['id'], self.dataset)
        with self.assertRaisesRegex(CorpusError, 'operation_conflict'):
            self.store.create_dataset('DATASET-002', 'CREATE-001')
        one = self.preview()
        self.assertEqual(self.preview(), one)
        with self.assertRaisesRegex(CorpusError, 'operation_conflict'):
            self.store.preview(self.dataset, self.file, 'PREVIEW-001', 'utf-8')
        with self.assertRaisesRegex(CorpusError, 'operation_conflict'):
            self.store.create_dataset('DATASET-001', 'PREVIEW-001')

    def test_original_bytes_are_preserved_and_reopen_has_no_duplicates(self):
        one = self.commit()
        stored = self.root / 'originals' / digest(self.raw)[:2] / (digest(self.raw) + '.bin')
        self.assertEqual(stored.read_bytes(), self.raw)
        self.assertEqual(self.file.read_bytes(), self.raw)
        other = CorpusStore(self.root)
        self.assertEqual(other.inspect()['original_count'], 1)
        self.assertEqual(other.verify_originals()['originals_checked'], 1)
        self.file.unlink()
        self.assertEqual(self.store.retain_original(self.preview()['id'], self.preview()['manifest_hash'], 'COMMIT-001'), one)

    def test_same_bytes_in_another_file_add_provenance_without_duplicate_original(self):
        one = self.commit()
        copy = self.directory / 'copy.md'
        copy.write_bytes(self.raw)
        second = self.preview(copy, 'PREVIEW-002')
        two = self.commit(second, 'COMMIT-002')
        self.assertEqual(one['id'], two['id'])
        self.assertTrue(two['duplicate_original'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_source_locations').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM corpus_imports').fetchone()[0], 1)

    def test_concurrent_retention_has_one_import_and_complete_original(self):
        first = self.preview()
        second = self.preview(operation='PREVIEW-002')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.commit, p, operation) for p, operation in
                       [(first, 'COMMIT-001'), (second, 'COMMIT-002')]]
            results = [f.result() for f in futures]
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(self.store.inspect()['original_count'], 1)
        self.assertTrue(self.store.verify_originals()['passed'])

    def test_other_datasets_do_not_share_binding_and_repeated_lines_remain(self):
        one = self.commit()
        other = self.store.create_dataset('DATASET-002', 'CREATE-002')['id']
        two = self.commit(self.preview(operation='PREVIEW-002', dataset_id=other), 'COMMIT-002')
        self.assertNotEqual(one['id'], two['id'])
        self.assertEqual(self.store.inspect()['original_count'], 1)
        self.assertEqual([d['original_count'] for d in self.store.inspect()['datasets']], [1, 1])
        self.assertEqual(self.store._blob(one['file_hash']).read_bytes().count(b'ID-002'), 2)

    def test_changed_source_or_wrong_preview_hash_cannot_be_committed(self):
        value = self.preview()
        with self.assertRaisesRegex(CorpusError, 'preview_changed'):
            self.store.retain_original(value['id'], '0' * 64, 'COMMIT-001')
        self.file.write_bytes(self.raw + b'ID-003')
        with self.assertRaisesRegex(CorpusError, 'source_changed'):
            self.commit(value)
        self.assertEqual(self.store.inspect()['original_count'], 0)

    def test_tampered_preview_is_rejected_even_with_original_hash(self):
        value = self.preview()
        with self.store.connection(write=True) as db:
            body = value['manifest'] | {'filename': 'changed.txt'}
            db.execute('UPDATE corpus_previews SET manifest_json=? WHERE id=?', (json.dumps(body), value['id']))
        with self.assertRaisesRegex(CorpusError, 'preview_changed'):
            self.commit(value)

    def test_corrupt_original_is_not_silently_replaced(self):
        result = self.commit()
        original = self.store._blob(result['file_hash'])
        original.write_bytes(b'ID-003')
        with self.assertRaisesRegex(CorpusError, 'original_changed'):
            self.store.verify_originals()
        with self.assertRaisesRegex(CorpusError, 'original_changed'):
            self.commit(self.preview(operation='PREVIEW-002'), 'COMMIT-002')
        self.assertEqual(original.read_bytes(), b'ID-003')

    def test_revocation_invalidates_prepared_import_without_claiming_erasure(self):
        value = self.preview()
        result = self.store.revoke_dataset(self.dataset, 1, 'REVOKE-001')
        self.assertFalse(result['original_bytes_deleted'])
        self.assertEqual(self.store.revoke_dataset(self.dataset, 1, 'REVOKE-001'), result)
        with self.assertRaisesRegex(CorpusError, 'dataset_revoked'):
            self.commit(value)
        with self.assertRaisesRegex(CorpusError, 'dataset_revoked'):
            self.preview(operation='PREVIEW-002')
        self.assertEqual(self.store.inspect(self.dataset)['datasets'][0]['state'], 'revoked')

    def test_interrupted_commit_can_resume_after_original_was_written(self):
        preview = self.preview()
        original_save = self.store._save
        def fail_commit(db, operation_id, action, request, result):
            if action == 'import_commit':
                raise OSError('STATE-001')
            return original_save(db, operation_id, action, request, result)
        with patch.object(self.store, '_save', side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.commit(preview)
        self.assertEqual(self.store.inspect()['original_count'], 0)
        interrupted = self.store.verify_originals()
        self.assertFalse(interrupted['passed'])
        self.assertEqual(interrupted['unindexed_files'], 1)
        resumed = self.commit(preview)
        self.assertEqual(self.store.inspect()['original_count'], 1)
        self.assertEqual(self.store._blob(resumed['file_hash']).read_bytes(), self.raw)
        self.assertTrue(self.store.verify_originals()['passed'])

    def test_cloud_and_expression_permissions_cannot_be_enabled_in_foundation_schema(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as db:
                db.execute('UPDATE corpus_datasets SET cloud_learning=1 WHERE id=?', (self.dataset,))
        self.assertFalse(self.store.inspect()['cloud_learning'])
        self.assertFalse(self.store.inspect()['expression_use'])

    def test_entire_local_flow_runs_without_network(self):
        with patch('socket.socket', side_effect=AssertionError('NETWORK-001')):
            value = self.preview()
            self.commit(value)
            self.store.verify_originals()

    def test_cli_reports_only_metadata_and_controlled_errors(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--corpus', str(self.root), 'import-preview', '--dataset-id', self.dataset,
                         '--source', str(self.file), '--operation-id', 'CLI-001'])
        self.assertEqual(code, 0)
        self.assertNotIn('ID-002', output.getvalue())
        value = json.loads(output.getvalue())
        self.assertFalse(value['manifest']['learning_eligible'])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--corpus', str(self.directory / 'not-initialized'), 'inspect'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'corpus_not_initialized')


if __name__ == '__main__':
    unittest.main()
