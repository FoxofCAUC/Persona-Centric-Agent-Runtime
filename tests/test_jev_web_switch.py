"""Thin native Jev decision contract; local fixtures, no paid calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.knowledge_web_decision import request,parse,execute
from persona_runtime.knowledge_routing import configured_model


class DecisionContractTests(unittest.TestCase):
    def test_binary_choice_does_not_require_confidence_or_probability_threshold(self):
        for choice in ('web','none'):
            for extra in ({},{'confidence':0.01,'probabilities':{'web':0.1,'none':0.9}},{'confidence':float('nan')}):
                raw=json.dumps({'answers':{'route':{'choice':choice,**extra}}})
                self.assertEqual(parse(raw)['need_web'],choice=='web')

    def test_unknown_choice_is_not_an_offline_decision(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'jev_web_decision_invalid'):
            parse('{"answers":{"route":{"choice":"local"}}}')

    def test_wire_contains_only_one_binary_question(self):
        body=request({'name':'jev-test','max_input_chars':24000,'encrypted_key':'not-in-body'},{'question':'新消息呢？'})
        self.assertEqual(set(body),{'model','state','questions'});self.assertEqual(set(body['questions']),{'route'})
        self.assertEqual(set(body['questions']['route']['criteria']),{'none','web'})
        self.assertNotIn('not-in-body',json.dumps(body))

    def test_configuration_never_falls_back_to_the_main_credential(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='jev-config-',dir=root) as folder:
            paths=SimpleNamespace(home=Path(folder));main={'timeout':60,'max_input_chars':24000,'name':'main','base_url':'https://main.invalid','encrypted_key':'main-secret'}
            with patch('persona_runtime.config.ConfigStore') as config:
                config.return_value.load.return_value={'model':main}
                with self.assertRaisesRegex(RuntimeErrorBase,'jev_configuration_required'):configured_model(paths)
                file=paths.home/'private/knowledge-router/config.json';file.parent.mkdir(parents=True)
                file.write_text(json.dumps({'name':'jev-test','base_url':'https://jev.invalid/v1','encrypted_key':'jev-secret'}),encoding='utf-8')
                value=configured_model(paths);self.assertEqual(value['encrypted_key'],'jev-secret');self.assertNotIn('main-secret',json.dumps(value))


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_error_is_known_before_dispatch(self):
        from persona_runtime.chat import ModelError
        model={'name':'jev-test','base_url':'https://jev.invalid/v1','encrypted_key':'wrong-user','timeout':60,'max_input_chars':24000}
        with patch('persona_runtime.security.unprotect',side_effect=OSError('DPAPI')), \
                patch('persona_runtime.network_proxy.configured_proxy',return_value='http://127.0.0.1:7890'), \
                patch('aiohttp.ClientSession',side_effect=AssertionError('must not dispatch')):
            with self.assertRaisesRegex(ModelError,'jev_credential_unavailable') as caught:
                await execute(model,request(model,{'question':'查询'}))
        self.assertFalse(caught.exception.diagnostics['request_headers_sent'])

    async def test_native_request_uses_system_proxy_and_dedicated_key_without_redirects(self):
        calls=[]
        class Content:
            async def iter_chunked(self,_):yield b'{"answers":{"route":{"choice":"web"}},"usage":{"input_tokens":2,"output_tokens":1}}'
        class Response:
            status=200;content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Session:
            def __init__(self,**kwargs):self.kwargs=kwargs
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def post(self,url,**kwargs):calls.append((url,kwargs));return Response()
        model={'name':'jev-test','base_url':'https://jev.invalid/v1','encrypted_key':'protected-jev','timeout':60,'max_input_chars':24000}
        with patch('aiohttp.ClientSession',Session),patch('aiohttp.TCPConnector',return_value=None), \
                patch('persona_runtime.security.unprotect',return_value='jev-key-fixture'), \
                patch('persona_runtime.network_proxy.configured_proxy',return_value='http://127.0.0.1:7890'):
            value=await execute(model,request(model,{'question':'查一下更新'}))
        self.assertEqual(parse(value[0])['need_web'],True);self.assertEqual(value[1]['total_tokens'],3)
        self.assertEqual(calls[0][0],'https://jev.invalid/v1/systemone')
        self.assertEqual(calls[0][1]['proxy'],'http://127.0.0.1:7890');self.assertFalse(calls[0][1]['allow_redirects'])
        self.assertEqual(calls[0][1]['headers']['Authorization'],'Bearer jev-key-fixture')
