import json,unittest
from persona_runtime.knowledge_processing_output import parse
from persona_runtime.models import RuntimeErrorBase
from tests import test_knowledge_public_pages as pages

class KeyQuoteTests(unittest.TestCase):
    def payload(self):return {'knowledge_processing':{'stage':'translate','policy':{'data':{'prompt_profile':'question_focused_v6'}},'input':{'materials':[{'id':'real','text':'2000年开放。','evidence':{}}]}}}
    def test_known_key_only_is_recovered_and_values_stay_unchanged(self):
        raw='{"format_version":1,"units":[{"text":"2000年开放。",kind":"fact","refs":[{"material_id":"M1"}]}]}'
        out=parse(self.payload(),raw)
        self.assertEqual(out['units'][0]['text'],'2000年开放。');self.assertEqual(out['units'][0]['kind'],'fact')
        self.assertTrue(out['_normalizations'][0]['values_unchanged']);self.assertEqual(out['_normalizations'][0]['keys'],['kind'])
        with self.assertRaises(RuntimeErrorBase):parse(self.payload(),raw.replace('2000年','2030年'))
    def test_unknown_keys_and_value_typos_not_repaired(self):
        for raw in ('{"format_version":1,other":[]}', '{"format_version":1,"units":[{"text":"x","kind":fact,"refs":[]}]}'):
            with self.assertRaises(RuntimeErrorBase):parse(self.payload(),raw)
    def test_duplicate_review_flags_rejected_and_private_true_preserved(self):
        payload={'knowledge_processing':{'stage':'public_page_review','policy':{'data':{'prompt_profile':'question_focused_v6'}},'input':{'candidates':[{'id':'p1'}]}}}
        raw='{"format_version":1,"decisions":[{"id":"p1","public":true,"relevant":true,private_detail":true,"note":"私人信息"}]}'
        self.assertTrue(parse(payload,raw)['decisions'][0]['private_detail'])
        with self.assertRaises(RuntimeErrorBase):parse(payload,raw.replace('private_detail":true','"private_detail":true,"private_detail":false'))
    def test_legacy_invalid_receipts_not_implicitly_changed(self):
        p=self.payload();p['knowledge_processing']['policy']['data']['prompt_profile']='legacy_v1'
        with self.assertRaises(RuntimeErrorBase):parse(p,'{"format_version":1,units":[]}')

class FormatReceiptTests(unittest.IsolatedAsyncioTestCase):
    setUp=pages.PublicPageTests.setUp
    asyncTearDown=pages.PublicPageTests.asyncTearDown
    enable=pages.PublicPageTests.enable
    ask=pages.PublicPageTests.ask
    async def test_recovered_translation_keeps_raw_receipt_and_one_attempt(self):
        self.enable();original=self.model.complete
        async def bad_key(model,messages):
            value=json.loads(messages[-1]['content']);response,usage=await original(model,messages)
            if 'materials' in value and 'units' not in value:response=response.replace('"kind":','kind":')
            return response,usage
        self.model.complete=bad_key;result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        with self.store.connection() as db:
            rows=db.execute("SELECT r.response_text,r.diagnostic_json FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id JOIN organization_receipts r ON r.attempt_id=a.id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='translate'").fetchall()
        self.assertEqual(len(rows),1);self.assertIn('kind":',rows[0][0]);audit=json.loads(rows[0][1])
        self.assertTrue(any(r['kind']=='inserted_opening_key_quotes' for r in audit['normalizations']))
