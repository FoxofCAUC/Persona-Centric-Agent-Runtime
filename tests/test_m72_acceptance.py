"""Run the paid-acceptance driver through a real local API with mock models first."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestServer
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.models import generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_turn
from tests.test_m4 import OrganizerModel
from scripts.acceptance_m72 import Acceptance


class MockProvider:
    def __init__(self):self.major=0;self.organizer=OrganizerModel()
    async def complete(self,model,messages):
        if messages[0]['content'].startswith('整理所给对话'):return await self.organizer.complete(model,messages)
        self.major+=1
        if model.get('_reply_mode')=='natural_structured':
            from persona_runtime.reply_batch import expression_version
            return json.dumps({'schema_version':3 if expression_version(model.get('_expression_snapshot'))==3 else 2,'behavior_intent':'respond','messages':[{'content':'离线模拟回复','content_kind':'chat','cancel_if_new_input':False}]},ensure_ascii=False),{'total_tokens':1}
        return '离线模拟回复',{'total_tokens':1}


class AcceptanceDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_budget_restore_resume_and_manifests_with_mock_provider(self):
        with tempfile.TemporaryDirectory(prefix='m72-driver-',dir=RuntimePaths.from_environment().home/'tmp') as d:
            paths=RuntimePaths(Path(d));store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
            cfg.update({'model':{'name':'mock','base_url':'http://127.0.0.1:1/v1'},'organization':{'reuse_chat':True,'daily_enabled':False}})
            model=MockProvider();engine=ChatEngine(store,cfg,model);service=engine.service
            pids=[service.create_persona(generic_persona())['id'] for _ in range(2)];service.use_persona(pids[0]);sid=engine.session()['session_id']
            with store.connection(write=True) as db:seed_turn(db,pids[0],sid,'m72-real-outside',['人工来源一','来源二','来源三'])
            out=paths.home/'exports/acceptance/driver';out.mkdir(parents=True)
            atomic_json(out/'real-checkpoint.json',{'personas':pids,'long_session':sid,'calls':{}})
            atomic_json(out/'formal-upgrade.json',{'original_persona_id':pids[0]})
            async with TestServer(create_app(paths,model)) as server:
                cfg.update({'service_port':server.port})
                with patch.dict('os.environ',{'PERSONA_RUNTIME_HOME':str(paths.home)}),patch.object(Acceptance,'online'):
                    driver=Acceptance(out);await asyncio.to_thread(driver.desktop)
                    self.assertEqual(model.major,7);self.assertEqual(len(model.organizer.calls),1)
                    await asyncio.to_thread(Acceptance(out).desktop)
                    self.assertEqual(model.major,7);self.assertEqual(len(model.organizer.calls),1)
            self.assertEqual(cfg.load()['model']['hourly_limit'],30)
            with store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM context_manifests').fetchone()[0],8)
