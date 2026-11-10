"""Real entry-point export, fenced recovery, package rollback and native controls."""

from contextlib import closing, contextmanager
import copy
import gc
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.knowledge_admin import overview, rollback
from persona_runtime.chat import ModelError
from persona_runtime.knowledge_service import KnowledgeService, read
from persona_runtime.knowledge_transfer import restore, snapshot, validate
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.portable_memory import import_export
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.server import create_app
from tests import test_knowledge_maintenance as maintenance
from tests import test_knowledge_integration as integration
from tests import test_desktop as desktop


class KnowledgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = maintenance.MaintenanceTests.setUp
    asyncTearDown = maintenance.MaintenanceTests.asyncTearDown
    enable = maintenance.MaintenanceTests.enable
    submit = maintenance.MaintenanceTests.submit
    process = maintenance.MaintenanceTests.process

    async def populated(self):
        self.enable(); self.source.put(); self.submit(); await self.process()

    def export(self):
        path = Path(self.service.export_persona()['path'])
        return json.loads(path.read_text(encoding='utf-8'))

    async def test_format20_exports_public_versions_and_imports_under_review(self):
        await self.populated()
        package = self.export()
        self.assertEqual(package['format_version'], 20)
        validate(package['knowledge'])
        self.assertEqual(len(package['knowledge']['tables']['knowledge_claims']), 1)
        self.assertIsNone(package['knowledge']['tables']['knowledge_batches'][0]['extract_task_id'])
        paths = RuntimePaths(self.paths.home / '新的 中文环境')
        store = Store(paths); store.initialize()
        service = RuntimeService(store)
        owner = service.create_persona(generic_persona()); service.use_persona(owner['id'])
        result = import_export(store, package, owner['id'], 'import-20')
        self.assertEqual(import_export(store, package, owner['id'], 'import-20'), result)
        self.assertTrue(result['knowledge_review_required'])
        knowledge = KnowledgeService(store)
        self.assertFalse(knowledge.current_policy(result['id'])['data']['enabled'])
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM knowledge_claims').fetchone()[0], 'quarantined')
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_fetch_jobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM organization_tasks').fetchone()[0], 0)
        knowledge.close()

    async def test_old_exports_remain_importable_and_invalid_evidence_is_atomic(self):
        await self.populated()
        package = self.export()
        package['knowledge']['tables']['knowledge_evidence'][0]['quote'] = '伪造引文'
        with self.store.connection() as db:
            count = db.execute('SELECT count(*) FROM personas').fetchone()[0]
        with self.assertRaises(RuntimeErrorBase):
            import_export(self.store, package, self.pid, 'bad-import')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM personas').fetchone()[0], count)
        old = self.export(); old['format_version'] = 19; old.pop('knowledge')
        self.assertTrue(import_export(self.store, old, self.pid, 'legacy-import')['history_imported'])

    async def test_backup_restore_preserves_corrections_withdrawal_permissions_and_calls(self):
        await self.populated()
        self.service.backup('before.sqlite3')
        self.source.put(value='红色', revision=2); self.now += 2; self.submit(); await self.process()
        with self.store.connection() as db:
            target = db.execute('SELECT id FROM knowledge_targets').fetchone()[0]
            attempts = db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]
        self.maintenance.withdraw(self.pid, target, 'retracted', 0, 'withdraw')
        source = self.maintenance.current_source('wikipedia_zh')
        self.maintenance.update_source('wikipedia_zh', source['data'] | {'enabled': False}, 0, 'source-off')
        result = restore(self.chat.knowledge, 'before.sqlite3', 0, 'restore-1')
        self.assertEqual(restore(self.chat.knowledge, 'before.sqlite3', 0, 'restore-1'), result)
        self.assertFalse(result['data']['execution_restored'])
        self.assertFalse(self.maintenance.current_source('wikipedia_zh')['data']['enabled'])
        with self.store.connection() as db:
            row = db.execute('SELECT latest_revision,state FROM knowledge_pages').fetchone()
            self.assertEqual(tuple(row), (2, 'withdrawn'))
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0], attempts)
        self.assertFalse(self.chat.knowledge.search(self.pid, '测试行星颜色是什么')['evidence'])
        self.assertEqual(len(self.processor.calls), 4)

    async def test_restore_missing_public_rows_and_reject_tampered_backup(self):
        await self.populated()
        self.service.backup('saved.sqlite3')
        with self.store.connection(write=True) as db:
            db.execute('UPDATE knowledge_pages SET head_document_id=NULL')
            for table in ('knowledge_evidence','knowledge_claims','knowledge_batches','knowledge_fetch_jobs',
                          'knowledge_targets','knowledge_documents','knowledge_pages'):
                db.execute('DELETE FROM ' + table)
        restore(self.chat.knowledge, 'saved.sqlite3', 0, 'recover-data')
        found = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        self.assertTrue(found['evidence'])
        self.assertEqual(len(self.processor.calls), 2)
        with self.store.connection() as db:
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_fetch_jobs').fetchone()[0], 0)
        backup = self.paths.home / 'backups/saved.sqlite3'
        with backup.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(RuntimeErrorBase, 'knowledge_backup_invalid'):
            restore(self.chat.knowledge, 'saved.sqlite3', 1, 'bad-backup')

    async def test_overview_has_cost_freshness_coverage_and_isolated_metadata(self):
        await self.populated()
        result = overview(self.chat.knowledge, self.pid)
        self.assertEqual(result['cost']['calls'], 2)
        self.assertEqual(result['cost']['tokens_known'], 22)
        self.assertIsNone(result['cost']['amount'])
        self.assertEqual(result['pages'][0]['freshness'], 'fresh')
        self.assertIsNotNone(result['batches'][0]['coverage'])
        self.assertEqual(self.chat.knowledge.operation_show('read')['state'], 'completed')
        self.assertEqual(self.chat.knowledge.operation_show('absent')['state'], 'not_found')
        with self.assertRaisesRegex(RuntimeErrorBase, 'knowledge_backup_name_invalid'):
            restore(self.chat.knowledge, '../runtime.sqlite3', 0, 'traversal')

    async def test_overview_does_not_open_nested_database_readers(self):
        await self.populated()
        connection = self.store.connection
        depth = 0
        @contextmanager
        def checked(*args, **kwargs):
            nonlocal depth
            self.assertEqual(depth, 0)
            with connection(*args, **kwargs) as db:
                depth += 1
                try:
                    yield db
                finally:
                    depth -= 1
        with patch.object(self.store, 'connection', checked):
            self.assertEqual(overview(self.chat.knowledge, self.pid)['cost']['calls'], 2)

    async def test_legacy_backup_without_manifest_uses_current_policy(self):
        await self.populated()
        backup = Path(self.service.backup('legacy.sqlite3')['path'])
        backup.with_suffix('.sqlite3.knowledge.json').unlink()
        result = restore(self.chat.knowledge, 'legacy.sqlite3', 0, 'legacy-restore')
        self.assertEqual(result['data']['manifest_state'], 'legacy_observed')
        self.assertEqual(len(self.processor.calls), 2)

    async def test_owned_automatic_backup_can_be_recovered(self):
        await self.populated()
        from persona_runtime.maintenance import automatic_backup
        automatic_backup(self.paths, self.now)
        index = json.loads((self.paths.home / 'backups/workbench-automatic/index.json').read_text(encoding='utf-8'))
        name = 'workbench-automatic/' + index['entries'][-1]['file']
        self.assertEqual(restore(self.chat.knowledge, name, 0, 'automatic-restore')['data']['manifest_state'], 'verified')
        self.assertEqual(len(self.processor.calls), 2)

    async def test_missing_dependencies_stop_before_package_download(self):
        with patch('persona_runtime.knowledge_admin.dependencies', return_value={'ready': False, 'missing': ['jieba']}), \
                patch('persona_runtime.knowledge_service.PackageInstaller') as installer:
            with self.assertRaisesRegex(RuntimeErrorBase, 'knowledge_dependencies_missing'):
                self.chat.knowledge.prepare_package(0, 'missing-dependencies')
            installer.assert_not_called()

    async def test_import_preserves_input_and_current_invalidation(self):
        await self.populated()
        package = self.export(); original = copy.deepcopy(package)
        self.chat.knowledge.invalidate('a' * 64, 1, 'withdrawn', 'current fence', 0, 'fence')
        imported = import_export(self.store, package, self.pid, 'import-with-fence')
        self.assertEqual(package, original)
        self.assertEqual(import_export(self.store, package, self.pid, 'import-with-fence'), imported)
        with self.store.connection() as db:
            self.assertEqual(read(db, 'invalidations', {})['data']['a' * 64]['1']['state'], 'withdrawn')

    async def test_restore_does_not_replay_unknown_model_effect(self):
        self.enable(); self.source.put(); self.submit()
        self.processor.failure = ModelError('provider_network_error', diagnostics={'request_headers_sent': True})
        await self.process()
        self.service.backup('unknown.sqlite3')
        restore(self.chat.knowledge, 'unknown.sqlite3', 0, 'restore-unknown')
        self.org.recover(); await self.process()
        self.assertEqual(len(self.processor.calls), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM organization_tasks').fetchone()[0], 'blocked_unknown')
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0], 1)

    async def test_http_management_backup_restore_and_receipt(self):
        await self.populated()
        app = create_app(self.paths, self.processor)
        async with TestServer(app) as server, ClientSession() as client:
            admin = {'Authorization': 'Bearer ' + self.cfg.token()}
            bridge = {'Authorization': 'Bearer ' + self.cfg.token('bridge')}
            url = server.make_url('/v1/manage')
            request = {'action': 'knowledge.manage.show', 'args': {'persona_id': self.pid}}
            async with client.post(url, headers=bridge, json=request) as response:
                self.assertEqual(response.status, 403)
            async with client.post(url, headers=admin, json=request) as response:
                result = await response.json()
                self.assertEqual(response.status, 200)
                self.assertEqual(result['cost']['calls'], 2)
            async with client.post(url, headers=admin, json={'action': 'data.backup', 'args': {'name': 'api.sqlite3'}}) as response:
                self.assertEqual(response.status, 200)
            args = {'name': 'api.sqlite3', 'expected_revision': 0, 'operation_id': 'api-restore'}
            for _ in range(2):
                async with client.post(url, headers=admin, json={'action': 'knowledge.restore.submit', 'args': args}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertFalse((await response.json())['data']['execution_restored'])
            async with client.post(url, headers=admin, json={'action': 'knowledge.operation.show', 'args': {'operation_id': 'api-restore'}}) as response:
                self.assertEqual((await response.json())['state'], 'completed')


class KnowledgePackageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = integration.KnowledgeIntegrationTests.setUp
    asyncTearDown = integration.KnowledgeIntegrationTests.asyncTearDown
    enable = integration.KnowledgeIntegrationTests.enable

    async def test_switch_rollback_receipt_and_rebind_under_new_home(self):
        self.enable()
        old = self.identity
        other = self.package.parent / 'second'
        shutil.copytree(self.package, other)
        with closing(sqlite3.connect(other / 'body.db')) as db, db:
            db.execute("UPDATE sections SET content='第二包的人工月球资料。'")
        from persona_runtime.knowledge_package import file_digest
        from persona_runtime.config import atomic_json
        manifest = json.loads((other / 'manifest.json').read_text(encoding='utf-8'))
        manifest['database']['sha256'] = file_digest(other / 'body.db')
        atomic_json(other / 'manifest.json', manifest)
        self.knowledge.activate_package('second', 1, 'second')
        result = rollback(self.knowledge, 2, 'rollback')
        self.assertEqual(result['data']['active'], old)
        self.assertEqual(rollback(self.knowledge, 2, 'rollback'), result)
        moved = self.paths.home / '另一 环境'
        paths = RuntimePaths(moved); paths.initialize()
        store = Store(paths); store.initialize()
        destination = moved / 'data/knowledge/packages/copied'
        destination.parent.mkdir(parents=True)
        shutil.copytree(self.package, destination)
        service = KnowledgeService(store)
        service.activate_package('copied', 0, 'rebind')
        self.assertEqual(service.status()['registry']['data']['active'], old)
        service.close()


class KnowledgeDesktopTests(unittest.TestCase):
    setUp = desktop.DesktopTests.setUp
    window = desktop.DesktopTests.window

    def doCleanups(self):
        for window in self.windows:
            window.hub.pool.shutdown(wait=True, cancel_futures=True)
            window.hub.drain()
        result = unittest.TestCase.doCleanups(self)
        self.windows.clear()
        gc.collect()
        return result

    def test_page_controls_and_stale_role_response(self):
        w = self.window(); w.show_page('knowledge'); w.update()
        w.geometry('780x560'); w.update()
        labels = {b.cget('text') for b, _ in w.buttons}
        self.assertTrue({'读取知识状态','停止学习','回退上一包','合并恢复知识记录','找回上次操作'} <= labels)
        body = w.pages['knowledge'].body
        for button, _ in w.buttons:
            if str(button).startswith(str(body)):
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), body.winfo_rootx() + body.winfo_width() + 2)
        w.persona_id = 'first'; pending = []
        with patch.object(w, 'run_task', side_effect=lambda *a, **k: pending.append((a,k))):
            w.refresh_knowledge()
        w.persona_id = 'second'; w.view_epoch += 1
        pending[0][0][3]({'wrong': True})
        self.assertIsNone(w.knowledge_data)
        self.assertFalse(pending[0][1]['error_current']())

    def test_pending_operation_survives_new_window_and_uses_same_id(self):
        w = self.window(); w.persona_id = 'role'
        w.knowledge_data = {}; w.knowledge_loaded_key = w.knowledge_key()
        with patch.object(w, 'knowledge_ready', return_value=True), patch.object(w, 'send_knowledge_operation') as send:
            w.knowledge_write('knowledge.package.prepare', {'expected_revision': 0})
            first = copy.deepcopy(send.call_args[0][0])
            w.knowledge_write('knowledge.package.prepare', {'expected_revision': 0})
            self.assertEqual(first, send.call_args[0][0])
        second = self.window(); second.persona_id = 'role'
        with patch.object(second, 'send_knowledge_operation') as send:
            second.resume_knowledge_operation()
            self.assertEqual(first, send.call_args[0][0])
        second.clear_knowledge_intent('different-operation')
        self.assertTrue(second.knowledge_intent_path().exists())

    def test_stop_learning_can_be_saved_while_another_operation_is_unconfirmed(self):
        w = self.window(); w.persona_id = 'role'
        with patch.object(w, 'knowledge_ready', return_value=True), patch.object(w, 'send_knowledge_operation'):
            w.knowledge_write('knowledge.package.prepare', {'expected_revision': 0})
            w.knowledge_write('knowledge.maintenance.policy.update', {'expected_revision': 0, 'data': {'enabled': False}}, priority=True)
        self.assertTrue(w.knowledge_intent_path().exists())
        self.assertTrue(w.knowledge_intent_path(True).exists())
        self.assertTrue(w.pending_knowledge_operation()['priority'])

    def test_stop_uses_current_model_binding_after_configuration_change(self):
        w = self.window(); w.persona_id = 'role'
        w.knowledge_data = {'maintenance_policy': {'revision': 1, 'data': {
            'enabled': True, 'provider': 'old', 'sources': ['wikipedia_zh'], 'daily_scan': True}},
            'maintenance_provider': 'current'}
        with patch.object(w, 'knowledge_ready', return_value=True), patch.object(w, 'knowledge_write') as write:
            w.save_knowledge_maintenance(stop=True)
        args = write.call_args[0][1]
        self.assertFalse(args['data']['enabled'])
        self.assertEqual(args['data']['provider'], 'current')
        self.assertTrue(write.call_args[1]['priority'])


if __name__ == '__main__':
    unittest.main()
