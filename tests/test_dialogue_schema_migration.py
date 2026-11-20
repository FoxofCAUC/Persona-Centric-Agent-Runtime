"""结构33迁移与旧程序实际写入，测试材料均为本地标识符。"""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

from persona_runtime.config import ConfigStore
from persona_runtime.migration import export_instance, import_instance, rollback_instance
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.recall_rollback import prepare_snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store, SCHEMA_VERSION


class DialogueMigration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = RuntimePaths(self.root / 'primary')
        ConfigStore(self.paths).initialize()
        self.store = Store(self.paths)

    def seed_generation(self):
        self.store.initialize()
        service = RuntimeService(self.store)
        pid = service.create_persona(generic_persona())['id']
        service.use_persona(pid)
        session = service.create_session()['id']
        user_event = service.append_event(session, 'user', identifier())['id']
        reply_event = service.append_event(session, 'persona', identifier())['id']
        turn, unit, at = identifier(), identifier(), timestamp()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO dialogue_generation_policies VALUES(?,?,?)',
                       (pid, 1, '{"enabled":true}'))
            db.execute("""INSERT INTO dialogue_source_bundles(id,persona_id,scope_key,authority,home,purpose,
                manifest_path,manifest_hash,state,created_at)
                VALUES(?,?,?,?,?,'offline_replay',?,?,'active',?)""",
                (identifier(), pid, identifier(), identifier(), str(self.paths.home), identifier(), identifier(), at))
            db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,status,
                model_snapshot,created_at,updated_at,user_event_id,reply_event_id)
                VALUES(?,?,?,?,'desktop','completed','{}',?,?,?,?)""",
                       (turn, turn, pid, session, at, at, user_event, reply_event))
            db.execute('INSERT INTO dialogue_generation_units VALUES(?,?,?,?,?,?,?)',
                       (unit, turn, 'main', '{}', '{}', 1, at))
            for stage, state, raw in (('understanding','completed',b'{}'), ('selection','sending',None)):
                db.execute("""INSERT INTO dialogue_generation_stages(id,unit_id,stage,state,model_json,
                    request_json,request_hash,dependencies_json,attempt_id,started_at,raw_response,raw_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier(), unit, stage, state, '{}', '{}', hashlib.sha256(b'{}').hexdigest(),
                     '{}', identifier(), at, raw, hashlib.sha256(raw).hexdigest() if raw else None))
        return session

    def assert_paused(self, store):
        with store.connection() as db:
            raw = db.execute('SELECT data_json FROM dialogue_generation_policies').fetchone()[0]
            self.assertFalse(json.loads(raw)['enabled'])
            self.assertEqual(db.execute('SELECT state FROM dialogue_source_bundles').fetchone()[0], 'paused')
            states = dict(db.execute('SELECT stage,state FROM dialogue_generation_stages'))
            self.assertEqual(states, {'understanding':'completed', 'selection':'unknown'})
            self.assertEqual(db.execute("""SELECT raw_response,raw_hash FROM dialogue_generation_stages
                              WHERE stage='understanding'""").fetchone()[:],
                             (b'{}', hashlib.sha256(b'{}').hexdigest()))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_whole_package_preserves_evidence_and_pauses_candidate(self):
        self.seed_generation()
        package = export_instance(self.paths)
        target = RuntimePaths(self.root / 'target')
        self.assertEqual(import_instance(target, package['path'], apply=True)['state'], 'needs_configuration')
        self.assert_paused(Store(target))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM dialogue_generation_stages WHERE stage='selection'").fetchone()[0],
                             'sending')

    def test_incomplete_compatibility_copy_is_refused_atomically(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE dialogue_generation_stages')
            db.execute('PRAGMA user_version=32')
        with self.assertRaisesRegex(RuntimeErrorBase, 'dialogue_schema_incomplete'):
            self.store.initialize()
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 32)

    def old_program(self, revision, home, session=''):
        repo = Path(__file__).resolve().parents[1]
        git = shutil.which('git')
        if not git or not (repo/'.git').exists():
            self.skipTest('历史源码验证需要本地Git对象')
        archive = subprocess.run([git,'archive','--format=zip',revision, 'persona_runtime'],
                                 cwd=repo, capture_output=True)
        if archive.returncode:
            self.skipTest('当前源码副本缺少固定的历史提交')
        old = self.root / ('program-' + revision)
        with zipfile.ZipFile(io.BytesIO(archive.stdout)) as z:
            z.extractall(old)
        program = """import asyncio,json,sys
from pathlib import Path
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store,SCHEMA_VERSION
p=RuntimePaths(Path(sys.argv[1]))
s=Store(p)
s.initialize()
service=RuntimeService(s)
if not sys.argv[2]:
    pid=service.create_persona(generic_persona())['id']
    service.use_persona(pid)
sid=sys.argv[2] or service.create_session()['id']
cfg=ConfigStore(p)
cfg.initialize()
cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-flash'}})
class OfflineClient:
    async def complete(self, model, messages):
        return identifier(), {'total_tokens':1}
async def run():
    chat=ChatEngine(s,cfg,OfflineClient())
    submitted=await chat.submit('desktop',identifier(),identifier(),sid)
    await chat.wait()
    turn=chat.get(submitted['id'])
    assert turn['status']=='completed', (turn['status'],turn['error_code'])
    print(json.dumps([SCHEMA_VERSION,turn['user_event_id'],turn['reply_event_id']]))
asyncio.run(run())
"""
        env = dict(os.environ, PYTHONPATH=str(old))
        proc = subprocess.run([sys.executable, '-B','-X','utf8','-c',program,str(home),session],
                              cwd=old, env=env, capture_output=True, check=False, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8',errors='replace'))
        return json.loads(proc.stdout)

    def test_actual_rc7_writes_conversation_then_reupgrade(self):
        session = self.seed_generation()
        snap = prepare_snapshot(self.paths, target_schema=32)
        compat = RuntimePaths(self.root / 'rc7')
        compat.initialize()
        store = Store(compat)
        shutil.copyfile(snap['path'], store.path)
        rev, *events = self.old_program('9a00d983cab30c9b3b81f2f94f247fc13e8ed54b',compat.home,session)
        self.assertEqual(rev, 32)
        store.initialize()
        self.assert_paused(store)
        self.assertTrue(list((compat.home/'backups').glob('before-dialogue-generation-*.sqlite3')))
        with store.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE id IN (?,?)',events).fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM dialogue_generation_turns').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT status FROM turns WHERE user_event_id=? AND reply_event_id=?',
                                        events).fetchone()[0], 'completed')
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='dialogue_generation_turn_freeze'").fetchone())
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE id IN (?,?)',events).fetchone()[0], 0)

    def test_original_27_and_32_databases_upgrade_preserving_completed_turn(self):
        versions = {27:'678cd690c1495933a4332bcc58617b2abdeb8dc0',
                    32:'9a00d983cab30c9b3b81f2f94f247fc13e8ed54b'}
        for schema, commit in versions.items():
            with self.subTest(schema=schema):
                store = Store(RuntimePaths(self.root / ('original-' + str(schema))))
                rev, *events = self.old_program(commit, store.paths.home)
                self.assertEqual(rev, schema)
                store.initialize()
                with store.connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM events WHERE id IN (?,?)',events).fetchone()[0], 2)
                    self.assertEqual(db.execute('SELECT status FROM turns WHERE user_event_id=? AND reply_event_id=?',
                                                events).fetchone()[0], 'completed')
                    self.assertEqual(db.execute('SELECT count(*) FROM dialogue_generation_turns').fetchone()[0], 0)
                    self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    def test_selected_corpus_only_and_rollback(self):
        from persona_runtime.corpus_dataset import DatasetCorpusStore
        self.store.initialize()
        corpus = DatasetCorpusStore(self.root/'corpus')
        info = corpus.initialize()
        ident = info['corpus_id']
        did = corpus.create_dataset(identifier(), identifier())['id']
        original = self.root/'original.md'
        original.write_text(identifier(), encoding='utf-8')
        preview = corpus.preview(did, original, identifier())
        corpus.retain_original(preview['id'], preview['manifest_hash'], identifier())
        (corpus.root/'unrelated').write_bytes(b'123')
        default = export_instance(self.paths)
        self.assertEqual(default['included_corpora'], [])
        chosen = export_instance(self.paths, include_corpora=[corpus.root])
        self.assertEqual(chosen['included_corpora'], [ident])
        with zipfile.ZipFile(chosen['path']) as bundle:
            self.assertFalse(any('unrelated' in n or '/backups/' in n for n in bundle.namelist()))
        target = RuntimePaths(self.root/'target')
        import_instance(target, chosen['path'], apply=True)
        migrated = DatasetCorpusStore(target.home/'private/corpora'/ident)
        self.assertTrue(migrated.verify_originals()['passed'])
        self.assertEqual(migrated.inspect(did)['corpus_id'], ident)
        self.assertEqual(migrated._identity_key(), corpus._identity_key())
        rollback_instance(target, apply=True)
        self.assertFalse(migrated.root.exists())
        import_instance(target, chosen['path'], apply=True)
        self.assertEqual(migrated.inspect(did)['corpus_id'], ident)

    def test_selected_corpus_cannot_overwrite_existing_private_store(self):
        from persona_runtime.corpus_dataset import DatasetCorpusStore
        self.store.initialize()
        corpus = DatasetCorpusStore(self.root/'corpus')
        ident = corpus.initialize()['corpus_id']
        chosen = export_instance(self.paths, include_corpora=[corpus.root])
        target = RuntimePaths(self.root/'target')
        ConfigStore(target).initialize()
        folder = target.home/'private/corpora'/ident
        folder.mkdir(parents=True)
        kept = folder/'local'
        kept.write_bytes(b'123')
        with self.assertRaisesRegex(RuntimeErrorBase, 'migration_corpus_target_occupied'):
            import_instance(target, chosen['path'], apply=True)
        self.assertEqual(kept.read_bytes(), b'123')
        self.assertFalse(Store(target).path.exists())

    def test_corpus_catalog_verified_inside_package(self):
        from persona_runtime.corpus_dataset import DatasetCorpusStore
        self.store.initialize()
        corpus = DatasetCorpusStore(self.root/'corpus')
        info = corpus.initialize()
        chosen = export_instance(self.paths, include_corpora=[corpus.root])
        partial = self.root/'partial.zip'
        with zipfile.ZipFile(chosen['path']) as original, zipfile.ZipFile(partial,'w') as copy:
            manifest = json.loads(original.read('manifest.json'))
            manifest['files'].pop('private/corpora/'+info['corpus_id']+'/identity.key')
            for name in manifest['files']:
                copy.writestr(name, original.read(name))
            copy.writestr('manifest.json', json.dumps(manifest))
        target = RuntimePaths(self.root/'target')
        with self.assertRaisesRegex(RuntimeErrorBase, 'migration_corpus_manifest_invalid'):
            import_instance(target, partial, apply=True)
        self.assertFalse(Store(target).path.exists())


if __name__ == '__main__':
    unittest.main()
