"""Production-path integration, source races and durable local maintenance."""
import asyncio,json,time,unittest
from persona_runtime.recall_runtime import RecallRuntime
from persona_runtime.recall_sources import epoch
from persona_runtime.models import identifier
from tests import test_recall_support as fixture

class Integration(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Recall.setUp
    asyncTearDown=fixture.Recall.asyncTearDown
    seed=fixture.Recall.seed
    memory=fixture.Recall.memory
    new_turn=fixture.Recall.new_turn
    raw_root=fixture.Recall.raw_root

    def enable(self):
        self.runtime=RecallRuntime(self.store)
        self.runtime.update(self.persona['id'],True,0,identifier())
        for _ in range(20):
            self.runtime.maintain(self.persona['id'],pages=3)
            if not self.runtime.status(self.persona['id'])['dirty_jobs']:break

    async def test_real_context_and_feedback_without_test_injection(self):
        mid,source=await self.memory('山岚乌龙茶是我喜欢的饮品。')
        self.enable();captured=[]
        async def complete(model,messages):captured.append(messages);return '那就喝山岚乌龙茶。',{'total_tokens':1}
        self.chat.client.complete=complete
        turn=await self.new_turn('我喜欢什么茶来着，山岚那种？')
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertIn('山岚乌龙茶',str(captured));self.runtime.feedback()
        with self.store.connection() as db:
            meta=json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(turn['id'],)).fetchone()[0])['recall_support']
            self.assertTrue(meta['selected_ids'])
            self.assertGreater(db.execute('SELECT count(*) FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone()[0],0)
            before=db.execute('SELECT sum(alpha) FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone()[0]
        self.runtime.feedback()
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT sum(alpha) FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone()[0],before)

    async def test_policy_is_frozen_at_acceptance(self):
        mid,_=await self.memory('山岚乌龙茶')
        turn=await self.chat.submit('desktop','山岚乌龙茶',identifier())
        self.enable();await self.chat.wait()
        result=self.chat.get(turn['id']);self.assertEqual(result['status'],'completed',result['error_code'])
        with self.store.connection() as db:
            snapshot=json.loads(db.execute('SELECT policy_json FROM recall_runtime_turns WHERE turn_id=?',(turn['id'],)).fetchone()[0])
            self.assertFalse(snapshot['data']['enabled'])

    async def test_withdrawal_during_model_wait_blocks_commit_without_replay(self):
        mid,source=await self.memory('独立资料山岚乌龙茶')
        self.enable();started=asyncio.Event();release=asyncio.Event();calls=[]
        async def complete(model,messages):calls.append(messages);started.set();await release.wait();return '山岚乌龙茶。',{'total_tokens':1}
        self.chat.client.complete=complete
        turn=await self.chat.submit('desktop','独立资料山岚乌龙茶',identifier())
        await asyncio.wait_for(started.wait(),10)
        self.service.retract_memory(mid)
        release.set();await self.chat.wait();result=self.chat.get(turn['id'])
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error_code'],'recall_sources_changed')
        with self.assertRaises(Exception):await self.chat.retry(turn['id'])
        self.assertEqual(len(calls),1)
        with self.store.connection() as db:self.assertFalse(db.execute('SELECT 1 FROM recall_support_events WHERE root_id=?',(turn['id'],)).fetchone())

    async def test_dirty_memory_update_reaches_next_real_request(self):
        mid,source=await self.memory('山岚乌龙茶')
        self.enable()
        revised=self.service.revise_memory(mid,'山岚白茶')
        captured=[]
        async def complete(model,messages):captured.append(messages);return '现在喜欢白茶。',{'total_tokens':1}
        self.chat.client.complete=complete
        turn=await self.new_turn('山岚白茶')
        self.assertEqual(turn['status'],'completed',turn['error_code']);self.assertIn('山岚白茶',str(captured))

    async def test_disabled_policy_before_dispatch_is_not_silently_enabled(self):
        await self.memory('山岚乌龙茶');self.enable()
        current=self.runtime.policy(self.persona['id'])
        self.runtime.update(self.persona['id'],False,current['revision'],identifier())
        turn=await self.new_turn('你好')
        self.assertEqual(turn['status'],'completed',turn['error_code'])

    async def test_cancelling_worker_waits_for_sqlite_maintenance_thread(self):
        import threading
        from unittest.mock import patch
        entered=threading.Event();release=threading.Event();closed=threading.Event()
        store=self.store
        def maintenance(runtime,**options):
            with store.connection(write=True) as db:
                entered.set();release.wait(5)
                db.execute('SELECT 1')
            closed.set();return {'pages':0}
        with patch.object(RecallRuntime,'maintain',maintenance):
            worker=asyncio.create_task(self.org.worker())
            try:
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                worker.cancel();await asyncio.sleep(.02)
                self.assertFalse(worker.done());self.assertFalse(closed.is_set())
                release.set();await asyncio.gather(worker,return_exceptions=True)
                self.assertTrue(closed.is_set())
            finally:
                release.set()
                if not worker.done():worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_time_words_alone_do_not_inject_unrelated_day_plan(self):
        await self.memory('今天晚饭准备做西葫芦。');self.enable();seen=[]
        async def complete(model,messages):seen.extend(messages);return '那就慢慢来。',{'total_tokens':1}
        self.chat.client.complete=complete
        for text in ('先换个话题，我今天一整天都犯困，想把晚上放慢点。','今天累得够呛，晚上想歇着发会儿呆。'):
            turn=await self.new_turn(text)
            self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertNotIn('今天晚饭准备做西葫芦',str(seen))

    async def test_topic_pointer_finds_canonical_preference_after_detour(self):
        mid,_=await self.memory('我喜欢乌龙茶，不喜欢饮料太甜。');self.enable()
        async def complete(model,messages):return '乌龙茶，你喜欢清爽一点的。',{'total_tokens':1}
        self.chat.client.complete=complete
        await self.new_turn('今晚喝点什么茶？')
        await self.new_turn('先换个话题，我想休息一下。')
        turn=await self.new_turn('回到喝茶那件事，我前面说的甜度偏好是什么？')
        with self.store.connection() as db:
            meta=json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(turn['id'],)).fetchone()[0])['recall_support']
            sources=[db.execute('SELECT source_id FROM recall_support_units WHERE id=?',(ident,)).fetchone()[0] for ident in meta['selected_ids']]
        self.assertIn(mid,sources)

    async def test_long_current_input_stays_complete_when_recall_query_is_bounded(self):
        await self.memory('山岚乌龙茶');self.enable()
        self.cfg.update({'model':{'max_input_chars':50000}})
        text='技术问题甲\n'+'A'*17000+'\n末尾问题乙';seen=[]
        async def complete(model,messages):seen.extend(messages);return '人工完整输入确认。',{'total_tokens':1}
        self.chat.client.complete=complete
        turn=await self.new_turn(text)
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertEqual(seen[-1]['content'],text)

    async def test_management_api_uses_persona_id_and_idempotent_policy_update(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        app=create_app(self.paths,self.chat.client)
        async with TestServer(app) as server,ClientSession() as client:
            headers={'Authorization':'Bearer '+self.cfg.token()}
            async def manage(action,args):
                async with client.post(server.make_url('/v1/manage'),headers=headers,json={'action':action,'args':args}) as response:
                    data=await response.json();self.assertEqual(response.status,200,data);return data
            pid=self.persona['id']
            current=await manage('recall.support.policy.current',{'persona_id':pid})
            self.assertFalse(current['data']['enabled'])
            args={'persona_id':pid,'enabled':True,'expected_revision':current['revision'],'operation_id':identifier()}
            changed=await manage('recall.support.policy.update',args)
            self.assertEqual(await manage('recall.support.policy.update',args),changed)
            state=await manage('recall.support.status',{'persona_id':pid})
            self.assertTrue(state['policy']['data']['enabled'])

if __name__=='__main__':unittest.main()
