"""Public offline scenarios for an actual persona utterance before deep search."""
import asyncio
import json
import unittest

from persona_runtime.chat import ModelError
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_m9r_native_retrieval as native_fixture


class SearchNoticeTests(unittest.IsolatedAsyncioTestCase):
    setUp=native_fixture.Integration.setUp
    asyncTearDown=native_fixture.Integration.asyncTearDown
    enable=native_fixture.Integration.enable
    ask=native_fixture.Integration.ask
    decision_transport=native_fixture.Integration.decision_transport
    transport=native_fixture.Integration.transport

    async def test_notice_is_visible_and_saved_before_native_search_starts(self):
        self.enable();started=asyncio.Event();release=asyncio.Event()
        original=self.provider.complete
        async def native(model,messages):
            started.set();await release.wait()
            return await original(model,messages)
        self.provider.complete=native
        turn=await self.chat.submit('desktop','帮我查公开游戏攻略',identifier(),public_task={'question':'公开游戏攻略'})
        try:
            await asyncio.wait_for(started.wait(),8)
            waiting=self.chat.get(turn['id'])
            self.assertEqual(waiting['status'],'queued');self.assertIsNone(waiting['reply'])
            spoken=waiting['search_notice']
            self.assertEqual(spoken['text'],'等我翻翻看，这个我想查清楚再跟你说。')
            from types import SimpleNamespace
            from unittest.mock import Mock
            from persona_runtime.desktop import Workbench
            transcript=Mock();transcript.yview.return_value=(0.0,1.0)
            window=SimpleNamespace(transcript=transcript,turns=[waiting],chat_batches=[],
                persona_id=waiting['persona_id'],session_id=waiting['session_id'])
            Workbench.render_chat(window)
            displayed=''.join(call.args[1] for call in transcript.insert.call_args_list)
            self.assertIn('人格：'+spoken['text'],displayed)
            self.assertNotIn('主回复用量',displayed)
            self.assertEqual(self.chat.history_snapshot(turn['session_id'])['turns'][0]['search_notice'],spoken)
            with self.store.connection() as db:
                published=db.execute("SELECT at_epoch FROM request_activity WHERE turn_id=? AND event='search_notice_published'",(turn['id'],)).fetchone()[0]
                sent=db.execute("SELECT a.started_epoch FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()[0]
                self.assertLessEqual(published,sent)
        finally:release.set()
        await asyncio.wait_for(self.chat.wait(),8)
        final=self.chat.get(turn['id'])
        self.assertEqual(final['status'],'completed',final['error_code'])
        self.assertEqual(final['search_notice'],spoken)
        # The completed-search assembly retains the visible notice in history and supplies the returned notes to the writer.
        with self.store.connection() as db:
            raw=db.execute("SELECT value FROM knowledge_control WHERE key=?",('knowledge.conversation.record.'+turn['id'],)).fetchone()[0]
        notes=json.loads(raw)['data']['organized']['notes']
        self.assertTrue(notes)
        self.assertIn(notes,self.main.calls[0][0]['content'])
        self.assertNotIn(spoken['text'],self.main.calls[0][0]['content'])
        self.assertEqual(len(self.main.notice_calls),1)
        from persona_runtime.provider_compat import build_request
        body,_=build_request(*self.provider.calls[0])
        self.assertEqual(body['thinking'],{'type':'enabled'});self.assertEqual(body['reasoning_effort'],'high')
        self.assertIn({'type':'deepseek_web_search'},body['tools'])
        self.assertGreaterEqual(body['max_tokens'],8192)
        self.assertNotIn(spoken['text'],json.dumps(body,ensure_ascii=False))

    async def test_none_and_disabled_web_do_not_generate_a_search_notice(self):
        self.enable();self.need_web=False
        quiet=await self.ask('今天随便聊聊')
        self.assertIsNone(quiet['search_notice']);self.assertFalse(self.main.notice_calls)
        pid=self.persona['id'];reader=self.chat.knowledge.current_policy(pid)
        processing=self.org.processing.policy(pid);web=self.org.web.policy(pid)
        self.org.processing.enable_for_persona(pid,False,reader['revision'],processing['revision'],web['revision'],identifier())
        self.need_web=True
        off=await self.ask(public={'question':'公开查询'})
        self.assertEqual(off['status'],'completed');self.assertIsNone(off['search_notice'])
        self.assertFalse(self.main.notice_calls);self.assertFalse(self.provider.calls)

    async def test_missing_public_projection_does_not_claim_to_be_searching(self):
        self.enable()
        turn=await self.ask('这些私人敏感经历不要用于搜索')
        self.assertIsNone(turn['search_notice']);self.assertFalse(self.main.notice_calls)
        self.assertFalse(self.provider.calls)

    async def test_v8_voice_and_persona_remain_in_the_short_utterance_context(self):
        self.enable()
        turn=await self.chat.submit('desktop','帮我查公开游戏攻略',identifier(),public_task={'question':'公开游戏攻略'})
        from persona_runtime.dialogue_voice import freeze,DEMONSTRATION_VERSION,CONVERSATION_V8
        with self.store.connection(write=True) as db:
            root=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
            model=json.loads(root['model_snapshot']);model.pop('_companion_profile',None);model.pop('_generation_profile',None)
            model['_dialogue_profile']=freeze('public-notice-v8',['公开问题'],version=DEMONSTRATION_VERSION)
            policy=json.loads(root['context_policy_snapshot']);policy.pop('companion',None)
            db.execute("UPDATE turns SET model_snapshot=?,context_policy_snapshot=?,reply_mode_snapshot='context_full' WHERE id=?",(json.dumps(model),json.dumps(policy),turn['id']))
        await asyncio.wait_for(self.chat.wait(),10)
        final=self.chat.get(turn['id']);self.assertEqual(final['status'],'completed',final['error_code'])
        model,messages=self.main.notice_calls[0]
        self.assertEqual(model['_dialogue_profile']['version'],DEMONSTRATION_VERSION)
        self.assertIn(CONVERSATION_V8,messages[0]['content'])
        self.assertIn(self.persona['spec']['name'],messages[0]['content'])
        self.assertNotIn('_json_output',model)
        self.assertNotIn('稍等，我查一下',messages[0]['content'])
        self.assertIn('让对方知道你准备去查',messages[0]['content'])
        self.assertIn('让对方知道你准备去查',messages[-1]['content'])
        self.assertNotIn('搜索尚未开始',messages[0]['content'])
        self.assertNotIn('目前还没有检索结果',messages[-1]['content'])
        self.assertIn('帮我查公开游戏攻略',messages[-1]['content'])
        from persona_runtime.provider_compat import build_request
        body,_=build_request(model,messages)
        # Check the serialized outgoing request and final persona context, not
        # just the declaration of the short-message helper.
        sent=json.dumps(body,ensure_ascii=False)
        final_context=json.dumps(self.main.calls[-1],ensure_ascii=False)
        for phrase in ('台词不固定','不固定台词','固定句数','统一顺序','不要求必须给',
                '初步判断不是必须','建议不是必做','可以先给一点此刻的想法或建议'):
            self.assertNotIn(phrase,sent);self.assertNotIn(phrase,final_context)

    async def test_cancel_retains_spoken_notice_and_never_sends_it_twice_on_retry(self):
        self.enable();started=asyncio.Event();calls=[]
        async def blocked(model,messages):
            calls.append(1);started.set();await asyncio.Event().wait()
        self.provider.complete=blocked
        turn=await self.chat.submit('desktop','帮我查公开游戏攻略',identifier(),public_task={'question':'公开游戏攻略'})
        await asyncio.wait_for(started.wait(),8)
        spoken=self.chat.get(turn['id'])['search_notice']
        await self.chat.cancel(turn['id']);await asyncio.wait_for(self.chat.wait(),8)
        self.assertEqual(self.chat.get(turn['id'])['search_notice'],spoken)
        await self.chat.retry(turn['id']);await asyncio.wait_for(self.chat.wait(),8)
        self.assertEqual(len(calls),1);self.assertEqual(len(self.main.notice_calls),1)
        self.assertEqual(self.chat.get(turn['id'])['search_notice'],spoken)

    async def test_failed_notice_has_no_canned_substitute_and_search_can_continue(self):
        self.enable();original=self.main.complete;calls=[]
        async def fail_notice(model,messages):
            if '_search_notice_profile' in model:
                calls.append(1)
                raise ModelError('provider_http_400',usage={'total_tokens':7},diagnostics={
                    'request_headers_sent':True,'response_headers_received':True,'provider_terminal_error':True,'http_status':400})
            return await original(model,messages)
        self.main.complete=fail_notice
        final=await self.ask(public={'question':'公开查询'})
        self.assertEqual(final['status'],'completed',final['error_code']);self.assertIsNone(final['search_notice'])
        self.assertEqual(len(calls),1);self.assertEqual(len(self.provider.calls),1)
        self.assertIn('provider_http_400',final['context']['knowledge']['retrieval']['errors'])
        self.assertEqual(final['context']['knowledge']['retrieval']['cost']['usage_unknown'],0)

    async def test_received_notice_recovers_local_publication_before_search_without_regeneration(self):
        self.enable();original=self.org.processing.search_notice.publish
        def fail(*args):raise RuntimeErrorBase('local_publication_fixture')
        self.org.processing.search_notice.publish=fail
        first=await self.ask(public={'question':'公开查询'})
        self.assertEqual(first['error_code'],'search_notice_publication_pending')
        self.assertEqual(len(self.main.notice_calls),1);self.assertFalse(self.provider.calls)
        self.org.processing.search_notice.publish=original
        await self.chat.retry(first['id']);await asyncio.wait_for(self.chat.wait(),10)
        final=self.chat.get(first['id']);self.assertEqual(final['status'],'completed',final['error_code'])
        self.assertIsNotNone(final['search_notice'])
        self.assertEqual(len(self.main.notice_calls),1);self.assertEqual(len(self.provider.calls),1)

    async def test_unknown_notice_is_not_replayed_and_its_cost_survives_primary_retry(self):
        self.enable();original=self.main.complete;notice_calls=[];primary_calls=[]
        async def fail_first(model,messages):
            if '_search_notice_profile' in model:
                notice_calls.append(1)
                raise ModelError('provider_effect_unknown',diagnostics={'request_headers_sent':True})
            primary_calls.append(1)
            if len(primary_calls)==1:
                raise ModelError('provider_http_400',usage={'total_tokens':1},diagnostics={
                    'request_headers_sent':True,'response_headers_received':True,'http_status':400,'provider_terminal_error':True})
            return await original(model,messages)
        self.main.complete=fail_first
        first=await self.ask(public={'question':'公开查询'})
        self.assertEqual(first['status'],'failed')
        await self.chat.retry(first['id']);await asyncio.wait_for(self.chat.wait(),10)
        final=self.chat.get(first['id']);self.assertEqual(final['status'],'completed',final['error_code'])
        self.assertEqual(len(notice_calls),1);self.assertEqual(len(self.provider.calls),1)
        self.assertIsNone(final['search_notice'])
        cost=final['context']['knowledge']['retrieval']['cost']
        self.assertEqual(cost['usage_unknown'],1);self.assertEqual(cost['model_attempts'],3)

    async def test_saved_notice_reuse_keeps_its_known_usage_in_retrieval_total(self):
        self.enable();original=self.main.complete;primary_calls=[]
        async def fail_primary(model,messages):
            if '_search_notice_profile' not in model:
                primary_calls.append(1)
                if len(primary_calls)==1:
                    raise ModelError('provider_http_400',diagnostics={
                        'request_headers_sent':True,'response_headers_received':True,'http_status':400,'provider_terminal_error':True})
            return await original(model,messages)
        self.main.complete=fail_primary
        first=await self.ask(public={'question':'公开查询'})
        self.assertEqual(first['status'],'failed')
        await self.chat.retry(first['id']);await asyncio.wait_for(self.chat.wait(),10)
        final=self.chat.get(first['id']);self.assertEqual(final['status'],'completed',final['error_code'])
        self.assertEqual(len(self.main.notice_calls),1);self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(final['context']['knowledge']['retrieval']['cost']['tokens_known'],19)

    async def test_notice_is_part_of_the_same_archive_and_next_turn_context(self):
        self.enable();first=await self.ask(public={'question':'公开查询'})
        from persona_runtime.archive import read_exact,scope
        selected=scope(self.persona['id'],'desktop')
        with self.store.connection() as db:
            group=read_exact(db,selected,{'kind':'turn','id':first['id']})
            self.assertEqual([row['kind'] for row in group['replies']],['search_notice','generated_reply'])
            self.assertEqual(read_exact(db,selected,{'kind':'event','id':first['search_notice']['event_id']})['status'],'found')
            self.assertEqual(read_exact(db,scope(identifier(),'desktop'),{'kind':'event','id':first['search_notice']['event_id']})['status'],'not_found')
        self.need_web=False
        second=await self.ask('就按刚才的资料接着说')
        self.assertEqual(second['status'],'completed',second['error_code'])
        self.assertIsNone(second['search_notice']);self.assertEqual(len(self.main.notice_calls),1)
        self.assertIn(first['search_notice']['text'],json.dumps(self.main.calls[-1],ensure_ascii=False))

    def test_link_and_ordinary_inputs_use_deep_search_for_new_tasks_only(self):
        from persona_runtime.knowledge_native import request_model,RETRIEVAL_PROFILE
        from persona_runtime.provider_compat import build_request
        model={'base_url':'https://api.deepseek.com/v1','name':'deepseek-flash','max_tokens':2048}
        inputs={'public_task':{'question':'读取公开页面','intent':'read_link','url':'https://example.org/','depth':'ordinary'},'retrieval_profile':RETRIEVAL_PROFILE}
        body,_=build_request(request_model(model,inputs),[{'role':'user','content':'公开页面'}])
        self.assertEqual(body['thinking'],{'type':'enabled'});self.assertEqual(body['reasoning_effort'],'high')
        self.assertEqual(body['tools'],[{'type':'deepseek_web_search'},{'type':'deepseek_read_url'}])
        del inputs['retrieval_profile']
        historical,_=build_request(request_model(model,inputs),[{'role':'user','content':'公开页面'}])
        self.assertEqual(historical['thinking'],{'type':'disabled'})
        inputs['retrieval_profile']='unrecognized'
        with self.assertRaisesRegex(ValueError,'native_retrieval_profile_invalid'):request_model(model,inputs)


if __name__=='__main__':unittest.main()
