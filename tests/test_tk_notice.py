"""Opted-in desktop inputs freeze the display boundary; no remote model is used."""
import asyncio
import json
import unittest

from persona_runtime import tk_notice
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_m9r_native_retrieval as fixture


class DisplayGate(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Integration.setUp
    enable=fixture.Integration.enable
    asyncTearDown=fixture.Integration.asyncTearDown
    decision_transport=fixture.Integration.decision_transport
    transport=fixture.Integration.transport

    async def start(self):
        self.enable()
        self.ingress=IngressService(self.chat)
        value=self.ingress.accept({'text':'ID-U1','request_id':identifier(),'channel':'desktop',
            'persona_id':self.persona['id'],'public_task':{'question':'ID-P1'},
            'desktop_presentation':tk_notice.CONTRACT})
        if not value['turn_id']:
            value=self.ingress.action(value['batch_id'],'seal',value['batch_revision'],identifier(),self.persona['id'])
        turn=self.chat.get(value['turn_id'])
        async with asyncio.timeout(14):
            while True:
                pending=self.chat.history_snapshot(turn['session_id']).get('pending_notices')
                if pending:return turn, dict(pending[0],persona_id=turn['persona_id'],session_id=turn['session_id'])
                current=self.chat.get(turn['id'])
                if current['status'] in ('failed','interrupted'):self.fail(current['error_code'])
                await asyncio.sleep(.03)

    async def test_only_matching_display_can_release_search_once(self):
        turn,args=await self.start()
        notice=self.org.processing.search_notice
        self.assertFalse(self.provider.calls)
        self.assertIsNone(self.chat.history(turn['session_id'])[0]['search_notice'])
        for field in ('persona_id','session_id','turn_id','notice_id'):
            with self.subTest(field=field),self.assertRaises(RuntimeErrorBase):
                tk_notice.display(notice,**(args|{field:'ID-OTHER'}),acknowledge=True)
        a=tk_notice.display(notice,**args)
        self.assertEqual(a,tk_notice.display(notice,**args))
        self.assertFalse(self.provider.calls)
        receipt=tk_notice.display(notice,**args,acknowledge=True)
        self.assertEqual(receipt['event_id'],tk_notice.display(notice,**args,acknowledge=True)['event_id'])
        await asyncio.wait_for(self.chat.wait(),12)
        current=self.chat.get(turn['id'])
        self.assertEqual(current['status'],'completed',current['error_code'])
        self.assertEqual(current['search_notice']['text'],a['text'])
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(self.main.notice_calls),1)
        self.assertNotIn(tk_notice.CONTRACT,json.dumps([self.main.notice_calls,self.main.calls,self.provider.calls],
                                                     ensure_ascii=False,default=str))
        self.assertFalse(self.chat.history_snapshot(turn['session_id']).get('pending_notices'))

    async def test_superseding_input_retires_the_unseen_notice(self):
        turn,args=await self.start()
        self.need_web=False
        self.ingress.accept({'channel':'desktop','text':'ID-NEW','request_id':identifier(),'persona_id':turn['persona_id'],
            'session_id':turn['session_id'],'desktop_presentation':tk_notice.CONTRACT})
        with self.assertRaises(RuntimeErrorBase):tk_notice.display(self.org.processing.search_notice,**args,acknowledge=True)
        async with asyncio.timeout(8):
            while self.chat.get(turn['id'])['status']=='queued':await asyncio.sleep(.05)
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertFalse(self.provider.calls)

    async def test_withdrawn_source_cannot_be_previewed_or_publish_on_late_ack(self):
        from persona_runtime.models import LOCAL_USER_ID, timestamp
        turn,args=await self.start()
        self.assertTrue(tk_notice.display(self.org.processing.search_notice,**args)['text'])
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',
                       (turn['user_event_id'],turn['persona_id'],LOCAL_USER_ID,'ID-WITHDRAWN',timestamp()))
        for ack in (False,True):
            with self.assertRaises(ValueError):
                tk_notice.display(self.org.processing.search_notice,**args,acknowledge=ack)
        async with asyncio.timeout(8):
            while self.chat.get(turn['id'])['status']=='queued':await asyncio.sleep(.05)
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertFalse(self.provider.calls)

    async def test_manual_resume_uses_original_pending_receipt_after_service_stop(self):
        turn,args=await self.start()
        self.chat.knowledge.closed.set()
        await asyncio.wait_for(self.chat.wait(),8)
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertFalse(self.provider.calls)
        self.chat.knowledge.closed.clear()
        self.chat.recover()
        await self.chat.retry(turn['id'])
        async with asyncio.timeout(8):
            while not self.chat.history_snapshot(turn['session_id']).get('pending_notices'):
                await asyncio.sleep(.05)
        self.assertTrue(tk_notice.display(self.org.processing.search_notice,**args,acknowledge=True)['published'])
        await asyncio.wait_for(self.chat.wait(),12)
        final=self.chat.get(turn['id'])
        self.assertEqual(final['status'],'completed',final['error_code'])
        self.assertTrue(final['search_notice']['event_id'])
        self.assertEqual(len(self.main.notice_calls),1)
        self.assertEqual(len(self.provider.calls),1)
