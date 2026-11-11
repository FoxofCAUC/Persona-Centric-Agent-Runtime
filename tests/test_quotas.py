"""Quota editing, unlimited semantics and recoverable machine-acceptance scopes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.service import RuntimeService
from persona_runtime.chat import ChatEngine
from persona_runtime.organization import OrganizationEngine
from persona_runtime.server import create_app
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base=RuntimePaths.from_environment().home/'tmp';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base,prefix='quota-');self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.store=Store(self.paths);self.store.initialize();self.quotas=QuotaService(self.store,self.cfg)

    async def test_development_is_persistent_unlimited_with_recoverable_base_limits(self):
        before=self.quotas.current()['limits']
        started=self.quotas.begin_development()
        self.assertTrue(started['development_unlimited']);self.assertTrue(all(v==0 for v in started['limits'].values()))
        reloaded=QuotaService(self.store,self.cfg)
        self.assertTrue(reloaded.current()['development_unlimited'])
        self.cfg.update({'organization':{'hourly_limit':1,'daily_limit':1}})
        self.assertEqual(reloaded.current()['limits']['background_daily'],0)
        # A nested legacy acceptance scope cannot remove development status.
        reloaded.begin_acceptance();self.assertTrue(reloaded.current()['development_unlimited'])
        reloaded.end_acceptance();self.assertEqual(reloaded.current()['limits'],before)
        self.assertFalse(reloaded.current()['development_unlimited'])

    async def test_zero_is_valid_but_negative_bool_and_unknown_fields_are_rejected(self):
        unlimited=dict(project_hourly=0,background_hourly=0,background_daily=0)
        self.assertEqual(self.quotas.update(unlimited)['limits'],unlimited | {'selector_hourly':300,'selector_daily':2000})
        for bad in (-1,True,10001,'0'):
            with self.assertRaises(RuntimeErrorBase):self.quotas.update(dict(unlimited,project_hourly=bad))
        with self.assertRaises(RuntimeErrorBase):self.quotas.update(dict(unlimited,other=1))

    async def test_scope_restores_on_exception_and_nested_scope_does_not_end_outer(self):
        original=self.quotas.current()['limits']
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with self.quotas.acceptance():
                with self.quotas.acceptance():self.assertTrue(self.quotas.current()['acceptance_active'])
                self.assertTrue(self.quotas.current()['acceptance_active'])
                raise RuntimeError('injected')
        self.assertEqual(self.quotas.current()['limits'],original)
        self.assertFalse(self.quotas.current()['acceptance_active'])

    async def test_repeated_begin_and_process_reopen_keep_original_restore_values(self):
        before=self.quotas.current()['limits'];self.quotas.begin_acceptance();self.quotas.begin_acceptance()
        other=QuotaService(self.store,ConfigStore(self.paths))
        self.assertEqual(other.current()['restore_limits'],before)
        other.end_acceptance();other.end_acceptance()
        self.assertEqual(other.current()['limits'],before)

    async def test_edit_while_acceptance_active_changes_restore_values(self):
        self.quotas.begin_acceptance();target=dict(project_hourly=31,background_hourly=7,background_daily=21,selector_hourly=32,selector_daily=201)
        result=self.quotas.update(target)
        self.assertEqual(result['limits'],dict.fromkeys(target,0));self.assertEqual(result['restore_limits'],target)
        self.quotas.end_acceptance();self.assertEqual(self.quotas.current()['limits'],target)

    async def test_unlimited_chat_does_not_treat_zero_as_exhausted_and_keeps_counts(self):
        class Fake:
            async def complete(self,model,messages):return 'ok',{'total_tokens':1}
        self.cfg.update({'model':{'base_url':'http://localhost:9','name':'mock','hourly_limit':0}})
        service=RuntimeService(self.store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        engine=ChatEngine(self.store,self.cfg,Fake())
        turns=[await engine.submit('desktop','test',identifier()) for _ in range(3)]
        await engine.wait()
        self.assertTrue(all(engine.get(t['id'])['status']=='completed' for t in turns))
        self.assertEqual(self.quotas.current()['used']['project_hourly'],0)
        self.assertEqual(self.quotas.current()['used']['manual_hourly'],3)

    async def test_manual_background_probes_bypass_automatic_limits_but_keep_usage(self):
        class Fake:
            calls = 0
            async def complete(self, model, messages):
                self.calls += 1
                return 'OK', {'total_tokens': 1}
        self.cfg.update({'model': {'base_url': 'http://localhost:9', 'name': 'mock'},
                         'organization': {'reuse_chat': True, 'daily_enabled': False}})
        service = RuntimeService(self.store)
        service.use_persona(service.create_persona(generic_persona())['id'])
        fake = Fake(); chat = ChatEngine(self.store, self.cfg, fake)
        org = OrganizationEngine(self.store, self.cfg, fake, chat)
        self.quotas.update(dict.fromkeys(('project_hourly', 'background_hourly', 'background_daily'), 1))
        for _ in range(4):
            task=org.test_model();self.assertTrue(await org.run_next());self.assertEqual(org.show(task['id'])['status'],'completed')
        self.assertEqual(fake.calls,4)
        self.assertEqual(self.quotas.current()['used']['background_hourly'],0)
        self.assertEqual(self.quotas.current()['used']['manual_hourly'],4)

    async def test_quota_management_requires_admin_and_restores_original_limits(self):
        class Fake:
            async def complete(self, model, messages):
                raise AssertionError('quota management must not call a model')
        server = TestServer(create_app(self.paths, Fake())); await server.start_server()
        self.addAsyncCleanup(server.close)
        before = self.quotas.current()['limits']
        async with ClientSession() as client:
            for action in ('quota.current', 'quota.update', 'quota.acceptance.begin', 'quota.acceptance.end', 'quota.development.begin', 'quota.development.end'):
                async with client.post(server.make_url('/v1/manage'),
                    headers={'Authorization': 'Bearer ' + self.cfg.token('bridge')},
                    json={'action': action, 'args': {'limits': dict.fromkeys(before, 0)} if action == 'quota.update' else {}}) as response:
                    self.assertEqual(response.status, 403)
            self.assertEqual(self.quotas.current()['limits'], before)
            for action in ('quota.acceptance.begin', 'quota.acceptance.end', 'quota.development.begin', 'quota.development.end'):
                async with client.post(server.make_url('/v1/manage'),
                    headers={'Authorization': 'Bearer ' + self.cfg.token()},
                    json={'action': action, 'args': {}}) as response:
                    self.assertEqual(response.status, 200)
                    value = await response.json()
                    self.assertEqual(value['acceptance_active'], action.endswith('begin'))
                    self.assertEqual(value['development_unlimited'], action == 'quota.development.begin')
            self.assertEqual(self.quotas.current()['limits'], before)
