"""One explicit same-day reopening, using only artificial data and local mocks."""
import unittest
from unittest.mock import patch

from experiments import companion_c2_chat as trial
from experiments import companion_c2_lifecycle as lifecycle
from experiments import companion_workbench_trial as workbench
from experiments.companion_workbench_check import NetworkFence
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.provider_compat import build_request
from persona_runtime.paths import RuntimePaths
from pathlib import Path
from persona_runtime.storage import Store
from tests import test_companion_c2_chat as fixture

FixtureClient=fixture.FixtureClient


class LifecycleTests(unittest.TestCase):
    integration=True
    setUp=fixture.C2ChatTests.setUp
    submit=fixture.C2ChatTests.submit
    wait_turn=fixture.C2ChatTests.wait_turn

    def runtime(self, model, reopen=False):
        return workbench.TrialRuntime(trial_manifest=self.manifest,client=model,reopen=reopen)

    def segment(self, model, text, reopen=False):
        runtime=self.runtime(model,reopen)
        before=len(model.calls)
        with NetworkFence() as fence,runtime:
            fence.port=runtime.port
            self.assertEqual(len(model.calls),before)
            client=LocalClient(ConfigStore(self.paths))
            row=self.wait_turn(client,self.submit(client,text))
            self.assertEqual(row['status'],'completed',row['error_code'])
        self.assertFalse(runtime.thread.is_alive())
        return row

    def state(self):
        return trial.read(trial.body(self.owner)/'lifecycle.json')

    def test_two_openings_keep_history_without_replaying_success(self):
        model=FixtureClient()
        first=self.segment(model,'First artificial segment.')
        self.assertEqual(self.state()['status'],'checkpoint_ready')
        before=lifecycle.snapshot(self.owner)
        with self.assertRaisesRegex(RuntimeErrorBase,'explicit_reopen_required'):
            with self.runtime(model):pass
        self.assertEqual(lifecycle.snapshot(self.owner),before)
        second=self.segment(model,'Second artificial segment.',reopen=True)
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(first['session_id'],second['session_id'])
        self.assertEqual(len(model.calls),2)
        self.assertIn('First artificial segment.',str(model.calls[1][1]))
        self.assertEqual(self.state()['status'],'finished')
        for opening in (1,2):
            receipt=trial.read(trial.body(self.owner)/f'opening-{opening}-ready.json')
            self.assertEqual(receipt['startup_generation_calls'],0)
        with self.assertRaisesRegex(RuntimeErrorBase,'checkpoint_not_available'):
            with self.runtime(model,True):pass
        self.assertEqual(len(model.calls),2)

    def test_changed_day_or_history_refuses_before_start(self):
        model=FixtureClient();self.segment(model,'Artificial checkpoint.')
        with patch.object(lifecycle,'local_day',return_value='2099-01-01'):
            with self.assertRaisesRegex(RuntimeErrorBase,'different_day'):
                with self.runtime(model,True):pass
        with Store(self.paths).connection(write=True) as db:
            db.execute("UPDATE events SET text='Changed artificial history' WHERE text IS NOT NULL")
        with self.assertRaisesRegex(RuntimeErrorBase,'history_or_ledger_changed'):
            with self.runtime(model,True):pass
        self.assertEqual(len(model.calls),1)

    def test_model_drift_and_unfinished_lifecycle_refuse_reopen(self):
        model=FixtureClient();self.segment(model,'Artificial closed turn.')
        path=trial.body(self.owner)/'lifecycle.json'
        atomic_json(path,{**self.state(),'status':'running'})
        with self.assertRaisesRegex(RuntimeErrorBase,'checkpoint_not_available'):
            with self.runtime(model,True):pass
        ConfigStore(self.paths).update({'model':{'name':'changed-model'}})
        with self.assertRaises(RuntimeErrorBase):
            with self.runtime(model,True):pass
        self.assertEqual(len(model.calls),1)

    def test_missing_response_receipt_blocks_clean_checkpoint(self):
        model=FixtureClient();runtime=self.runtime(model)
        with self.assertRaisesRegex(RuntimeErrorBase,'response_missing'):
            with NetworkFence() as fence,runtime:
                fence.port=runtime.port
                row=self.wait_turn(LocalClient(ConfigStore(self.paths)),self.submit(LocalClient(ConfigStore(self.paths)),'Receipt test.'))
                folder=trial.body(self.owner)/'turns'/row['id']
                next(folder.glob('*.response.json')).unlink()
        self.assertEqual(self.state()['status'],'blocked')
        self.assertEqual(len(model.calls),1)

    def test_checkpoint_write_failure_blocks_reopen(self):
        model=FixtureClient();original=lifecycle.atomic_json
        def write(path,value):
            if path.name=='opening-1-closed.json':raise OSError('artificial checkpoint failure')
            return original(path,value)
        with patch.object(lifecycle,'atomic_json',side_effect=write):
            with self.assertRaisesRegex(OSError,'checkpoint failure'):
                self.segment(model,'Artificial persistence test.')
        self.assertEqual(self.state()['status'],'blocked')
        with self.assertRaisesRegex(RuntimeErrorBase,'checkpoint_not_available'):
            with self.runtime(model,True):pass
        self.assertEqual(len(model.calls),1)

    def test_retry_recovery_is_auditable_and_not_replayed(self):
        model=FixtureClient(failures=1)
        async def no_delay(seconds):self.assertEqual(seconds,1)
        with patch('persona_runtime.generation_retry.retry_delay',no_delay):
            self.segment(model,'Artificial retry first half.')
        self.segment(model,'Artificial second half.',True)
        self.assertEqual(len(model.calls),3)
        self.assertEqual(model.calls[0][1],model.calls[1][1])

    def test_old_once_only_manifest_rejects_new_reopen_option(self):
        old=trial.prepare(self.binding,self.source)
        with self.assertRaisesRegex(RuntimeErrorBase,'legacy_reopen_forbidden'):
            with workbench.TrialRuntime(trial_manifest=old,client=FixtureClient(),reopen=True):pass

    def test_normal_entry_without_trial_hooks_has_identical_messages_and_wire(self):
        audited=FixtureClient();self.segment(audited,'Same artificial standalone input.')
        self.manifest=trial.prepare(self.binding,self.source)
        self.owner,self.plan=trial.verify(self.manifest)
        self.paths=RuntimePaths(Path(self.owner['home']))
        normal=FixtureClient()
        with patch.object(trial,'install',return_value=None):
            self.segment(normal,'Same artificial standalone input.')
        self.assertEqual(normal.calls[0][1],audited.calls[0][1])
        self.assertEqual(build_request(*normal.calls[0]),build_request(*audited.calls[0]))

    def test_retry_exhaustion_blocks_checkpoint_and_old_success_is_not_replayed(self):
        model=FixtureClient(failures=4);runtime=self.runtime(model)
        delays=[]
        async def no_delay(seconds):delays.append(seconds)
        with patch('persona_runtime.generation_retry.retry_delay',no_delay):
            with self.assertRaisesRegex(RuntimeErrorBase,'request_requires_review'):
                with NetworkFence() as fence,runtime:
                    fence.port=runtime.port
                    client=LocalClient(ConfigStore(self.paths))
                    row=self.wait_turn(client,self.submit(client,'Artificial exhaustion.'))
                    self.assertEqual(row['status'],'failed')
        self.assertEqual(delays,[1,2,4]);self.assertEqual(len(model.calls),4)
        self.assertEqual(self.state()['status'],'blocked')
        with self.assertRaises(RuntimeErrorBase):
            with self.runtime(model,True):pass
        self.assertEqual(len(model.calls),4)


if __name__=='__main__':unittest.main()
