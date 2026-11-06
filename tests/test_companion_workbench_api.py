"""Artificial loopback checks for accepted-policy cancellation and capabilities."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.character_runtime import CharacterRuntime
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app


class WaitingModel:
    def __init__(self):
        self.entered=asyncio.Event()
        self.calls=[]

    async def complete(self, model, messages):
        self.calls.append((model, messages))
        self.entered.set()
        await asyncio.Event().wait()


class API(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='companion-api-', dir=RuntimePaths.from_environment().home/'tmp')
        self.paths=RuntimePaths(Path(self.tmp.name))
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'name':'artificial','base_url':'http://127.0.0.1:1/v1'},
                            'interaction':{'cancel_queued':False}})
        self.model=WaitingModel()
        self.notifier=patch.object(CharacterRuntime,'notify',return_value=[])
        self.notifier.start()
        self.app=create_app(self.paths,self.model)
        self.engine=self.app['engine'];self.ingress=self.app['ingress']
        self.pid=self.engine.service.create_persona(generic_persona())['id']
        self.engine.service.use_persona(self.pid)
        self.local=patch.object(self.engine.cognitive.characters,'_hybrid',side_effect=SemanticFailure('character_cache_missing'))
        self.local.start()
        self.server=TestServer(self.app);await self.server.start_server()
        self.http=ClientSession()
        self.admin={'Authorization':'Bearer '+self.config.token()}
        self.bridge={'Authorization':'Bearer '+self.config.token('bridge')}

    async def asyncTearDown(self):
        for task in list(self.engine.tasks.values()):task.cancel()
        await self.engine.wait()
        await self.http.close();await self.server.close()
        self.local.stop();self.notifier.stop();self.tmp.cleanup()

    def enable(self):
        old=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop','legacy_per_message',old['id'],identifier(),
            companion_mode='minimal_structural',reply_mode='natural_structured',expression_policy_version=3)

    async def queued(self):
        with patch.object(self.engine,'_schedule'):
            return await self.engine.submit('desktop','人工消息，不访问供应商',identifier(),persona_id=self.pid)

    async def cancel(self,tid,role='admin'):
        async with self.http.post(self.server.make_url('/v1/requests/'+tid+'/cancel'),
                                  json={},headers=getattr(self,role)) as response:
            value=await response.json() if response.content_type=='application/json' else {'error':await response.text()}
            return response.status,value

    async def test_health_advertises_real_controls_without_startup_generation(self):
        async with self.http.get(self.server.make_url('/v1/health'),headers=self.admin) as response:
            value=await response.json()
        self.assertIn('companion_workbench_v1',value['capabilities'])
        self.assertIn('companion_c2_workbench_v1',value['capabilities'])
        self.assertIn('companion_cancel_v1',value['capabilities'])
        self.assertEqual(self.model.calls,[])

    async def test_old_queue_switch_still_controls_ordinary_request(self):
        turn=await self.queued()
        status,value=await self.cancel(turn['id'])
        self.assertEqual((status,value['error']),(400,'queue_cancellation_disabled'))
        self.config.update({'interaction':{'cancel_queued':True}})
        status,value=await self.cancel(turn['id'])
        self.assertEqual(status,200)
        self.assertEqual(value['error_code'],'cancelled_by_user')
        self.assertEqual(self.model.calls,[])

    async def test_candidate_queue_cancels_with_old_switch_off(self):
        self.enable();turn=await self.queued()
        status,value=await self.cancel(turn['id'])
        self.assertEqual(status,200)
        self.assertEqual(value['error_code'],'cancelled_by_user')
        self.assertEqual(self.model.calls,[])

    async def test_running_candidate_cancels_after_current_policy_disabled(self):
        self.enable();turn=await self.queued()
        self.engine._schedule(turn['id'])
        await asyncio.wait_for(self.model.entered.wait(),5)
        old=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop',old['mode'],old['id'],identifier(),companion_mode='disabled')
        status,value=await self.cancel(turn['id'])
        self.assertEqual(status,200)
        self.assertEqual(value['error_code'],'cancelled_by_user')
        self.assertIsNone(value['reply'])
        self.assertFalse(self.engine.locks[self.pid].locked())
        with self.app['store'].connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches WHERE turn_id=?',(turn['id'],)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0],0)

    async def test_forged_one_sided_candidate_cannot_bypass_queue_switch(self):
        self.enable();turn=await self.queued()
        with self.app['store'].connection(write=True) as db:
            model=json.loads(db.execute('SELECT model_snapshot FROM turns WHERE id=?',(turn['id'],)).fetchone()[0])
            model.pop('_companion_profile')
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),turn['id']))
        status,value=await self.cancel(turn['id'])
        self.assertEqual(status,400)
        self.assertEqual(value['error'],'companion_profile_invalid')
        self.assertIsNone(self.engine.get(turn['id'])['cancelled_at'])

    async def test_bridge_cannot_use_admin_cancel(self):
        self.enable();turn=await self.queued()
        status,_=await self.cancel(turn['id'],'bridge')
        self.assertEqual(status,403)
        self.assertIsNone(self.engine.get(turn['id'])['cancelled_at'])
