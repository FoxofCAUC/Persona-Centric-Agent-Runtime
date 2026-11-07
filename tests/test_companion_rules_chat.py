"""Normal Workbench lifecycle for the field-consumption candidate."""
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext
from experiments import companion_c2_chat as trial
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from tests import test_companion_c2_chat as fixture


class RulesChat(fixture.C2ChatTests):
    context_rules_off = True

    def test_plain_service_and_audited_service_send_the_same_input(self):
        calls = []
        for audited in (True, False):
            if not audited:
                self.manifest = trial.prepare(self.binding, self.source, context_rules_off=True)
                self.owner, self.plan = trial.verify(self.manifest)
                self.paths = RuntimePaths(Path(self.owner['home']))
            model = fixture.FixtureClient()
            runtime = self.runtime(model)
            with nullcontext() if audited else patch.object(trial, 'install', return_value=None):
                with fixture.NetworkFence() as fence, runtime:
                    fence.port = runtime.port
                    client = LocalClient(ConfigStore(self.paths))
                    row = self.wait_turn(client, self.submit(client, '你喜欢什么样的休息方式？'))
                    self.assertEqual(row['status'], 'completed')
            calls.append(model.calls[0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(build_request(*calls[0]), build_request(*calls[1]))
