"""Local process lifecycle and optional installed AstrBot SDK checks."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from persona_runtime.config import ConfigStore
from persona_runtime.deployment import REPO, child_environment
from persona_runtime.desktop_client import LocalClient
from persona_runtime.lifecycle import ServiceController
from persona_runtime.paths import RuntimePaths

SDK_PYTHON = Path("D:/Persona-Centric Agent Runtime/components/astrbot/.venv/Scripts/python.exe")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="integration-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))

    def test_real_local_service_identity_and_shutdown(self):
        cfg = ConfigStore(self.paths)
        cfg.initialize()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        while port in (6399, 6285, 6299):
            port += 1
        cfg.update({"service_port": port})
        controller = ServiceController(self.paths)
        controller.start_console()
        try:
            state = controller.start("service")
            self.assertEqual(state["state"], "运行")
            self.assertTrue(controller.owned("service"))
            self.assertEqual(controller.start("service")["state"], "运行")
            client = LocalClient(cfg)
            persona = client.manage("persona.create")
            client.manage("persona.use", persona_id=persona["id"])
            self.assertEqual(client.manage("persona.show")["id"], persona["id"])
            self.assertEqual(client.call("GET", "/v1/health")["instance_id"], cfg.load()["instance_id"])
        finally:
            try:
                if controller.owned("service"):
                    controller.stop("service")
            finally:
                controller.end_console()
        self.assertFalse(controller.owned("service"))

    @unittest.skipUnless(SDK_PYTHON.exists(), "Run prepare to install the independent AstrBot SDK")
    def test_installed_sdk_plugin_with_no_qq_or_model_network(self):
        env = child_environment(self.paths)
        result = subprocess.run([str(SDK_PYTHON), "-B", str(REPO / "scripts/check_sdk.py"), str(self.paths.home)],
                                env=env, cwd=self.paths.home, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-3500:])
        self.assertIn('"sdk_plugin_loaded": true', result.stdout)
        self.assertIn('"model_calls": 0', result.stdout)


if __name__ == "__main__":
    unittest.main()
