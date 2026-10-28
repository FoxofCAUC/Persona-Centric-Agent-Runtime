"""Heartbeat failure recovery with simulated SDK, disk, and process states."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from persona_runtime.bridge_health import heartbeat_loop
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.lifecycle import ServiceController
from persona_runtime.paths import RuntimePaths


class BridgeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def run_fault(self, fault):
        cfg={'bot_qq':'456','instance_id':'test-instance'}
        config=Mock();config.load.return_value=cfg
        platform=Mock();platform.meta.return_value=SimpleNamespace(name='aiocqhttp',id='persona-onebot')
        class Client:
            async def get_login_info(self):return {'user_id':456}
            async def get_status(self):return {'online':True}
        platform.get_client.return_value=Client()
        published=[];diagnostics=[];cycles=0
        paths=SimpleNamespace(home=Path('unused'))
        def publish(path,value):
            if fault=='publish' and cycles==0:raise PermissionError('secret exception content')
            published.append(value)
        def diagnose(paths,operation,**kw):
            diagnostics.append((operation,kw))
            if fault=='diagnostic':raise OSError('disk unavailable')
        if fault in ('config','diagnostic'):
            config.load.side_effect=[OSError('private configuration content'),cfg]
        if fault=='platform':
            platform.meta.side_effect=[ValueError('private adapter content'),platform.meta.return_value]
        async def sleep(_):
            nonlocal cycles
            cycles+=1
            if cycles==2:raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await heartbeat_loop(paths,config,lambda:[platform],publish=publish,sleep=sleep,diagnose=diagnose)
        self.assertEqual(cycles,2)
        self.assertTrue(published[-1]['online']);self.assertTrue(published[-1]['verified'])
        self.assertIsNone(published[-1]['error_type'])
        self.assertTrue(diagnostics)
        self.assertNotIn('private',json.dumps(diagnostics));self.assertNotIn('secret',json.dumps(diagnostics))
        return published,diagnostics

    async def test_config_failure_does_not_kill_loop(self):await self.run_fault('config')
    async def test_platform_metadata_failure_recovers(self):
        published,_=await self.run_fault('platform')
        self.assertEqual(published[0]['error_stage'],'platform')
        self.assertFalse(published[0]['online'])
    async def test_publish_failure_recovers(self):await self.run_fault('publish')
    async def test_error_log_failure_does_not_kill_loop(self):await self.run_fault('diagnostic')

    async def test_cancellation_during_probe_propagates_without_publishing(self):
        config=Mock();config.load.return_value={'bot_qq':'456','instance_id':'test'}
        platform=Mock();platform.meta.return_value=SimpleNamespace(name='aiocqhttp',id='persona-onebot')
        class Client:
            async def get_login_info(self):raise asyncio.CancelledError()
        platform.get_client.return_value=Client();publish=Mock();diagnose=Mock()
        with self.assertRaises(asyncio.CancelledError):
            await heartbeat_loop(SimpleNamespace(home=Path('unused')),config,lambda:[platform],publish=publish,diagnose=diagnose)
        publish.assert_not_called();diagnose.assert_not_called()

    def test_stale_heartbeat_is_unknown_not_logged_out(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='bridge-health-',dir=root) as directory:
            paths=RuntimePaths(Path(directory));config=ConfigStore(paths);cfg=config.initialize()
            atomic_json(paths.home/'runtime/astrbot-health.json',{'at':time.time()-300,'pid':123,
                        'instance_id':cfg['instance_id'],'online':True,'verified':True})
            ctl=ServiceController(paths)
            with patch.object(ctl,'owned',return_value=True),patch('persona_runtime.lifecycle.port_open',return_value=True), \
                    patch.object(ctl,'record',return_value={'host':{'ProcessId':123}}):
                result=ctl.health('astrbot')
            self.assertTrue(result['heartbeat_stale']);self.assertIsNone(result['login']);self.assertIsNone(result['onebot'])
            self.assertIn('心跳已过期',result['reason'])


if __name__=='__main__':unittest.main()
