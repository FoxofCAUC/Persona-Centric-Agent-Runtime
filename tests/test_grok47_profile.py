"""Grok interactive wire profile, without remote calls or weakened validators."""
from copy import deepcopy
import unittest
from persona_runtime.provider_compat import build_request,describe_request,endpoint,usage_fields
from persona_runtime.reply_batch import expression_snapshot,validate_reply
from persona_runtime.chat import ChatCompletionsClient,ModelError
from tests import test_grok_gateway_compatibility as fixture


class Contracts(unittest.TestCase):
    def model(self,**extra):return {'name':'grok-4.7','base_url':'https://api.uniapi.io/v1','max_tokens':512,**extra}
    def test_selector_schema_is_separate_from_main_and_other_json_tasks(self):
        model=self.model(_json_output=True,_character_selection=True)
        messages=[{'role':'system','content':'原选择提示不改 JSON'},{'role':'user','content':'人工材料'}];before=deepcopy(messages)
        p,d=build_request(model,messages)
        self.assertEqual(messages,before);self.assertEqual(p['response_format']['type'],'json_schema')
        schema=p['response_format']['json_schema']['schema']
        self.assertEqual(set(schema['required']),{'schema_version','lore_ids','example_ids'})
        self.assertFalse(schema['additionalProperties']);self.assertEqual(p['max_tokens'],512)
        self.assertEqual(p['reasoning_effort'],'low');self.assertNotIn('thinking',p)
        self.assertEqual(d['profile'],'grok47-chat-v1')
        other,_=build_request(self.model(_json_output=True),messages)
        self.assertNotIn('response_format',other);self.assertNotIn('reasoning_effort',other)
    def test_main_keeps_original_json_schema_budget_and_temperature(self):
        m=self.model(max_tokens=8192,_reply_mode='natural_structured',_expression_snapshot=expression_snapshot('natural_structured'),
            _dialogue_profile={'version':'natural-dialogue-v2','seed':'artificial','temperature':1.1})
        p,d=build_request(m,[{'role':'user','content':'人工输入'}])
        self.assertEqual(p['reasoning_effort'],'low');self.assertEqual(p['temperature'],1.1)
        self.assertEqual(p['max_tokens'],8192);self.assertTrue(p['response_format']['json_schema']['strict'])
        self.assertEqual(p['response_format']['json_schema']['name'],'pcar_reply_v3')
    def test_endpoint_normalization_without_model_substitution(self):
        m=self.model(base_url='https://api.uniapi.io/v1/chat/completions/')
        self.assertEqual(endpoint(m),'https://api.uniapi.io/v1/chat/completions')
        self.assertEqual(endpoint(m,'models'),'https://api.uniapi.io/v1/models')
    def test_deepseek_selector_payload_is_unchanged(self):
        m=self.model(name='deepseek-flash',base_url='https://api.deepseek.com',_json_output=True)
        messages=[{'role':'system','content':'JSON'}]
        self.assertEqual(build_request(m,messages),build_request(m|{'_character_selection':True},messages))
    def test_numeric_reasoning_usage_never_retains_reasoning_text(self):
        value=usage_fields({'completion_tokens':5,'completion_tokens_details':{'reasoning_tokens':2,'content':'private'},
            'prompt_tokens_details':{'cached_tokens':3,'secret':'private'},'cost':'untrusted'},grok=True)
        self.assertEqual(value['completion_tokens_details'],{'reasoning_tokens':2})
        self.assertNotIn('private',str(value))


class HTTP(fixture.GrokGatewayCompatibility):
    async def test_truncated_even_parseable_answer_is_rejected(self):
        self.response['choices'][0]['finish_reason']='length'
        with self.assertRaisesRegex(ModelError,'provider_output_truncated'):
            await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(len(self.payloads),1)
    async def test_blank_is_classified_without_body_or_second_request(self):
        self.response['choices'][0]['message']['content']=' '*12
        with self.assertRaisesRegex(ModelError,'provider_empty_response') as caught:
            await ChatCompletionsClient().complete(self.model,self.messages)
        self.assertEqual(caught.exception.diagnostics['content_kind'],'ascii_spaces_only')
        self.assertEqual(caught.exception.diagnostics['http_status'],200)
        self.assertNotIn('synthetic-internal',str(caught.exception.diagnostics));self.assertEqual(len(self.payloads),1)
    async def test_selector_sends_schema_and_effort_and_keeps_local_validation(self):
        model=self.model|{'_character_selection':True,'_json_output':True}
        await ChatCompletionsClient().complete(model,self.messages)
        self.assertEqual(self.payloads[0]['reasoning_effort'],'low')
        self.assertEqual(self.payloads[0]['response_format']['json_schema']['name'],'pcar_character_selection_v1')


if __name__=='__main__':unittest.main()
