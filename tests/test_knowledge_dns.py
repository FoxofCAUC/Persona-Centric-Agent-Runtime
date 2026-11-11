import asyncio,json,unittest
from unittest.mock import AsyncMock,patch
from persona_runtime.chat import ModelError
from persona_runtime.knowledge_dns import public_target,answer_addresses
from persona_runtime.knowledge_processing_output import parse,numeric_tokens
from persona_runtime.models import RuntimeErrorBase

class DNSCases(unittest.IsolatedAsyncioTestCase):
    async def test_nonpublic_system_result_needs_independent_public_proof(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',new=AsyncMock(return_value=[(0,0,0,'',('2001::1',443,0,0))])),patch('persona_runtime.knowledge_dns.https_answers',new=AsyncMock(return_value=(['208.80.153.224','2620:0:863:ed1a::1'],True))) as secure:
            result=await public_target('zh.wikipedia.org',443,180)
        self.assertEqual(result['method'],'https_dns');self.assertTrue(result['via_system_proxy']);secure.assert_awaited_once()
    async def test_private_secure_records_are_still_blocked(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',new=AsyncMock(return_value=[(0,0,0,'',('10.0.0.1',443))])),patch('persona_runtime.knowledge_dns.https_answers',new=AsyncMock(return_value=(['127.0.0.1'],False))):
            with self.assertRaisesRegex(ModelError,'web_target_not_public'):await public_target('example.com',443,180)
    async def test_unconfirmed_secure_response_cannot_authorize_fetch(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',new=AsyncMock(return_value=[])),patch('persona_runtime.knowledge_dns.https_answers',new=AsyncMock(side_effect=TimeoutError())):
            with self.assertRaisesRegex(ModelError,'web_dns_unavailable'):await public_target('example.com',443,180)
    def test_response_question_and_cname_chain_are_bound(self):
        value={'Status':0,'Question':[{'name':'example.com.','type':1}],'Answer':[{'name':'example.com.','type':5,'data':'target.example.'},{'name':'target.example.','type':1,'data':'93.184.216.34'},{'name':'unrelated.example.','type':1,'data':'10.0.0.1'}]}
        self.assertEqual(answer_addresses(value,'example.com',1),['93.184.216.34'])
        with self.assertRaises(ModelError):answer_addresses(value,'other.example',1)

class NumericGroupingCases(unittest.TestCase):
    def test_thousands_grouping_is_equivalent_without_changing_values(self):
        self.assertEqual(numeric_tokens('2,406.4万及1.7%'),numeric_tokens('2406.4万及1.7%'))
        self.assertNotEqual(numeric_tokens('2406.5万'),numeric_tokens('2,406.4万'))
        self.assertNotEqual(numeric_tokens('240.6'),numeric_tokens('2,40.6'))
    def test_grouping_repair_does_not_admit_unsupported_numbers(self):
        material={'id':'known','text':'有2,406.4万立方公里。','evidence':{}}
        payload={'knowledge_processing':{'stage':'translate','input':{'materials':[material]}}}
        out={'format_version':1,'units':[{'text':'有2406.4万立方公里。','kind':'fact','refs':[{'material_id':'known','quote':material['text']}]}]}
        self.assertTrue(parse(payload,json.dumps(out))['units'])
        out['units'][0]['text']='有2406.5万立方公里。'
        with self.assertRaises(RuntimeErrorBase):parse(payload,json.dumps(out))
