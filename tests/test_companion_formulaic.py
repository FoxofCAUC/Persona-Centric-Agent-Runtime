"""Artificial normal-runtime integration; no private sources or real generation."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_formulaic as run
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.storage import Store


class FormulaicFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=DEFAULT_HOME/'tmp',prefix='formulaic-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);paths=RuntimePaths(self.home)
        Store(paths).initialize();config=ConfigStore(paths);config.initialize()
        config.update({'model':{'base_url':'http://127.0.0.1:9/v1','name':'grok-4.7','hourly_limit':0}})
        self.enterContext(patch('socket.socket.connect',side_effect=AssertionError('unit_network_blocked')))

    async def prepared(self):
        return await run.prepare(self.home)

    async def test_twelve_actual_runtime_requests_match_frozen_and_only_assistant_history_changes(self):
        root=await self.prepared();checked=run.verify(root);plan=checked['plan'];owner=checked['owner']
        self.assertEqual(len(plan['positions']),12)
        self.assertTrue(all(x['only_assistant_history_changed'] for x in plan['pair_checks']))
        self.assertEqual(set(p.name for p in root.iterdir()),{'trial.json'})
        client=run.FixtureClient()
        result=await run.execute(root,simulated=True,client=client)
        self.assertEqual(result['completed'],12)
        self.assertEqual(result['physical_calls'],0);self.assertEqual(result['simulated_calls'],12)
        self.assertEqual(len(client.calls),12)
        data=run.body_root(owner)
        for pid,call in zip(plan['order'],client.calls):
            frozen=run.read(data/'preflight'/pid/'request.json')
            self.assertEqual(call['wire'],frozen['wire'])
            output=run.read(data/'positions'/pid/'result.json')
            self.assertEqual(output['messages_hash'],frozen['messages_hash'])
            self.assertEqual(len(output['usage']['generation_retry']['attempts']),1)
        with Store(RuntimePaths(Path(owner['home']))).connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sessions').fetchone()[0],24)
            self.assertEqual(conn.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0],0)
        with self.assertRaisesRegex(Exception,'no_auto_resume'):
            await run.execute(root,simulated=True,client=client)
        self.assertEqual(len(client.calls),12)

    async def test_paid_entry_rejects_artificial_trial_before_dispatch(self):
        root=await self.prepared();plan=run.verify(root)['plan'];client=run.FixtureClient()
        with self.assertRaisesRegex(Exception,'simulation_scope_mismatch'):
            await run.run_position(root,plan['order'][0],client=client)
        self.assertEqual(client.calls,[])
        self.assertFalse((root/'execution.lock').exists())

    async def test_changed_frozen_request_stops_before_start(self):
        root=await self.prepared();checked=run.verify(root);data=run.body_root(checked['owner'])
        pid=checked['plan']['order'][0];file=data/'preflight'/pid/'request.json'
        value=run.read(file);value['messages'][-1]['content']='人为篡改';atomic_json(file,value)
        with self.assertRaisesRegex(Exception,'frozen_request_changed'):
            await run.run_position(root,pid,simulated=True)
        self.assertFalse((data/'positions'/pid/'started.json').exists())

    async def test_unfinished_position_is_never_resumed(self):
        root=await self.prepared();checked=run.verify(root);data=run.body_root(checked['owner'])
        pid=checked['plan']['order'][0]
        atomic_json(data/'positions'/pid/'started.json',{'kind':'simulated'})
        with self.assertRaisesRegex(Exception,'unfinished_no_auto_resume'):
            await run.run_position(root,pid,simulated=True)

    async def test_native_retry_recovers_with_identical_input(self):
        root=await self.prepared();plan=run.verify(root)['plan'];base=run.FixtureClient();calls=[]
        class Client:
            async def complete(self,model,messages):
                calls.append(messages)
                if len(calls)==1:raise ModelError('provider_network_error')
                return await base.complete(model,messages)
        async def no_delay(_):pass
        with patch('persona_runtime.generation_retry.retry_delay',no_delay):
            result=await run.run_position(root,plan['order'][0],simulated=True,client=Client())
        self.assertEqual(result['status'],'completed',result.get('error_code'))
        self.assertEqual(result['simulated_calls'],2);self.assertEqual(calls[0],calls[1])
        with self.assertRaisesRegex(Exception,'already_completed'):
            await run.run_position(root,plan['order'][0],simulated=True,client=Client())

    async def test_retry_exhaustion_stops_batch_after_four_calls(self):
        root=await self.prepared();checked=run.verify(root);plan=checked['plan']
        class Client:
            async def complete(self,model,messages):raise ModelError('provider_network_error')
        async def no_delay(_):pass
        with patch('persona_runtime.generation_retry.retry_delay',no_delay):
            result=await run.run_position(root,plan['order'][0],simulated=True,client=Client())
        self.assertEqual(result['status'],'failed');self.assertEqual(result['simulated_calls'],4)
        self.assertEqual(result['error_code'],'provider_retries_exhausted')
        with self.assertRaisesRegex(Exception,'batch_stopped'):
            await run.run_position(root,plan['order'][1],simulated=True)

    async def test_cancel_settles_native_attempt_without_success(self):
        root=await self.prepared();checked=run.verify(root);data=run.body_root(checked['owner']);entered=asyncio.Event()
        class Client:
            async def complete(self,model,messages):entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(run.run_position(root,checked['plan']['order'][0],simulated=True,client=Client()))
        await asyncio.wait_for(entered.wait(),10)
        atomic_json(data/'cancel.json',{'reason':'user'})
        result=await asyncio.wait_for(task,5)
        self.assertNotEqual(result['status'],'completed')
        self.assertEqual(result['usage']['generation_retry']['attempts'][0]['status'],'interrupted')

    async def test_terminal_write_failure_remains_visible_without_replay_or_review_overwrite(self):
        root=await self.prepared();checked=run.verify(root);data=run.body_root(checked['owner'])
        pid=checked['plan']['order'][0];original=run.atomic_json;client=run.FixtureClient()
        def fail_terminal(path,value):
            if Path(path).name=='terminal.json':raise OSError('artificial_terminal_failure')
            return original(path,value)
        with patch.object(run,'atomic_json',side_effect=fail_terminal):
            with self.assertRaises(OSError):
                await run.run_position(root,pid,simulated=True,client=client)
        self.assertEqual(len(client.calls),1)
        summary=run.report(root)
        self.assertEqual(summary['unfinished_positions'],1)
        self.assertEqual(summary['simulated_calls'],1)
        self.assertEqual(summary['completed'],0)
        atomic_json(data/'machine-review.json',{'manual_review':'preserve this'})
        run.report(root)
        self.assertEqual(run.read(data/'machine-review.json'),{'manual_review':'preserve this'})
        with self.assertRaisesRegex(Exception,'unfinished_no_auto_resume'):
            await run.run_position(root,pid,simulated=True,client=client)

    async def test_reservation_without_client_dispatch_is_not_counted_as_call(self):
        root=await self.prepared();checked=run.verify(root);pid=checked['plan']['order'][0]
        original=run.atomic_json;client=run.FixtureClient()
        def fail_dispatch(path,value):
            if Path(path).name.endswith('.dispatch.json'):raise OSError('artificial_dispatch_write_failure')
            return original(path,value)
        with patch.object(run,'atomic_json',side_effect=fail_dispatch):
            result=await run.run_position(root,pid,simulated=True,client=client)
        self.assertEqual(client.calls,[])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['simulated_calls'],0)
        self.assertEqual(result['generation_attempts'],1)

    async def test_private_client_injection_rejected_without_dispatch(self):
        root=await self.prepared();owner=run.load_trial(root);owner['private']=True
        with patch.object(run,'load_trial',return_value=owner):
            with self.assertRaisesRegex(Exception,'real_client_injection_forbidden'):
                await run.run_position(root,'unused',client=run.FixtureClient())
        self.assertFalse((root/'execution.lock').exists())


if __name__=='__main__':unittest.main()
