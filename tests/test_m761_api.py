"""Local HTTP management, no QQ or external model dispatch."""
import json
from pathlib import Path
import tempfile
import unittest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.models import generic_persona,identifier
from tests.test_m761 import Fake


class API(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home/'tmp')
        self.paths=RuntimePaths(Path(self.tmp.name));cfg=ConfigStore(self.paths);cfg.initialize()
        cfg.update({'character_selector':{'enabled':True,'base_url':'https://example.invalid','name':'fake'}})
        self.fake=Fake();self.app=create_app(self.paths,self.fake)
        service=self.app['engine'].service;self.pid=service.create_persona(generic_persona())['id'];service.use_persona(self.pid)
        self.server=TestServer(self.app);await self.server.start_server();self.http=ClientSession()
        self.admin={'Authorization':'Bearer '+cfg.token()};self.bridge={'Authorization':'Bearer '+cfg.token('bridge')}

    async def asyncTearDown(self):
        await self.http.close();await self.server.close();self.tmp.cleanup()

    async def post(self,action,args,role='admin'):
        async with self.http.post(self.server.make_url('/v1/manage'),json={'action':action,'args':args},headers=getattr(self,role)) as r:
            return r.status,await r.json()

    async def test_bridge_cannot_enumerate_or_compile(self):
        for action in ('disposition.current','disposition.sources','disposition.compile','disposition.inspect'):
            code,_=await self.post(action,{'persona_id':self.pid},'bridge');self.assertEqual(code,403)
        self.assertEqual(self.fake.calls,[])

    async def test_admin_preview_compile_and_health(self):
        code,value=await self.post('disposition.sources',{'persona_id':self.pid});self.assertEqual(code,200)
        source=next(s for s in value['sources'] if s['kind']=='expression')
        self.fake.output='{"schema_version":1,"definitions":[]}'
        code,result=await self.post('disposition.compile',{'persona_id':self.pid,'source_ids':[source['source_id']],'expected_revision':0,'operation_id':identifier()})
        self.assertEqual(code,200);await self.app['disposition_compiler'].task
        code,status=await self.post('disposition.status',{'persona_id':self.pid,'attempt_id':result['attempt_id']})
        self.assertEqual(status[0]['status'],'completed');self.assertNotIn('model_snapshot',status[0])
        with self.app['store'].connection() as db:before=db.execute('SELECT count(*) FROM events').fetchone()[0]
        code,preview=await self.post('disposition.preview',{'persona_id':self.pid,'members':['人工消息']})
        self.assertEqual(code,200);self.assertFalse(preview['persisted'])
        with self.app['store'].connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],before)
        async with self.http.get(self.server.make_url('/v1/health'),headers=self.admin) as r:health=await r.json()
        self.assertEqual(health['schema_version'],19);self.assertIn('disposition_shadow_v1',health['capabilities'])
        self.assertEqual(len(self.fake.calls),1)
