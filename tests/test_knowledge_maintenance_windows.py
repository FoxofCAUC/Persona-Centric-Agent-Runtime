import json,unittest
from persona_runtime.knowledge_output import parse,messages,MAIN_VERSION
from persona_runtime.models import RuntimeErrorBase

class MaintenanceWindowTests(unittest.TestCase):
    def payload(self):
        return {'external_knowledge':{'pipeline':MAIN_VERSION,'stage':'extract','document':{'title':'公开测试','excerpt':'对象颜色是蓝色。','upstream_at':'2026-11-12T00:00:00Z',
            'windows':[{'id':'W1','start':0,'end':8,'text':'对象颜色是蓝色。'}]},'existing_claims':[]}}
    def test_main_input_uses_windows_without_duplicated_excerpt_and_binds_exact_quote(self):
        payload=self.payload();value=json.loads(messages(payload)[1]['content']);self.assertNotIn('excerpt',value['document'])
        item={'subject':'对象','predicate':'颜色','value':'蓝色','kind':'source_claim','window_id':'W1','valid_from':None,'valid_until':None}
        out=parse(payload,json.dumps({'format_version':1,'claims':[item]}));self.assertEqual(out['claims'][0]['quote'],'对象颜色是蓝色。')
        self.assertEqual(out['_normalizations'][0]['kind'],'registered_maintenance_window')
        bad=item|{'window_id':'invented'}
        with self.assertRaises(RuntimeErrorBase):parse(payload,json.dumps({'format_version':1,'claims':[bad]}))
        legacy=self.payload();legacy['external_knowledge']['pipeline']='public-knowledge-extract-review-v1'
        with self.assertRaises(RuntimeErrorBase):parse(legacy,json.dumps({'format_version':1,'claims':[item]}))
    def test_key_recovery_preserves_review_boolean_and_rejects_duplicate_flags(self):
        payload=self.payload();payload['external_knowledge'].update(stage='review',proposals={'claims':[{}]})
        raw='{"format_version":1,"decisions":[{"index":0,supported":false,"temporal_ok":true,"version_ok":true,"kind":"source_claim","note":"不支持"}]}'
        out=parse(payload,raw);self.assertFalse(out['decisions'][0]['supported'])
        with self.assertRaises(RuntimeErrorBase):parse(payload,raw.replace('supported":false','"supported":false,"supported":true'))


class ConflictContractTests(unittest.TestCase):
    def test_conflicting_attribution_stays_unresolved_and_prompt_matches_validator(self):
        from persona_runtime.knowledge_processing_output import messages,parse
        from persona_runtime.models import RuntimeErrorBase
        payload={'knowledge_processing':{'stage':'translate','policy':{'data':{'prompt_profile':'question_focused_v6'}},'input':{
            'question':'两份公告有什么分歧？','conflict_contract_revision':1,
            'materials':[{'id':'source-a','text':'公告A称11点开放。','evidence':{'claim_state':'conflict','claim_kind':'source_claim'}}]}}}
        self.assertIn('包括分别转述单方来源的条目',messages(payload)[0]['content'])
        unit={'text':'公告A称11点开放。','kind':'source_claim','refs':[{'material_id':'M1'}]}
        with self.assertRaises(RuntimeErrorBase) as raised:parse(payload,json.dumps({'format_version':1,'units':[unit]}))
        self.assertEqual(raised.exception.diagnostic['reason'],'conflict_erased')
        unit['kind']='unresolved'
        result=parse(payload,json.dumps({'format_version':1,'units':[unit]}))
        self.assertEqual(result['units'][0]['kind'],'unresolved')
        self.assertEqual(result['units'][0]['text'],'公告A称11点开放。')
        self.assertEqual(result['units'][0]['refs'][0]['quote'],'公告A称11点开放。')
