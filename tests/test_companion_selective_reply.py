"""v9 real consumer and isolated comparison failure behavior; no supplier access."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_selective_reply as run
from persona_runtime.companion import choose, checked, read, GENERATION_PROFILE
from persona_runtime.companion_im import GUIDANCE as OLD_GUIDANCE
from persona_runtime.companion_selective_reply import GUIDANCE, INSTRUCTION_VERSION
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from tests.prompt_baseline_fixture import capture
from tests.test_companion_im_chat import builder
from tests import test_companion_c2_chat as fixture
from experiments import companion_c2_chat as trial


class Profiles(unittest.TestCase):
    def test_actual_input_changes_only_registered_paragraph(self):
        case = {'mode':'context_full','expression':3}
        old = capture(case,builder('minimal_im'))
        diag = []
        new = capture(case,builder('minimal_im_selective',diag))
        self.assertNotIn('error_code',new)
        restored = json.loads(json.dumps(new))
        restored['messages'][0]['content'] = restored['messages'][0]['content'].replace(GUIDANCE,OLD_GUIDANCE,1)
        self.assertEqual(restored['messages'],old['messages'])
        self.assertEqual(new['messages'][0]['content'].count(GUIDANCE),1)
        self.assertNotIn(OLD_GUIDANCE,new['messages'][0]['content'])
        self.assertEqual(old['details']['character'],new['details']['character'])
        self.assertFalse(new['details']['character']['example_ids'])
        self.assertEqual(diag[0]['expression_renderer'],INSTRUCTION_VERSION)
        self.assertEqual(choose('minimal_im')['version'],'companion-runtime-v8')

    def test_unknown_mismatch_and_old_service_fail_before_writes(self):
        profile = choose('minimal_im_selective')
        for changed in (profile|{'instruction_version':'companion-im-instruction-v1'}, profile|{'version':'unknown'}):
            with self.assertRaises(RuntimeErrorBase): checked(changed)
        with self.assertRaisesRegex(RuntimeErrorBase,'snapshot_mismatch'):
            read({'model_snapshot':{'_companion_profile':profile,'_generation_profile':GENERATION_PROFILE},
                  'context_policy_snapshot':{'companion':choose('minimal_im')}})
        client = LocalClient(ConfigStore(RuntimePaths(DEFAULT_HOME/'tmp/unused-selective-client')))
        with patch.object(client,'call',return_value={'capabilities':['companion_im_text_v1']}) as call:
            with self.assertRaisesRegex(RuntimeErrorBase,'runtime_upgrade_required'):
                client.update_companion_policy(companion_mode='minimal_im_selective')
            self.assertEqual(call.call_count,1)


class Workbench(fixture.C2ChatTests):
    model_name = 'deepseek-flash'

    def setUp(self):
        super().setUp()
        self.manifest = trial.prepare(self.binding,self.source,selective=True)
        self.owner,self.plan = trial.verify(self.manifest)
        self.paths = RuntimePaths(Path(self.owner['home']))
        self.companion_mode = 'minimal_im_selective'

    def runtime(self, model):
        model.native = True
        return super().runtime(model)

    def test_actual_service_without_audit_uses_v9(self):
        model = fixture.FixtureClient()
        runtime = self.runtime(model)
        with patch.object(trial,'install',return_value=None), fixture.NetworkFence() as fence, runtime:
            fence.port = runtime.port
            client = LocalClient(ConfigStore(self.paths))
            client.require_capability('companion_selective_reply_v1')
            self.assertEqual(model.calls,[])
            row = self.wait_turn(client,self.submit(client,'人工闲聊'))
            self.assertEqual(row['status'],'completed')
        self.assertIn(GUIDANCE,model.calls[0][1][0]['content'])


class Desktop(unittest.TestCase):
    from tests import test_companion_workbench_desktop as ui
    setUp = ui.CompanionDesktopTests.setUp
    window = ui.CompanionDesktopTests.window
    ready = ui.CompanionDesktopTests.ready
    doCleanups = ui.CompanionDesktopTests.doCleanups

    def test_v9_is_recognized_and_button_stays_v8(self):
        value=self.ui.policy(True)
        value['companion']=choose('minimal_im_selective')
        data=json.loads(value['data_json']);data['companion']=value['companion']
        value.update(data_json=json.dumps(data),reply_mode='context_full')
        window=self.ready(value);window.remember_companion_policy(value)
        self.assertIn('v9',window.chat_mode_text.get())
        self.assertEqual(str(window.ctx_character_combo.cget('state')),'disabled')
        self.assertEqual(str(window.ctx_companion_enable.cget('state')),'disabled')
        self.assertEqual(window.ctx_companion_enable.cget('text'),'已启用伴侣聊天')


class Comparison(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME/'tmp',prefix='selective-unit-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.out = self.home/'evidence'
        self.enterContext(patch.object(run,'TEMP',self.home/'tmp/owned'))
        cfg = ConfigStore(RuntimePaths(self.home));cfg.initialize()
        cfg.update({'model':{'name':'deepseek-flash','base_url':'https://offline.invalid/v1','hourly_limit':0}})
        run.write(self.out/'opening.json',{'old_evidence_hashes':{}})
        await run.prepare(self.out,self.home,fixture=True)

    async def test_twelve_simulated_positions_only_registered_difference(self):
        result = await run.execute(self.out,simulated=True)
        self.assertEqual((result['completed'],result['simulated_calls'],result['physical_calls']),(12,12,0))
        self.assertEqual(len(run.pair_check(self.out,'simulation')),6)
        with self.assertRaisesRegex(Exception,'no_automatic_resume'):
            await run.execute(self.out,simulated=True)

    async def test_native_recovery_and_success_not_replayed(self):
        class Once(run.shared.SimulationClient):
            count = 0
            async def complete(self,model,messages):
                self.count+=1
                if self.count==1: raise RuntimeErrorBase('provider_network_error')
                return await super().complete(model,messages)
        client = Once(); delays=[]
        async def sleep(n): delays.append(n)
        with patch('persona_runtime.generation_retry.retry_delay',sleep):
            result = await run.execute(self.out,simulated=True,client=client)
        self.assertEqual((result['simulated_calls'],client.count,delays),(13,13,[1]))

    async def test_exhaustion_keeps_four_terminal_receipts(self):
        class Failed:
            async def complete(self,*args): raise RuntimeErrorBase('provider_network_error')
        delays=[]
        async def sleep(n): delays.append(n)
        with patch('persona_runtime.generation_retry.retry_delay',sleep):
            with self.assertRaisesRegex(Exception,'provider_retries_exhausted'):
                await run.execute(self.out,simulated=True,client=Failed())
        folder=self.out/'simulation/positions/R01-v8'
        self.assertEqual(len(run.calls(folder)),4)
        self.assertEqual(delays,[1,2,4])
        owner,_=run.verify(self.out)
        self.assertFalse(any(run.shared.idle(RuntimePaths(Path(owner['temporary_root'])/'simulation')).values()))

    async def test_reservation_failure_never_sends_and_terminal_unknown_never_resumes(self):
        original=run.write
        class Count(run.shared.SimulationClient):
            count=0
            async def complete(self,*args): self.count+=1;return await super().complete(*args)
        client=Count()
        def fail(path,value):
            if Path(path).name=='reserved.json': raise OSError('write_failed')
            return original(path,value)
        with patch.object(run,'write',fail):
            with self.assertRaises(Exception): await run.execute(self.out,simulated=True,client=client)
        self.assertEqual(client.count,0)
        with self.assertRaisesRegex(Exception,'no_automatic_resume'):
            await run.execute(self.out,simulated=True,client=client)

    async def test_missing_terminal_receipt_stops_and_cannot_replay(self):
        original=run.write
        class Count(run.shared.SimulationClient):
            count=0
            async def complete(self,*args): self.count+=1;return await super().complete(*args)
        client=Count()
        def fail(path,value):
            if Path(path).name=='terminal.json': raise OSError('terminal_write_failed')
            return original(path,value)
        with patch.object(run,'write',fail):
            with self.assertRaises(Exception): await run.execute(self.out,simulated=True,client=client)
        self.assertEqual(client.count,1)
        with self.assertRaisesRegex(Exception,'unknown_receipt'):
            run.calls(self.out/'simulation/positions/R01-v8')
        with self.assertRaisesRegex(Exception,'no_automatic_resume'):
            await run.execute(self.out,simulated=True,client=client)

    async def test_cancel_persists_terminal_and_releases_runtime(self):
        entered=asyncio.Event()
        class Waiting:
            async def complete(self,*args):
                entered.set();await asyncio.Event().wait()
        task=asyncio.create_task(run.execute(self.out,simulated=True,client=Waiting()))
        await asyncio.wait_for(entered.wait(),15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        receipts=run.calls(self.out/'simulation/positions/R01-v8')
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['status'],'interrupted')
        owner,_=run.verify(self.out)
        self.assertFalse(any(run.shared.idle(RuntimePaths(Path(owner['temporary_root'])/'simulation')).values()))
