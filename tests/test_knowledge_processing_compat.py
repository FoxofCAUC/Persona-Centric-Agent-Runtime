import asyncio,json,unittest
from persona_runtime.archive import scope
from persona_runtime.knowledge_context import attach
from persona_runtime.knowledge_processing_output import parse
from persona_runtime.models import RuntimeErrorBase,timestamp,LOCAL_USER_ID
from tests import test_m4 as fixtures

class CompatibilityCases(unittest.TestCase):
    def payload(self,materials):return {'knowledge_processing':{'stage':'translate','input':{'materials':materials}}}
    def test_missing_header_uses_only_the_registered_stage_schema(self):
        payload={'knowledge_processing':{'stage':'select','input':{'sources':[{'id':'known'}]}}}
        result=parse(payload,'{"section_ids":["known"]}')
        self.assertEqual(result['section_ids'],['known']);self.assertEqual(result['format_version'],1)
        with self.assertRaises(RuntimeErrorBase):parse(payload,'{"format_version":2,"section_ids":["known"]}')
        with self.assertRaises(RuntimeErrorBase):parse(payload,'{"section_ids":["known"],"other":"x"}')
        with self.assertRaises(RuntimeErrorBase):parse(payload,'{"section_ids":["invented"]}')

    def test_exact_references_coalesce_without_discarding_content(self):
        body='甲句。乙句。丙句。丁句。'
        materials=[{'id':'known','text':body,'evidence':{}}]
        unit={'text':body,'kind':'fact','refs':[{'material_id':'known','quote':q} for q in ('甲句。','乙句。','丙句。','丁句。')]}
        out=parse(self.payload(materials),json.dumps({'format_version':1,'units':[unit]}))
        self.assertLessEqual(len(out['units'][0]['refs']),3)
        self.assertTrue(all(r['quote'] in body for r in out['units'][0]['refs']))
        self.assertEqual(out['units'][0]['text'],body)
    def test_different_materials_are_not_silently_dropped(self):
        materials=[{'id':str(i),'text':'原文','evidence':{}} for i in range(4)]
        unit={'text':'原文','kind':'fact','refs':[{'material_id':str(i),'quote':'原文'} for i in range(4)]}
        with self.assertRaises(RuntimeErrorBase):parse(self.payload(materials),json.dumps({'format_version':1,'units':[unit]}))
    def test_long_exact_quote_is_losslessly_partitioned(self):
        quote='甲乙丙丁'*160
        unit={'text':'原文内容。','kind':'fact','refs':[{'material_id':'known','quote':quote}]}
        result=parse(self.payload([{'id':'known','text':quote,'evidence':{}}]),json.dumps({'format_version':1,'units':[unit]}))
        refs=result['units'][0]['refs'];self.assertEqual(''.join(r['quote'] for r in refs),quote)
        self.assertTrue(all(len(r['quote'])<=500 for r in refs))
    def test_reviewer_annotation_quotes_do_not_change_truth_flags(self):
        payload={'knowledge_processing':{'stage':'verify','input':{'units':[{}]}}}
        raw='{"format_version":1,"decisions":[{"index":0,"supported":false,"temporal_ok":true,"version_ok":true,"note":"原文说"可能"，不能升格。"}]}'
        out=parse(payload,raw);self.assertFalse(out['decisions'][0]['supported']);self.assertTrue(out['decisions'][0]['temporal_ok'])
        self.assertIn('"可能"',out['decisions'][0]['note'])
        with self.assertRaises(RuntimeErrorBase):parse(payload,raw.replace('"supported":false','"supported":ture'))
    def test_missing_evidence_is_disclosed_but_chitchat_is_untouched(self):
        messages=[{'role':'system','content':'角色设定'},{'role':'user','content':'请核实'}]
        turn={'model_snapshot':json.dumps({'max_input_chars':20000})}
        output,details=attach(messages,{}, {'state':'ready','reason':'no_matches','evidence':[]},turn)
        self.assertIn('没有取得可用的参考资料',output[0]['content']);self.assertEqual(details['knowledge']['evidence'],[])
        plain,_=attach(messages,{}, {'state':'not_triggered','evidence':[]},turn)
        self.assertEqual(plain,messages)

class InputContextCases(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):self.chat.knowledge.close();await self.chat.wait();await self.org.knowledge.drain()
    async def make_failed(self):
        old=await self.chat.submit('desktop','植物为什么需要光合作用？','failed-input-fixture')
        self.chat.cancel_queued(old['id']);await self.chat.wait()
        current=await self.chat.submit('desktop','那它白天和晚上都会进行吗？','context-fixture',session_id=old['session_id'])
        return old,current
    async def test_failed_user_topic_is_available_without_failed_reply(self):
        old,current=await self.make_failed()
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(current['id'],)).fetchone())
            context=self.org.processing.context(db,turn)
        self.chat.cancel_queued(current['id'])
        self.assertEqual(context['recent_context'][0]['user'],'植物为什么需要光合作用？')
        self.assertEqual(context['recent_context'][0]['assistant'],'')
        self.assertEqual(context['recent_context'][0]['request_state'],'interrupted')
    async def test_excluded_failed_input_and_legacy_profile_remain_protected(self):
        old,current=await self.make_failed()
        with self.store.connection(write=True) as db:
            previous=db.execute('SELECT user_event_id FROM turns WHERE id=?',(old['id'],)).fetchone()[0]
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(previous,self.persona['id'],LOCAL_USER_ID,'excluded',timestamp()))
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(current['id'],)).fetchone())
            self.assertEqual(self.org.processing.context(db,turn)['recent_context'],[])
        self.chat.cancel_queued(current['id'])
        policy=self.org.processing.policy(self.persona['id']);legacy=dict(policy['data']);legacy.pop('prompt_profile')
        self.org.processing.update_policy(self.persona['id'],legacy,policy['revision'],'legacy-compatible')
        self.assertNotIn('prompt_profile',self.org.processing.policy(self.persona['id'])['data'])
