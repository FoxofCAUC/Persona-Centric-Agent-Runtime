"""Managed Jev/native/v8 integration with explicit public projections; no paid API."""
import copy
import json
import unittest

from persona_runtime.chat import ModelError
from persona_runtime.deepseek_native_response import NativeResponse
from persona_runtime.models import identifier
from tests import test_general_retrieval as general_fixture
from tests.test_m9r0_native_contract import response


class Main(general_fixture.Main):
    def __init__(self):
        super().__init__(); self.notice_calls=[]

    async def complete(self,model,messages):
        if '_search_notice_profile' in model:
            self.notice_calls.append((copy.deepcopy(model),copy.deepcopy(messages)))
            from persona_runtime.chat import ModelCompletion
            return ModelCompletion('等我翻翻看，这个我想查清楚再跟你说。',{'total_tokens':1},{'finish_reason':'stop'})
        return await super().complete(model,messages)


class Provider:
    def __init__(self):
        self.calls = []; self.failure = None; self.tools = True; self.tool_failed = False

    async def complete(self, model, messages):
        self.calls.append((copy.deepcopy(model), copy.deepcopy(messages)))
        if '_native_web_profile' not in model: raise AssertionError('legacy planner or organizer called')
        parser = NativeResponse()
        raw = response(content='公开任务资料：先练走位，再换装备。', tools=self.tools)
        if self.tool_failed:
            raw['choices'][0]['message']['server_tool_calls'][0]['calls'][0].update(status='failed', error={'code': 'unavailable'})
        try:
            if self.failure == 'known':
                parser.consume({'error': {'message': 'synthetic'}, 'usage': raw['usage']})
            if self.failure == 'unknown':
                parser.fail('provider_native_unknown')
            parser.consume(raw)
            value = parser.complete()
            value.diagnostics.update(response_headers_received=True, request_headers_sent=True, http_status=200)
            return value
        except ModelError as error:
            error.diagnostics.update(response_headers_received=True, request_headers_sent=True, http_status=200)
            raise


class Integration(unittest.IsolatedAsyncioTestCase):
    setUp = general_fixture.GeneralTests.setUp
    asyncTearDown = general_fixture.GeneralTests.asyncTearDown
    decision_transport = general_fixture.GeneralTests.decision_transport
    transport = general_fixture.GeneralTests.transport

    def enable(self):
        self.cfg.update({'model': {'base_url': 'https://api.deepseek.com/v1', 'name': 'deepseek-flash'}})
        general_fixture.GeneralTests.enable(self)
        self.main=self.chat.client=Main()
        self.provider = self.org.client = Provider()
        reader = self.org.processing.native.policy(self.persona['id'])
        self.org.processing.native.update(self.persona['id'], True, reader['revision'], identifier())

    async def ask(self, text='帮我查公开游戏攻略', *, public=None):
        turn = await self.chat.submit('desktop', text, identifier(), public_task=public)
        import asyncio
        await asyncio.wait_for(self.chat.wait(), 30)
        return self.chat.get(turn['id'])

    async def test_two_stage_uses_one_native_task_then_existing_v8_without_planner_organizer(self):
        self.enable()
        turn = await self.ask(public={'question': '公开游戏攻略'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(len(self.decisions), 1); self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.main.calls), 1); self.assertEqual(self.tools, [])
        self.assertNotIn('_json_output', self.provider.calls[0][0])
        self.assertIn('先练走位', self.main.calls[0][0]['content'])
        self.assertNotIn('synthetic-step', json.dumps(self.main.calls, ensure_ascii=False))
        cost=turn['context']['knowledge']['retrieval']['cost']
        self.assertEqual(cost['tokens_known'],19)
        self.assertEqual(len(self.main.notice_calls),1)
        self.assertEqual(turn['search_notice']['text'],'等我翻翻看，这个我想查清楚再跟你说。')
        self.assertEqual(cost['tool_estimated_cny_known'],'0.015')
        self.assertIsNone(cost['actual_total_amount'])
        with self.store.connection() as db:
            rows = db.execute("SELECT a.id FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchall()
            self.assertEqual(len(rows), 1)
            packet = db.execute('SELECT value FROM knowledge_control WHERE key=?', ('knowledge.native.receipt.' + rows[0][0],)).fetchone()
            self.assertIsNotNone(packet)

    async def test_explicit_composer_search_is_a_scoped_input_to_router(self):
        from persona_runtime.knowledge_native import public_task
        self.enable()
        turn=await self.ask('810 811',public={'question':'811','request_search':True})
        self.assertEqual(turn['status'],'completed')
        self.assertTrue(self.decisions[0][1]['state']['explicit_public_query_requested'])
        self.assertEqual(len(self.provider.calls),1)
        self.assertIn('811',self.provider.calls[0][1][-1]['content'])
        self.assertNotIn('810',self.provider.calls[0][1][-1]['content'])
        self.assertNotIn('request_search',json.dumps(self.provider.calls[0][1]))
        with self.assertRaises(ValueError):public_task({'question':'811','request_search':1})
        with self.assertRaises(ValueError):
            await self.ask('812',public={'question':'811','request_search':True})

    async def test_none_and_cached_followup_do_not_request_more_tools(self):
        self.enable(); await self.ask(public={'question': '公开游戏攻略'})
        self.need_web = False
        turn = await self.ask('就按刚才说的，把步骤展开')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(len(self.provider.calls), 1); self.assertEqual(self.tools, [])
        self.assertIn('先练走位', self.main.calls[-1][0]['content'])

    async def test_missing_public_projection_is_not_replaced_by_full_private_chat(self):
        self.enable(); turn = await self.ask('我的私人经历和敏感细节不要用于搜索')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(self.provider.calls, [])
        self.assertIn('native_public_projection_required', turn['context']['knowledge']['retrieval']['errors'])

    async def test_only_public_projection_enters_native_input(self):
        self.enable()
        await self.ask('这是我的私人细节，请另查公开问题', public={'question': '独立的公开查询'})
        self.assertEqual(len(self.provider.calls), 1)
        wire = json.dumps(self.provider.calls[0][1], ensure_ascii=False)
        self.assertIn('独立的公开查询', wire); self.assertNotIn('私人细节', wire)
        self.assertNotIn('persona', wire); self.assertNotIn('recent_context', wire)

    async def retry_after_primary_failure(self, failure):
        self.enable(); self.provider.failure = failure
        original = self.main.complete
        async def bad(model,messages):
            if '_search_notice_profile' in model:return await original(model,messages)
            raise ModelError('provider_http_400')
        self.main.complete = bad
        failed = await self.ask(public={'question': '公开查询'})
        self.assertEqual(failed['status'], 'failed')
        self.main.complete = original
        await self.chat.retry(failed['id'])
        import asyncio
        await asyncio.wait_for(self.chat.wait(), 30)
        self.assertEqual(self.chat.get(failed['id'])['status'], 'completed')
        self.assertEqual(len(self.provider.calls), 1)
        with self.store.connection() as db:
            row = db.execute("SELECT t.status,t.error_code,a.usage_json FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
        return row

    async def test_unknown_native_does_not_replay_after_main_retry(self):
        row = await self.retry_after_primary_failure('unknown')
        self.assertEqual(row[0], 'blocked_unknown')

    async def test_internal_error_is_known_failed_keeps_usage_and_does_not_replay(self):
        row = await self.retry_after_primary_failure('known')
        self.assertEqual(row[0], 'failed')
        self.assertEqual(json.loads(row[2])['total_tokens'], 17)

    async def test_completed_tool_free_request_gets_only_one_independent_supplement(self):
        self.enable(); self.provider.tools = False
        turn = await self.ask(public={'question': '明确公开查询'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([row[0] for row in self.tools], ['search'])
        self.assertTrue(turn['context']['knowledge']['retrieval']['material_acquired'])

    async def test_failed_tool_is_not_automatically_refetched(self):
        self.enable(); self.provider.tool_failed = True
        turn = await self.ask(public={'question': '公开查询'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(self.tools, []); self.assertEqual(len(self.provider.calls), 1)
        self.assertFalse(turn['context']['knowledge']['retrieval']['material_acquired'])

    async def test_public_task_changes_cannot_reuse_an_ingress_identity(self):
        self.enable(); rid = identifier()
        turn = await self.chat.submit('desktop', '查询', rid, public_task={'question': '甲'})
        same = await self.chat.submit('desktop', '查询', rid, public_task={'question': '甲'})
        self.assertEqual(turn['id'], same['id'])
        with self.assertRaisesRegex(Exception, 'submission_conflict'):
            await self.chat.submit('desktop', '查询', rid, public_task={'question': '乙'})
        import asyncio
        await asyncio.wait_for(self.chat.wait(), 30)

    async def test_native_credential_failure_is_known_not_sent_without_a_retry_loop(self):
        self.enable();calls=[]
        async def unavailable(model,messages):
            calls.append(1)
            raise ModelError('provider_native_credential_unavailable',diagnostics={'request_headers_sent':False})
        self.provider.complete=unavailable
        turn=await self.ask(public={'question':'公开查询'})
        self.assertEqual(turn['status'],'completed',turn['error_code']);self.assertEqual(len(calls),1)
        with self.store.connection() as db:
            row=db.execute("SELECT id,status FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
            self.assertEqual(row[1],'failed')
            contract=self.org.managed.contract(db,row[0])
            self.assertTrue(contract['data']['last_failure']['not_sent'])
        cost=turn['context']['knowledge']['retrieval']['cost']
        self.assertEqual(cost['usage_unknown'],0);self.assertEqual(cost['tool_usage_unknown'],0)

    async def test_explicit_readable_retention_fetches_only_the_requested_link_once(self):
        self.enable()
        turn=await self.ask(public={'question':'读取并保留这个公开页面','intent':'read_link',
            'url':'https://example.org/','retain_page':True})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertEqual([item[0] for item in self.tools],['fetch'])
        self.assertEqual(len(self.provider.calls),1)
        with self.store.connection() as db:
            record=json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',
                ('knowledge.conversation.record.'+turn['id'],)).fetchone()[0])['data']
        self.assertTrue(record['native']['retention']['readable_acquired'])
        self.assertTrue(record['sources'][0]['content'])
        self.assertEqual(record['sources'][0]['material_kind'],'fetched_page_text')

    async def test_natural_public_request_needs_no_extra_form_and_private_prefix_is_omitted(self):
        self.enable()
        turn=await self.ask('私人经历就不说了。帮我查《哈迪斯》盾的新手打法。')
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertEqual(len(self.provider.calls),1)
        wire=json.dumps(self.provider.calls[0][1],ensure_ascii=False)
        self.assertIn('《哈迪斯》盾的新手打法',wire);self.assertNotIn('私人经历',wire)


if __name__ == '__main__':
    unittest.main()
