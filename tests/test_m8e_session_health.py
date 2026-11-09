"""A platform-expired account must not destroy a healthy local bridge."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from persona_runtime.app_runtime import AppRuntime
from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_session import login_status_verified
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.desktop_runtime import connection_status_text


class SessionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='m8e-session-',dir='D:/Persona-Centric Agent Runtime/tmp')
        self.addCleanup(self.temp.cleanup);self.paths=RuntimePaths(Path(self.temp.name))
        self.cfg=ConfigStore(self.paths).initialize()
        self.identity={'ProcessId':321,'Created':'owned','ExecutablePath':'owned.exe'}
        self.record={'instance_id':self.cfg['instance_id'],'bot':self.cfg['bot_qq'],'qq_listener':self.identity}

    def status(self,responses,record=None,listener=None):
        from persona_runtime.qq_session import _health_credentials
        _health_credentials.clear()
        with patch('persona_runtime.lifecycle.listener',return_value=self.identity if listener is None else listener), \
                patch('persona_runtime.lifecycle.same_process',return_value=True), \
                patch('persona_runtime.qq_session.web_call',side_effect=responses) as call:
            result=login_status_verified(self.paths,record or self.record)
            return result,call.call_count

    def test_native_expired_login_is_visible_without_raw_diagnostic(self):
        result,calls=self.status([{'Credential':'never-persist'},
            {'isLogin':False,'coreReady':False,'loginError':'[KickedOffLine] private upstream diagnostic'}])
        self.assertFalse(result['account_logged_in']);self.assertTrue(result['login_required'])
        self.assertEqual(result['login_reason'],'session_expired');self.assertEqual(calls,2)
        self.assertNotIn('private',str(result));self.assertNotIn('never-persist',str(result))

    def test_logged_in_requires_matching_account(self):
        for user,expected in ((self.cfg['bot_qq'],True),('other',False)):
            with self.subTest(user=user):
                result,_=self.status([{'Credential':'credential'},{'isLogin':True,'coreReady':True},{'uin':user}])
                self.assertEqual(result['account_logged_in'],expected)
                if not expected:self.assertEqual(result['login_reason'],'account_mismatch')

    def test_wrong_instance_or_listener_never_receives_web_auth(self):
        for record,listener in ((self.record|{'instance_id':'other'},self.identity),
                                (self.record,self.identity|{'ProcessId':999})):
            with self.subTest(record=record),patch('persona_runtime.lifecycle.listener',return_value=listener), \
                    patch('persona_runtime.lifecycle.same_process',return_value=True), \
                    patch('persona_runtime.qq_session.web_call') as call:
                with self.assertRaises(RuntimeErrorBase):login_status_verified(self.paths,record)
                call.assert_not_called()

    def test_expired_account_does_not_consume_bridge_restart_budget(self):
        now=[time.time()]
        state={'state':'运行','owned':True,'login':True,'onebot':False}
        controller=SimpleNamespace(paths=self.paths,ensure_binding=lambda:None,closing=threading.Event(),
            config=SimpleNamespace(load=lambda:{'owner_qq':'123','bot_qq':'456',
                'runtime':{'components':{'astrbot':True},'qq_persona_id':'test-persona'}}),
            health=lambda _:dict(state),_start_ready=Mock(return_value=True))
        runtime=AppRuntime(controller,clock=lambda:now[0]);runtime.last_maintenance=now[0]
        with patch.object(runtime,'launch_operation') as launch:
            for _ in range(10):runtime.tick();now[0]+=5
            launch.assert_not_called();self.assertFalse(runtime.restarts)
        self.assertEqual(runtime.failures['astrbot'],0)

    def test_visible_status_separates_connection_from_native_login(self):
        text=connection_status_text({'napcat':{'account_logged_in':False,'login_reason':'session_expired'},
            'astrbot':{'login':True,'onebot_connected':True,'account_online':False}})
        self.assertIn('登录已失效',text);self.assertIn('OneBot：已连接',text);self.assertIn('账号在线：离线',text)
        self.assertIn('状态待核对',connection_status_text({}))

    def test_status_refresh_reuses_auth_without_consuming_login_rate(self):
        from persona_runtime.qq_session import _health_credentials
        _health_credentials.clear()
        with patch('persona_runtime.lifecycle.listener',return_value=self.identity), \
                patch('persona_runtime.lifecycle.same_process',return_value=True), \
                patch('persona_runtime.qq_session.web_call',side_effect=[
                    {'Credential':'in-memory'}, {'isLogin':False}, {'isLogin':False}]) as call:
            login_status_verified(self.paths,self.record);login_status_verified(self.paths,self.record)
        self.assertEqual(sum(c.args[1]=='/auth/login' for c in call.call_args_list),1)
        self.assertEqual(call.call_count,3)

    def test_network_status_failure_does_not_force_another_auth(self):
        from persona_runtime.qq_session import _health_credentials
        _health_credentials.clear()
        with patch('persona_runtime.lifecycle.listener',return_value=self.identity), \
                patch('persona_runtime.lifecycle.same_process',return_value=True), \
                patch('persona_runtime.qq_session.web_call',side_effect=[
                    {'Credential':'in-memory'},TimeoutError(),{'isLogin':False}]) as call:
            with self.assertRaises(TimeoutError):login_status_verified(self.paths,self.record)
            login_status_verified(self.paths,self.record)
        self.assertEqual(sum(c.args[1]=='/auth/login' for c in call.call_args_list),1)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_status_call_is_connection_proof_even_when_account_offline(self):
        from persona_runtime.bridge_health import heartbeat_loop
        class Client:
            async def get_login_info(self):return {'user_id':'123'}
            async def get_status(self):return {'online':False}
        platform=SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp',id='persona-onebot'),get_client=lambda:Client())
        config=SimpleNamespace(load=lambda:{'bot_qq':'123','instance_id':'test'})
        published=[]
        async def stop(_):raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await heartbeat_loop(SimpleNamespace(home=Path('unused')),config,lambda:[platform],
                publish=lambda _,value:published.append(value),sleep=stop)
        self.assertTrue(published[0]['connected']);self.assertTrue(published[0]['verified'])
        self.assertFalse(published[0]['online'])
