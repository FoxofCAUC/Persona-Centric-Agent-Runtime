"""New author-version publication protection, using artificial data only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from scripts import private_publication_guard as guard


OLD = '人工历史表达资料：蓝色纸船在没有海的模型池里兜圈，这句仅用于测试旧作者版本的发布防护。'
NEW = '人工新表达资料：木头风车带着一串会发光的骰子转动，这句仅用于测试新作者版本的发布防护。'
OTHER = '这是另一个不受本轮私密登记保护的人工人物资料，不应被错误扩大为当前登记人物的隐私匹配片段。'


class AuthorPublicationTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='author-publication-artificial-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = RuntimePaths(self.root / 'home')
        self.registry = self.paths.home / 'private/personas/registry.json'
        self.registry.parent.mkdir(parents=True)
        self.register([{'id': 'artificial-private', 'persona_id': 'protected', 'state': 'ready'}])
        self.db = self.paths.home / 'data/database/runtime.sqlite3'
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE profile_versions(persona_id TEXT,kind TEXT,data_json TEXT)')
            db.execute('CREATE TABLE character_versions(persona_id TEXT,data_json TEXT)')
            for kind, data in [('core', {'name': '人工防护测试人物'}), ('expression', {'style': OLD}),
                               ('expression', {'style': NEW}), ('relationship', {'description': ''}),
                               ('state', {'text': '', 'expires_at': None})]:
                db.execute('INSERT INTO profile_versions VALUES(?,?,?)', ('protected', kind, json.dumps(data)))
            db.execute('INSERT INTO character_versions VALUES(?,?)',
                       ('protected', json.dumps({'social': {'public_image': NEW}})))
            db.execute('INSERT INTO profile_versions VALUES(?,?,?)',
                       ('unprotected', 'expression', json.dumps({'style': OTHER})))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'safe.txt').write_text('Generic code.', encoding='utf-8')

    def register(self, records):
        self.registry.write_text(json.dumps({'version': 1, 'records': records}), encoding='utf-8')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('-c', 'user.name=Artificial Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def test_current_new_text_detected_without_reporting_body_or_writing_database(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        (self.repo / 'leak.txt').write_text(NEW, encoding='utf-8')
        result = guard.author_version_scan(self.paths, self.repo)
        self.assertFalse(result['passed'])
        self.assertEqual(result['hits'], [{'location': 'leak.txt', 'reason': 'private_author_version_content'}])
        self.assertNotIn(NEW, json.dumps(result, ensure_ascii=False))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_historical_profile_text_remains_protected(self):
        (self.repo / 'old.txt').write_text(OLD, encoding='utf-8')
        self.assertFalse(guard.author_version_scan(self.paths, self.repo)['passed'])

    def test_character_history_only_text_is_protected(self):
        text = '人工角色历史专用资料：棱角分明的橙色小屋装在虚构的口袋里，与当前表达资料没有共同正文。'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO character_versions VALUES(?,?)',
                       ('protected', json.dumps({'social': {'public_image': text}})))
        (self.repo / 'character.txt').write_text(text, encoding='utf-8')
        self.assertFalse(guard.author_version_scan(self.paths, self.repo)['passed'])

    def test_json_ascii_escaped_text_is_protected(self):
        (self.repo / 'escaped.json').write_text(json.dumps({'value': NEW}), encoding='utf-8')
        self.assertFalse(guard.author_version_scan(self.paths, self.repo)['passed'])

    def test_staged_blob_not_replaced_by_clean_working_file(self):
        p = self.repo / 'staged.txt'
        p.write_text(NEW, encoding='utf-8')
        self.git('add', 'staged.txt')
        p.write_text('Clean worktree content.', encoding='utf-8')
        self.assertTrue(guard.author_version_scan(self.paths, self.repo)['passed'])
        self.assertFalse(guard.author_version_scan(self.paths, self.repo, staged=True)['passed'])

    def test_outgoing_deleted_blob_is_checked(self):
        base = self.commit('safe base')
        p = self.repo / 'past.txt'
        p.write_text(NEW, encoding='utf-8')
        self.commit('artificial private content')
        p.unlink()
        self.commit('remove artificial private content')
        self.assertTrue(guard.author_version_scan(self.paths, self.repo)['passed'])
        result = guard.author_version_scan(self.paths, self.repo, base=base)
        self.assertFalse(result['passed'])
        self.assertTrue(any(h['location'].endswith(':past.txt') for h in result['hits']))

    def test_no_registered_persona_needs_no_database(self):
        self.register([{'id': 'not-imported', 'persona_id': None, 'state': 'registered'}])
        self.db.unlink()
        self.assertEqual(guard.author_version_scan(self.paths, self.repo),
                         {'passed': True, 'checked_blobs': 0, 'hits': []})
        self.register([])
        self.assertEqual(guard.author_version_needles(self.paths), set())

    def test_ready_record_missing_target_is_rejected(self):
        self.register([{'id': 'invalid', 'persona_id': None, 'state': 'ready'}])
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_author_scope_invalid'):
            guard.author_version_scan(self.paths, self.repo)

    def test_database_missing_schema_or_registered_rows_is_fail_closed(self):
        self.db.unlink()
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_author_history_unavailable'):
            guard.author_version_scan(self.paths, self.repo)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE profile_versions(persona_id TEXT,kind TEXT,data_json TEXT)')
            db.execute('CREATE TABLE character_versions(persona_id TEXT,data_json TEXT)')
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_author_history_unavailable'):
            guard.author_version_scan(self.paths, self.repo)
        with closing(sqlite3.connect(self.db)) as db, db: db.execute('CREATE TABLE unrelated(x)')
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_author_history_unavailable'):
            guard.author_version_scan(self.paths, self.repo)

    def test_malformed_version_fails_without_exposing_content(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE character_versions SET data_json=?', ('invalid ' + NEW,))
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_author_history_unavailable') as error:
            guard.author_version_scan(self.paths, self.repo)
        self.assertNotIn(NEW, str(error.exception))

    def test_unprotected_persona_text_is_not_added_to_scope(self):
        (self.repo / 'generic.txt').write_text(OTHER, encoding='utf-8')
        self.assertTrue(guard.author_version_scan(self.paths, self.repo)['passed'])

    def test_formal_copy_inherits_registered_privacy_root(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE persona_lifecycle(persona_id TEXT,privacy_root TEXT)')
            db.execute('INSERT INTO persona_lifecycle VALUES(?,?)', ('copy', 'protected'))
            db.execute('INSERT INTO profile_versions SELECT ?,kind,data_json FROM profile_versions WHERE persona_id=?',
                       ('copy', 'protected'))
            db.execute('UPDATE profile_versions SET data_json=? WHERE persona_id=? AND kind=?',
                       (json.dumps({'style': OTHER}), 'copy', 'expression'))
            db.execute('INSERT INTO character_versions SELECT ?,data_json FROM character_versions WHERE persona_id=?',
                       ('copy', 'protected'))
        (self.repo / 'copy.txt').write_text(OTHER, encoding='utf-8')
        self.assertFalse(guard.author_version_scan(self.paths, self.repo)['passed'])

    def test_original_guard_runs_first_and_failure_never_skipped(self):
        calls = []
        failure = {'passed': False, 'checked_blobs': 1,
                   'hits': [{'location': 'original.txt', 'reason': 'private_or_runtime_content'}]}
        with patch.object(guard, 'publication_scan', side_effect=lambda *a, **k: calls.append('original') or failure), \
                patch.object(guard, 'author_version_scan', side_effect=lambda *a, **k: calls.append('author')):
            self.assertEqual(guard.check_publication(self.paths, self.repo), failure)
        self.assertEqual(calls, ['original'])
        with patch.object(guard, 'publication_scan', return_value={'passed': True, 'checked_blobs': 1, 'hits': []}):
            (self.repo / 'new.txt').write_text(NEW, encoding='utf-8')
            result = guard.check_publication(self.paths, self.repo)
        self.assertFalse(result['passed'])
        self.assertFalse(result['author_versions']['passed'])


if __name__ == '__main__': unittest.main()
