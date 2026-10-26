"""Regression for the consumer schema that blocked OneBot initialization."""
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.deployment import onebot_config, repair_napcat_timeout, validate_onebot_config
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.security import unprotect


class NapCatConfigTests(unittest.TestCase):
    def test_generated_timeout_matches_napcat_object_contract(self):
        value = onebot_config({"onebot_port": 6299}, "synthetic-transport-token")
        timeout = value["timeout"]
        self.assertIsInstance(timeout, dict)
        # These keys and types are required by the bundled consumer, not by the former producer.
        self.assertEqual(set(timeout), {"baseTimeout", "uploadSpeedKBps", "downloadSpeedKBps", "maxTimeout"})
        self.assertTrue(all(type(v) in (int, float) and v > 0 for v in timeout.values()))
        self.assertGreaterEqual(timeout["maxTimeout"], timeout["baseTimeout"])
        client = value["network"]["websocketClients"][0]
        self.assertEqual(client["url"], "ws://127.0.0.1:6299/ws")
        self.assertEqual(client["token"], "synthetic-transport-token")

    def test_legacy_scalar_and_missing_field_rejected_before_launch(self):
        value = onebot_config({"onebot_port": 6299}, "test")
        value["timeout"] = 30000
        with self.assertRaises(RuntimeErrorBase):
            validate_onebot_config(value)
        value["timeout"] = {"baseTimeout": 10000}
        with self.assertRaises(RuntimeErrorBase):
            validate_onebot_config(value)

    def test_in_place_repair_preserves_routes_and_makes_encrypted_backup(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="napcat-repair-", dir=base) as directory:
            paths = RuntimePaths(Path(directory))
            config = ConfigStore(paths)
            config.initialize()
            config.update({"bot_qq": "123456"})
            value = onebot_config({"onebot_port": 12345}, "test-secret-token")
            value["timeout"] = 30000
            file = paths.home / "components/napcat/config/onebot11_123456.json"
            atomic_json(file, value)
            original = file.read_text(encoding="utf-8")
            result = repair_napcat_timeout(paths)
            repaired = json.loads(file.read_text(encoding="utf-8"))
            self.assertEqual(repaired["network"], value["network"])
            validate_onebot_config(repaired)
            backup = Path(result["encrypted_backup"])
            self.assertNotIn("test-secret-token", backup.read_text(encoding="utf-8"))
            self.assertEqual(unprotect(json.loads(backup.read_text(encoding="utf-8"))["original_config"]), original)
            self.assertFalse(repair_napcat_timeout(paths)["changed"])
