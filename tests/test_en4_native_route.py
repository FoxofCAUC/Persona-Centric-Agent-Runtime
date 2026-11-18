"""Verify frozen native routes with isolated instances and local provider fixtures."""
import json
import unittest
from unittest.mock import patch

from persona_runtime.deepseek_native_contract import build_request
from persona_runtime.knowledge_native import RETRIEVAL_PROFILE, request_model
from persona_runtime.knowledge_native_route import flash_model, for_new_work, PERMISSION, ROUTE
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.private_personas import PrivatePersonas, provider_identity
from tests import test_m9r_native_retrieval as native


class NativePro(unittest.IsolatedAsyncioTestCase):
    setUp=native.Integration.setUp
    asyncTearDown=native.Integration.asyncTearDown
    decision_transport=native.Integration.decision_transport
    transport=native.Integration.transport
    ask=native.Integration.ask

    def enable_pro(self):
        native.Integration.enable(self)
        self.cfg.update({'model':{'name':'deepseek-v4-pro'}})
        self.pid=self.persona['id']

    async def test_pro_notice_flash_native_then_pro_speech(self):
        self.enable_pro()
        sequence=[]
        speech=self.main.complete
        web=self.provider.complete
        async def capture_speech(model,messages):
            sequence.append(('notice' if '_search_notice_profile' in model else 'speech', model['name']))
            return await speech(model,messages)
        async def capture_web(model,messages):
            sequence.append(('native',model['name']))
            with self.store.connection() as db:
                notice=db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.search_notice.turn.%'").fetchone()
                self.assertIsNotNone(notice)
            return await web(model,messages)
        self.main.complete=capture_speech
        self.provider.complete=capture_web
        turn=await self.ask('ID-USER',public={'question':'ID-PUBLIC'})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertEqual(sequence,[('notice','deepseek-v4-pro'),('native','deepseek-flash'),('speech','deepseek-v4-pro')])
        self.assertEqual(len(self.decisions),1)
        model,messages=self.provider.calls[0]
        self.assertNotIn('ID-USER',str(messages))
        self.assertIn('ID-PUBLIC',str(messages))
        wire,_=build_request(model,messages)
        self.assertEqual(wire['thinking'],{'type':'enabled'})
        self.assertEqual(wire['reasoning_effort'],'high')
        self.assertIn('deepseek_web_search', str(wire['tools']))
        with self.store.connection() as db:
            row=db.execute("SELECT model_snapshot,payload_json FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
            self.assertEqual(json.loads(row['model_snapshot']),model)
            self.assertEqual(json.loads(row['payload_json'])['knowledge_processing']['native_model_route'],ROUTE)
            accepted=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
            self.assertEqual(for_new_work(db,accepted,'conversation_native'),ROUTE)
            self.assertIsNone(for_new_work(db,accepted|{'channel':'qq'},'conversation_native'))
            self.assertIsNone(for_new_work(db,accepted|{'reply_mode_snapshot':'legacy_full'},'conversation_native'))
            with patch('persona_runtime.dialogue_generation.enabled',return_value=True):
                self.assertIsNone(for_new_work(db,accepted,'conversation_native'))
            with patch('persona_runtime.expression_runtime.enabled',return_value=True):
                self.assertIsNone(for_new_work(db,accepted,'conversation_native'))

    async def test_private_native_consent_is_independent_and_reusable_for_matching_flash_only(self):
        self.enable_pro()
        private=PrivatePersonas(self.paths)
        processor=self.org.processing.native
        with patch.object(PrivatePersonas,'record_for_persona',return_value={'id':'ID-PRIVATE'}):
            model=self.cfg.load()['model']
            private.authorize_main(self.store,self.pid,provider_identity(model),identifier())
            self.org.routing.authorize(self.pid,provider_identity(self.org.routing.model()),identifier())
            before=processor.authorization(self.pid)
            self.assertFalse(before['authorized'])
            self.assertEqual(before['model'],'deepseek-flash')
            turn=await self.ask('ID-PRIVATE-1',public={'question':'ID-PUBLIC-1'})
            self.assertEqual(turn['status'],'completed',turn['error_code'])
            self.assertFalse(self.provider.calls)
            self.assertIn('private_cloud_provider_not_authorized',turn['context']['knowledge']['retrieval']['errors'])
            with self.assertRaisesRegex(RuntimeErrorBase,'private_provider_changed'):
                processor.authorize(self.pid,provider_identity(model),identifier())
            grant=processor.authorize(self.pid,before['expected_provider'],identifier())
            self.assertEqual(grant['kind'],PERMISSION)
            self.assertTrue(processor.authorization(self.pid)['authorized'])
            next_turn=await self.ask('ID-PRIVATE-2',public={'question':'ID-PUBLIC-2'})
            self.assertEqual(next_turn['status'],'completed',next_turn['error_code'])
            self.assertEqual(len(self.provider.calls),1)

    async def test_unknown_native_does_not_duplicate_after_restart_or_main_retry(self):
        self.enable_pro()
        self.provider.failure='unknown'
        original=self.main.complete
        async def fail_main(model,messages):
            if '_search_notice_profile' in model:return await original(model,messages)
            from persona_runtime.chat import ModelError
            raise ModelError('provider_http_400')
        self.main.complete=fail_main
        turn=await self.ask('ID-RETRY',public={'question':'ID-PUBLIC'})
        self.assertEqual(turn['status'],'failed')
        self.assertEqual(len(self.provider.calls),1)
        self.main.complete=original
        self.chat.recover()
        await self.chat.retry(turn['id'])
        await self.chat.wait()
        self.assertEqual(self.chat.get(turn['id'])['status'],'completed')
        self.assertEqual(len(self.decisions),1)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(self.main.notice_calls),1)

    async def test_native_privacy_checks_read_the_current_transaction(self):
        self.enable_pro()
        from persona_runtime.private_personas import guard_cloud_dispatch
        with self.store.connection() as db, patch('sqlite3.connect',side_effect=AssertionError('nested database open')):
            self.assertFalse(self.org.processing.native.protected(db,self.pid))
            model=flash_model(self.cfg.load()['model'])
            guard_cloud_dispatch(self.store,self.pid,PERMISSION,model,db=db)

    def test_strict_endpoint_and_legacy_contract(self):
        config={'name':'deepseek-v4-pro','base_url':'https://third-party.invalid','max_tokens':512}
        with self.assertRaisesRegex(RuntimeErrorBase,'native_web_official_flash_required'):flash_model(config)
        config['base_url']='https://api.deepseek.com'
        inputs={'public_task':{'question':'ID-PUBLIC'},'retrieval_profile':RETRIEVAL_PROFILE}
        with self.assertRaisesRegex(ValueError,'native_web_official_flash_required'):
            request_model(config,inputs)
        model=request_model(config|{'name':'deepseek-flash'},inputs)
        self.assertEqual((model['name'],model['_native_web_profile']['mode']),('deepseek-flash','deep'))
