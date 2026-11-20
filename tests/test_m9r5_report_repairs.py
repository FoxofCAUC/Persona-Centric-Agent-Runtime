"""Regression coverage from the supplied isolated rc1 verification report; no external calls."""
from datetime import datetime,timedelta,timezone
import json
import unittest

from persona_runtime.knowledge_native import public_query_span,NativeRetrieval,public_task
from tests import test_m9r_native_retrieval as fixture


class QueryIntent(unittest.TestCase):
    def test_references_to_existing_search_do_not_become_new_query_requests(self):
        for text in ('按你查到的降温幅度，我明天穿一件薄外套够不够？',
                '刚才搜索到的做法，给我解释一下。','你查过的那个攻略，接着说。','用查完的资料接着讲。'):
            self.assertIsNone(public_query_span(text),text)
        for text in ('帮我查杭州明天的天气。','查点普通做法。','按刚才的结果，再查一下别的方案。'):
            self.assertIsNotNone(public_query_span(text),text)

    def test_old_public_packet_text_is_stable_and_new_date_is_explicit(self):
        task=public_task({'question':'杭州今天下午到明天天气'})
        base={'knowledge_processing':{'input':{'public_task':task}}}
        self.assertEqual(NativeRetrieval.messages(base)[-1]['content'],task['question'])
        base['knowledge_processing']['input'].update(today='2026-11-15',public_context='先前的公开摘要')
        rendered=NativeRetrieval.messages(base)[-1]['content']
        self.assertIn('2026-11-15',rendered);self.assertIn(task['question'],rendered)
        self.assertIn('先前的公开摘要',rendered)


class ReportRepairs(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Integration.setUp
    asyncTearDown=fixture.Integration.asyncTearDown
    enable=fixture.Integration.enable
    ask=fixture.Integration.ask
    decision_transport=fixture.Integration.decision_transport
    transport=fixture.Integration.transport

    async def test_weather_followup_keeps_cached_material_and_clears_false_explicit_flag(self):
        self.enable();first=await self.ask(public={'question':'杭州今天下午到明天天气 降雨 气温'})
        self.need_web=False
        follow=await self.ask('按你查到的降温幅度，我明天穿一件薄外套够不够？')
        self.assertEqual(follow['status'],'completed',follow['error_code'])
        state=self.decisions[-1][1]['state']
        self.assertIs(state['explicit_public_query_requested'],False)
        self.assertIs(state['cached_notes_available'],True)
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.main.notice_calls),1)

    async def test_public_authorization_is_not_itself_a_search_intent_vote(self):
        self.enable();self.need_web=False
        turn=await self.ask('按你查到的资料继续说。',public={'question':'允许用作查询的公开天气主题'})
        self.assertEqual(turn['status'],'completed')
        state=self.decisions[0][1]['state']
        self.assertIs(state['public_task_available'],True);self.assertIs(state['explicit_public_query_requested'],False)
        self.assertFalse(self.provider.calls)

    async def test_completed_retrieval_supplies_date_and_answer_stage_to_the_persona(self):
        self.enable();turn=await self.ask(public={'question':'公开天气主题'})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        today=datetime.fromisoformat(turn['created_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        self.assertIn(today,self.provider.calls[0][1][-1]['content'])
        self.assertIn('资料已经返回',self.main.calls[-1][0]['content'])
        self.assertIn('先练走位',self.main.calls[-1][0]['content'])
        self.assertNotIn('资料已经返回',self.main.notice_calls[0][1][0]['content'])
        self.assertEqual(turn['context']['knowledge']['retrieval']['search_outcome'],'completed')

    async def test_unknown_retrieval_never_claims_material_has_returned(self):
        self.enable();self.provider.failure='unknown'
        turn=await self.ask(public={'question':'公开天气主题'})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertNotIn('资料已经返回',self.main.calls[-1][0]['content'])
        self.assertNotIn('尚未取得可确认',self.main.calls[-1][0]['content'])
        self.assertNotIn('没有新增可用资料',self.main.calls[-1][0]['content'])
        self.assertEqual(turn['context']['knowledge']['retrieval']['search_outcome'],'unconfirmed')

    async def test_read_only_collector_reports_both_native_stages(self):
        self.enable();turn=await self.ask(public={'question':'公开主题'})
        from scripts.pcar_acceptance_check import collect
        result=collect(self.paths.home);stages=result['retrieval_stages']
        self.assertEqual(stages['conversation_native']['attempt_rows'],1)
        self.assertEqual(stages['conversation_search_notice']['attempt_rows'],1)
        self.assertEqual(stages['conversation_web_decision']['attempt_rows'],1)
        self.assertNotIn(turn['reply'],json.dumps(result,ensure_ascii=False))

    async def test_new_link_worded_read_one_is_explicit_but_jev_remains_the_decider(self):
        self.enable();self.need_web=False
        url='https://developer.mozilla.org/zh-CN/docs/Web/API/AbortController'
        turn=await self.ask('再读个东西：'+url,public={'question':'阅读这页','intent':'read_link','url':url})
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        self.assertIs(self.decisions[0][1]['state']['explicit_public_query_requested'],True)
        self.assertFalse(self.provider.calls)
        self.assertNotIn('尚未实际取得',self.main.calls[-1][0]['content'])
        self.assertNotIn('不声称读过未取得的网页',self.main.calls[-1][0]['content'])
        self.assertFalse(turn['context']['knowledge']['retrieval']['requested_link']['available'])


if __name__=='__main__':unittest.main()
