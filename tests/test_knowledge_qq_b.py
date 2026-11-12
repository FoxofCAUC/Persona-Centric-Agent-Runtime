"""B-path QQ evidence/reuse/final-delivery contracts, without platform sends."""
import asyncio,unittest
from tests import test_knowledge_web_maintenance as bridge
from persona_runtime.models import identifier,RuntimeErrorBase
from persona_runtime.knowledge_service import read
from persona_runtime.delivery import DeliveryService

class QQBTests(unittest.IsolatedAsyncioTestCase):
    setUp=bridge.BridgeTests.setUp
    asyncTearDown=bridge.BridgeTests.asyncTearDown
    put=bridge.BridgeTests.put
    def enable(self):
        self.cfg.update({'bot_qq':'456','owner_qq':'123'})
        bridge.BridgeTests.enable(self)
        p=self.chat.knowledge.current_policy(self.pid);self.chat.knowledge.update_policy(self.pid,p['data']|{'channels':['desktop','qq']},p['revision'],'qq-reader')
        p=self.org.routing.policy(self.pid);self.org.routing.update_policy(self.pid,p['data']|{'variant':'web_only'},p['revision'],'web-only-qq')
    async def ask(self,text):
        row=await self.chat.submit('qq',text,identifier(),persona_id=self.pid,origin={'bot':'456','sender':'123'})
        await asyncio.wait_for(self.chat.wait(),20);return self.chat.get(row['id'])
    async def test_qq_b_question_reference_and_greeting_share_validated_pipeline(self):
        self.enable();first=await self.ask('测试行星的颜色是什么？')
        self.assertEqual(first['status'],'completed',first['error_code']);self.assertEqual(first['context']['knowledge']['routing']['choice'],'web')
        self.assertIn('蓝色',self.main.calls[-1][1][0]['content']);count=len(self.source_calls)
        self.choice='none';self.reuse=True;second=await self.ask('给我出处。')
        self.assertEqual(second['status'],'completed',second['error_code']);self.assertTrue(second['context']['knowledge']['routing']['reuse'])
        self.reuse=False;third=await self.ask('今天先聊到这里。')
        self.assertEqual(third['status'],'completed',third['error_code']);self.assertFalse(third['context']['knowledge']['evidence'])
        self.assertEqual(len(self.source_calls),count)
    async def test_qq_b_withdrawal_after_generation_blocks_delivery(self):
        self.enable();result=await self.ask('测试行星的颜色是什么？');self.assertEqual(result['status'],'completed')
        item=result['context']['knowledge']['evidence'][0]
        with self.store.connection() as db:rev=read(db,'web.invalidations',{})['revision']
        self.org.web.invalidate(self.pid,item['document_id'],'撤回测试资料',rev,'withdraw-before-send')
        service=DeliveryService(self.store);stream=service.by_batch(result['reply_batch']['id']);part=stream['parts'][0]
        claimed=service.claim(stream['id'],part['id'],stream['revision'],'claim-after-withdraw',persona_id=self.pid,
            capabilities=['reply_batch_v2','reply_rhythm_v1','reply_bubbles_v3','reply_display_messages_v1'])
        self.assertFalse(claimed['claimed']);self.assertEqual(claimed['reason'],'knowledge_evidence_changed')
