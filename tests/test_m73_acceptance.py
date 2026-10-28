"""Paid driver preflight through loopback with a fake provider only."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestServer
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.models import generic_persona,identifier
from persona_runtime.server import create_app
from persona_runtime.chat import ChatEngine
from persona_runtime.delivery import DeliveryService
from scripts.acceptance_m73 import Acceptance
from tests.test_m73 import Model

class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_calls_resume_and_quota_restore(self):
        with tempfile.TemporaryDirectory(prefix='m73-driver-',dir=RuntimePaths.from_environment().home/'tmp') as d:
            paths=RuntimePaths(Path(d));store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
            cfg.update({'bot_qq':'12345','owner_qq':'67890','model':{'name':'mock','base_url':'http://127.0.0.1:1/v1'},'organization':{'reuse_chat':True,'daily_enabled':False}})
            model=Model();engine=ChatEngine(store,cfg,model);service=engine.service
            original=service.create_persona(generic_persona())['id'];pid=service.create_persona(generic_persona())['id'];service.use_persona(original)
            out=paths.home/'exports/acceptance/m73';out.mkdir(parents=True)
            atomic_json(out/'real-checkpoint.json',{'persona_id':pid,'original_persona_id':original,'calls':{}})
            app=create_app(paths,model)
            async with TestServer(app) as server:
                cfg.update({'service_port':server.port})
                with patch.dict('os.environ',{'PERSONA_RUNTIME_HOME':str(paths.home)}),patch.object(Acceptance,'online'):
                    driver=Acceptance(out);await asyncio.to_thread(driver.desktop)
                    await asyncio.to_thread(Acceptance(out).desktop)
                    self.assertEqual(len(model.calls),3);self.assertEqual(driver.count_calls(),3)
                    service.use_persona(pid)
                    receipt=app['ingress'].accept({'channel':'qq','bot':'12345','sender':'67890','platform_message_id':'offline-q','request_id':'offline-q','text':'人工QQ来源'})
                    await app['engine'].wait();turn=app['engine'].get(receipt['turn_id'])
                    delivery=DeliveryService(store);stream=delivery.by_batch(turn['reply_batch']['id'])
                    for part in stream['parts']:
                        stream=delivery.get(stream['id']);claim=delivery.claim(stream['id'],part['id'],stream['revision'],identifier())
                        delivery.result(stream['id'],part['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='mock-'+part['id'])
                    await asyncio.to_thread(driver.seal,receipt['session_id'],'mock-qq','qq')
                    driver.state['calls']['R04']={'status':'completed','session_id':receipt['session_id']};driver.save()
                    await asyncio.to_thread(driver.organize);await asyncio.to_thread(Acceptance(out).organize)
                    self.assertEqual(len(model.calls),6)
                    with self.assertRaisesRegex(RuntimeError,'already reserved'):driver.reserve('R01','major')
            self.assertEqual(cfg.load()['model']['hourly_limit'],30)
            self.assertEqual(service.show_persona()['id'],original)
