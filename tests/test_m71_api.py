"""M7.1 administrator boundary and non-blocking service integration."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.server import create_app
from persona_runtime.archive_corpus import capture
from tests.m71_fixtures import seed_fixture


class SearchAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='m71-api-',dir=RuntimePaths.from_environment().home/'tmp')
        self.addCleanup(self.temp.cleanup);self.paths=RuntimePaths(Path(self.temp.name))
        self.store=Store(self.paths);self.store.initialize();self.pid,_,_=seed_fixture(self.store)
        self.app=create_app(self.paths);self.server=TestServer(self.app);await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.client=ClientSession();self.addAsyncCleanup(self.client.close)

    async def manage(self,action,args,role='admin'):
        return await self.client.post(self.server.make_url('/v1/manage'),json={'action':action,'args':args},
            headers={'Authorization':'Bearer '+self.app['config'].token(role)})

    async def test_all_search_actions_require_admin_and_create_no_events(self):
        args={'persona_id':self.pid,'channel':'desktop'}
        actions={'archive.search':args|{'query':'青禾书店'},'archive.search-status':args,
                 'archive.search-rebuild':args|{'cache_instance_id':None,'expected_generation':0,'operation_id':'admin-rebuild'},
                 'archive.working-set-preview':args|{'window':'recent_50'}}
        with self.store.connection() as db:before=list(db.iterdump())
        # Rebuild first, before a query creates a cache instance.
        for action in ('archive.search-rebuild','archive.search','archive.search-status','archive.working-set-preview'):
            denied=await self.manage(action,actions[action],'bridge');self.assertEqual(denied.status,403)
            response=await self.manage(action,actions[action]);self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['status'],'ok')
        with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))

    async def test_long_search_does_not_block_health_and_invalid_fields_are_safe(self):
        args={'persona_id':self.pid,'channel':'desktop','query':'青禾书店'}
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow(*values):
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError('Search blocked the event loop or health request')
                return capture(*values)
            finally:
                finished.set()
        with patch('persona_runtime.archive_search.capture',side_effect=slow):
            task=asyncio.create_task(self.manage('archive.search',args))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                # Prove health completes while search is still held, independent
                # of desktop/CI scheduling jitter in a 150 ms wall-clock cutoff.
                self.assertFalse(finished.is_set())
                response=await asyncio.wait_for(self.client.get(self.server.make_url('/v1/health'),headers={'Authorization':'Bearer '+self.app['config'].token()}), 3)
                self.assertEqual(response.status,200)
                self.assertFalse(finished.is_set())
                self.assertIn('archive_search_v1',(await response.json())['capabilities'])
            finally:
                release.set()
                searched=await task
            self.assertEqual((await searched.json())['status'],'ok')
        invalid=await self.manage('archive.search',args|{'audit':True})
        self.assertEqual((await invalid.json())['status'],'invalid_query')
