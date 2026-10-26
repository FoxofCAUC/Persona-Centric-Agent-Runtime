"""Emergency scope and repeated sweeps, always using simulated process lists."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.emergency import force_stop_related, select_targets
from persona_runtime.paths import RuntimePaths


def process(pid, name, command="", executable="", parent=0):
    return {"ProcessId": pid, "Name": name, "CommandLine": command,
            "ExecutablePath": executable, "ParentProcessId": parent}


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="force-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))

    def test_orphan_and_ordinary_qq_selected_without_any_host_records(self):
        rows = [process(10, "QQ.exe", executable="D:/personal/QQ.exe"),
                process(11, "NapCatWinBootMain.exe", executable="X:/unknown/NapCatWinBootMain.exe"),
                process(12, "python.exe", 'python -B -m persona_runtime.host --role service'),
                process(13, "pythonw.exe", 'pythonw "D:/QQ_Bot_2/gui/BotManager.py"'),
                process(14, "python.exe", executable="C:/Users/demo/AppData/Roaming/uv/tools/astrbot/Scripts/python.exe"),
                process(15, "node.exe", 'node "D:/AIPoweredTRPGinQQ/AIPoweredTRPG/apps/server/dist/index.js"'),
                process(16, "worker.exe", parent=10)]
        self.assertEqual({p["pid"] for p in select_targets(rows, self.paths.home, 100)}, set(range(10, 17)))

    def test_current_controller_ancestors_and_unrelated_programs_preserved(self):
        rows = [process(100, "pythonw.exe", "pythonw -m persona_runtime gui", parent=90),
                process(90, "powershell.exe", 'powershell "D:/QQ_Bot_2/start-manager.ps1"', parent=80),
                process(80, "explorer.exe"), process(20, "python.exe", "python unrelated.py"),
                process(21, "node.exe", "node unrelated-server.js"),
                process(22, "msedge.exe", parent=10), process(10, "QQ.exe"),
                process(23, "node.exe", f'node trusted-worker.js --working-dir "{self.paths.home}"',
                        "C:/Users/demo/AppData/Local/OpenAI/Codex/runtimes/cua_node/bin/node.exe")]
        self.assertEqual([p["pid"] for p in select_targets(rows, self.paths.home, 100)], [10])

    def test_other_manager_is_stopped_but_current_manager_is_not(self):
        rows = [process(100, "pythonw.exe", "pythonw -m persona_runtime gui"),
                process(101, "pythonw.exe", "pythonw -m persona_runtime gui")]
        self.assertEqual([p["pid"] for p in select_targets(rows, self.paths.home, 100)], [101])

    def test_repeated_sweeps_catch_respawn_and_write_metadata_only_report(self):
        live = {10: process(10, "QQ.exe", "not logged secret-string")}
        killed = []
        def snapshot():
            return list(live.values())
        def kill(pid):
            killed.append(pid)
            live.pop(pid, None)
            if pid == 10:
                live[11] = process(11, "QQ.exe")
            return True
        result = force_stop_related(self.paths, snapshot=snapshot, kill=kill, pause=lambda _: None)
        self.assertEqual(killed, [10, 11])
        self.assertTrue(result["success"])
        text = Path(result["report_file"]).read_text(encoding="utf-8")
        self.assertNotIn("secret-string", text)
        self.assertEqual(json.loads(text)["remaining"], [])

    def test_permission_failure_is_reported_instead_of_success(self):
        rows = [process(10, "QQ.exe")]
        result = force_stop_related(self.paths, snapshot=lambda: rows, kill=lambda _: False, pause=lambda _: None)
        self.assertFalse(result["success"])
        self.assertEqual(result["remaining"][0]["pid"], 10)

    def test_snapshot_failure_is_not_misreported_as_empty(self):
        def broken():
            raise RuntimeError("enumeration failed")
        with self.assertRaises(RuntimeError):
            force_stop_related(self.paths, snapshot=broken, pause=lambda _: None)

    def test_emergency_does_not_require_owned_or_service_api(self):
        from persona_runtime.config import ConfigStore
        from persona_runtime.lifecycle import ServiceController
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        with patch("persona_runtime.emergency.force_stop_related", return_value={"success": True}), \
                patch.object(controller, "owned", side_effect=AssertionError("ownership must not be consulted")):
            controller.force_stop_all()
        self.assertEqual(controller.stop_generation, 1)
        self.assertFalse(controller.force_stopping.is_set())
        controller.force_stopping.set()
        with self.assertRaisesRegex(ValueError, "强制停止正在执行"):
            controller.start("service")

    def test_urgent_task_runs_even_when_normal_pool_is_full(self):
        import threading
        from persona_runtime.reused.tasks import TaskHub
        hub = TaskHub()
        release, executed = threading.Event(), threading.Event()
        try:
            for i in range(6):
                hub.start(str(i), {str(i)}, lambda _: release.wait(2))
            hub.start("force", {"emergency"}, lambda _: executed.set(), urgent=True)
            self.assertTrue(executed.wait(.5))
        finally:
            release.set()
            hub.close()


if __name__ == "__main__":
    unittest.main()
