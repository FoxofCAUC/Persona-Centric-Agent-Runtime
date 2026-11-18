"""Exercise the selected v8 Pro contract through normal admission in an isolated home."""
from pathlib import Path
import tempfile
import unittest

from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class Capture:
    def __init__(self):
        self.requests=[]

    async def complete(self, model, messages):
        wire,_=build_request(model,messages)
        self.requests.append(wire)
        return ModelCompletion('ID-N4-REPLY',None,{'finish_reason':'stop'})


class StyleBaseline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch=tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.paths=RuntimePaths(Path(scratch.name))
        self.config=ConfigStore(self.paths)
        self.config.initialize()

    def test_existing_model_choice_survives_reopening(self):
        self.config.update({'model':{'name':'deepseek-flash'}})
        self.assertEqual(ConfigStore(self.paths).initialize()['model']['name'],'deepseek-flash')

    async def test_new_persona_uses_one_plain_pro_reply_and_its_own_spoken_history(self):
        self.config.update({'model':{'base_url':'https://api.deepseek.com'}})
        store=Store(self.paths)
        store.initialize()
        service=RuntimeService(store)
        pid=service.create_persona(generic_persona())['id']
        service.use_persona(pid)
        capture=Capture()
        engine=ChatEngine(store,self.config,capture)
        self.addCleanup(engine.knowledge.close)
        session=None
        for text in ('ID-N4-1','ID-N4-2'):
            turn=await engine.submit('desktop',text,identifier(),session_id=session)
            await engine.wait()
            result=engine.get(turn['id'])
            self.assertEqual(result['status'],'completed',result.get('error_code'))
            self.assertEqual(result['reply'],'ID-N4-REPLY')
            self.assertEqual(result['reply_batch']['mode'],'context_full')
            self.assertEqual([s['text'] for s in result['reply_batch']['segments']],['ID-N4-REPLY'])
            self.assertNotIn('expression',result)
            self.assertNotIn('dialogue_generation',result)
            session=result['session_id']
        self.assertEqual(len(capture.requests),2)
        for wire in capture.requests:
            self.assertEqual({k:v for k,v in wire.items() if k!='messages'},{
                'model':'deepseek-v4-pro','stream':True,'stream_options':{'include_usage':True},'thinking':{'type':'disabled'}})
        self.assertIn({'role':'assistant','content':'ID-N4-REPLY'},capture.requests[1]['messages'])
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_main_receipts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM dialogue_generation_stages').fetchone()[0],0)


class PrivateNativeAccess(unittest.TestCase):
    def test_only_current_character_and_matching_route_can_be_granted(self):
        from unittest.mock import Mock
        from persona_runtime.desktop_knowledge import KnowledgeUI
        panel=KnowledgeUI()
        panel.persona_id='ID-N4-P'
        panel.view_epoch=3
        panel.knowledge_loaded_key=(panel.persona_id,2)
        panel.knowledge_data={'native_authorization':{'error_code':'private_cloud_provider_not_authorized',
                                                    'expected_provider':'ID-N4-FLASH'}}
        panel.client=Mock()
        panel.run_task=lambda label,resources,work,done:work(None)
        panel.authorize_native_reader()
        panel.client.manage.assert_not_called()
        panel.knowledge_loaded_key=panel.knowledge_key()
        panel.authorize_native_reader()
        action,=panel.client.manage.call_args.args
        args=panel.client.manage.call_args.kwargs
        self.assertEqual((action,args['persona_id'],args['expected_provider']),
                         ('knowledge.native.authorize','ID-N4-P','ID-N4-FLASH'))
        self.assertTrue(args['operation_id'])
        panel.knowledge_data={'native_authorization':{'authorized':True,'expected_provider':'ID-N4-FLASH'}}
        panel.authorize_native_reader()
        self.assertEqual(panel.client.manage.call_count,1)
