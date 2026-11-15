"""Local release gates; no public API or private business fixture."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from persona_runtime.chat import ChatCompletionsClient,ModelError
from persona_runtime.models import identifier,RuntimeErrorBase,LOCAL_USER_ID
from persona_runtime.provider_policy import require_generation_provider
from tests import test_m9r_native_retrieval as fixture


class ProviderPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_unofficial_generation_is_known_not_sent_before_key_or_transport(self):
        model={'base_url':'https://gateway.example/v1','name':'deepseek-flash','encrypted_key':'must-not-decrypt'}
        with patch('persona_runtime.chat.aiohttp.ClientSession') as transport,patch('persona_runtime.chat.unprotect') as decrypt:
            with self.assertRaisesRegex(ModelError,'provider_official_deepseek_required') as caught:
                await ChatCompletionsClient().complete(model,[{'role':'user','content':'public'}])
        transport.assert_not_called();decrypt.assert_not_called()
        self.assertIs(caught.exception.diagnostics['request_headers_sent'],False)

    def test_exact_official_address_and_current_model_required(self):
        base={'base_url':'https://api.deepseek.com/v1','name':'deepseek-flash'}
        require_generation_provider(base)
        for url in ('http://api.deepseek.com/v1','https://api.deepseek.com.evil.invalid/v1',
                'https://user@api.deepseek.com/v1','https://api.deepseek.com:8443/v1','https://api.deepseek.com/v1?q=1',
                'http://127.0.0.1:1234/v1'):
            with self.assertRaises(ValueError):require_generation_provider(base|{'base_url':url})
        with self.assertRaises(ValueError):require_generation_provider(base|{'name':'deepseek-chat'})
        require_generation_provider(base|{'base_url':'http://127.0.0.1:1234/v1'},allow_loopback_fixture=True)


class LocalRelease(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Integration.setUp
    asyncTearDown=fixture.Integration.asyncTearDown
    enable=fixture.Integration.enable
    ask=fixture.Integration.ask
    decision_transport=fixture.Integration.decision_transport
    transport=fixture.Integration.transport

    async def test_model_probe_bypasses_persona_and_retrieval(self):
        self.enable()
        turn=await self.chat.submit('test','Reply with OK.',identifier(),test=True)
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(turn['id'])['status'],'completed')
        self.assertFalse(self.decisions);self.assertFalse(self.provider.calls);self.assertFalse(self.main.notice_calls)

    async def test_reader_off_on_reuses_existing_paid_roots_after_primary_failure(self):
        self.enable();original=self.main.complete;failed=[]
        async def first(model,messages):
            if '_search_notice_profile' not in model and not failed:
                failed.append(1);raise ModelError('provider_http_400',diagnostics={'http_status':400,'provider_terminal_error':True,'response_headers_received':True,'request_headers_sent':True})
            return await original(model,messages)
        self.main.complete=first
        turn=await self.ask(public={'question':'公开游戏攻略'})
        self.assertEqual(turn['status'],'failed')
        for value in (False,True):
            pid=self.persona['id'];r=self.chat.knowledge.current_policy(pid);p=self.org.processing.policy(pid);w=self.org.web.policy(pid)
            self.org.processing.enable_for_persona(pid,value,r['revision'],p['revision'],w['revision'],identifier())
        await self.chat.retry(turn['id']);await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(turn['id'])['status'],'completed')
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.main.notice_calls),1);self.assertEqual(len(self.decisions),1)

    async def test_same_root_legacy_unknown_is_held_before_new_native_dispatch(self):
        self.enable();turn=await self.chat.submit('desktop','公开旧请求',identifier())
        with self.store.connection(write=True) as db:
            from persona_runtime.archive import turn_scope
            root=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
            payload={'scope':turn_scope(db,root),'knowledge_processing':{'root_turn_id':turn['id'],'stage':'conversation_search_plan'}}
            # A frozen old task with no reusable result is sufficient to require
            # review; it must not silently switch executors on a primary retry.
            db.execute('INSERT INTO organization_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier(),'legacy-fixture',turn['persona_id'],LOCAL_USER_ID,turn['session_id'],'desktop','both',1,'legacy',json.dumps(payload),'{}','blocked_unknown','organization_effect_unknown',turn['created_at'],turn['created_at']))
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(turn['id'])['error_code'],'legacy_request_requires_review')
        self.assertFalse(self.provider.calls);self.assertFalse(self.decisions);self.assertFalse(self.main.calls)

    async def test_upgrade_checker_keeps_original_rows_and_allows_new_business(self):
        self.enable();turn=await self.ask(public={'question':'公开游戏攻略'})
        from scripts.pcar_upgrade_check import snapshot,compare
        before=snapshot(self.paths.home)
        self.need_web=False;await self.ask('继续刚才的资料')
        after=snapshot(self.paths.home);result=compare(before,after)
        self.assertTrue(result['passed'],result);self.assertGreater(result['new_business_rows'],0)
        corrupted=deepcopy(after)
        key=next(iter(before['tables']['events']));corrupted['tables']['events'][key]='changed'
        self.assertFalse(compare(before,corrupted)['passed'])
        root=next(iter(before['attempts_by_root']));corrupted=deepcopy(after);corrupted['attempts_by_root'][root].append('new-dispatch')
        self.assertFalse(compare(before,corrupted)['passed'])


if __name__=='__main__':unittest.main()
