"""Emergency scope and repeated sweeps, always using simulated process lists."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.emergency import force_stop_related, select_targets, select_qq_targets, force_stop_qq_processes
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

    def test_elevated_close_helper_keeps_explicit_controller(self):
        rows=[process(100,'python.exe','force_stop_services.py'),
              process(101,'pythonw.exe','pythonw -m persona_runtime gui'),
              process(102,'python.exe','python -m persona_runtime.host --role service')]
        self.assertEqual([p['pid'] for p in select_targets(rows,self.paths.home,100,protected_pids=(101,))],[102])

    def test_elevated_helper_keeps_console_venv_redirector_but_stops_services(self):
        # The UAC broker separates the helper from the protected GUI's tree.
        # Killing redirector 90 would also kill its base-interpreter child 101.
        rows = [process(100, 'python.exe', 'force_stop_services.py', parent=80),
                process(80, 'svchost.exe'),
                process(101, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=90),
                process(90, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=70),
                process(70, 'powershell.exe', 'powershell D:/QQ_Bot_2/start-manager.ps1'),
                process(102, 'python.exe', 'python -m persona_runtime.host --role service', parent=101),
                process(103, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=70),
                process(104, 'QQ.exe', parent=102)]
        actual = {p['pid'] for p in select_targets(rows, self.paths.home, 100, protected_pids=(101,))}
        self.assertEqual(actual, {102, 103, 104})

    def test_explicit_protected_ancestor_chain_handles_overlap_missing_and_cycles(self):
        rows = [process(100, 'python.exe', 'force_stop_services.py', parent=90),
                process(90, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=80),
                process(80, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=90),
                process(101, 'pythonw.exe', 'pythonw -m persona_runtime gui', parent=90),
                process(102, 'QQ.exe')]
        actual = select_targets(rows, self.paths.home, 100, protected_pids=(101, 80, 999))
        self.assertEqual([p['pid'] for p in actual], [102])

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
        with patch("persona_runtime.emergency.force_stop_all_elevated", return_value={"success": True}), \
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

    def test_qq_only_includes_external_qq_but_preserves_both_services_and_controller(self):
        rows = [process(10, "QQ.exe", executable="X:/external/QQ.exe"),
                process(11, "node.exe", 'node X:/unmanaged/napcat/napcat.mjs'),
                process(12, "python.exe", 'python -m persona_runtime.host --role napcat'),
                process(13, "worker.exe", parent=10),
                process(20, "python.exe", 'python -m persona_runtime.host --role service', parent=10),
                process(21, "python.exe", 'python -m persona_runtime.host --role astrbot'),
                process(22, "pythonw.exe", 'python -m persona_runtime gui'),
                process(23, "msedge.exe", parent=10),
                process(24, "node.exe", 'node unrelated.js'),
                process(100, "python.exe", "force_stop_qq.py")]
        self.assertEqual({p['pid'] for p in select_qq_targets(rows, self.paths.home, 100)}, {10, 11, 12, 13})

    def test_qq_only_respawn_and_permission_failure_report(self):
        live = {10: process(10, "QQ.exe"), 20: process(20, "python.exe", 'python -m persona_runtime.host --role service')}
        killed = []
        def kill(pid):
            killed.append(pid)
            live.pop(pid)
            if pid == 10:
                live[11] = process(11, "QQ.exe")
            return True
        report = force_stop_qq_processes(self.paths, snapshot=lambda: list(live.values()), kill=kill, pause=lambda _: None)
        self.assertTrue(report['success'])
        self.assertEqual(killed, [10, 11])
        self.assertIn(20, live)
        report = force_stop_qq_processes(self.paths, snapshot=lambda: [process(30, 'QQ.exe')], kill=lambda _: False, pause=lambda _: None)
        self.assertFalse(report['success'])
        self.assertEqual(report['remaining'][0]['pid'], 30)

    def test_qq_selector_uses_entrypoint_not_log_paths_or_code_strings(self):
        rows = [process(10, 'node.exe', r'node C:\tools\log-viewer.js --input D:\reports\napcat.log'),
                process(11, 'powershell.exe', r'powershell -File C:\tools\archive.ps1 -Source D:\exports\napcat'),
                process(12, 'node.exe', r'node -e "require(\"D:/napcat/napcat.mjs\")"'),
                process(13, 'powershell.exe', r'powershell -Command "Write-Output napcat.ps1"'),
                process(14, 'cmd.exe', r'cmd /c echo napcat.bat'),
                process(15, 'node.exe', r'node --require D:\napcat\napcat.mjs C:\tools\other.js'),
                process(16, 'python.exe', 'python other.py -m persona_runtime.host --role napcat'),
                process(20, 'node.exe', r'node --no-warnings "X:\External QQ\napcat.mjs" --log D:\logs\ordinary.log'),
                process(21, 'powershell.exe', r'powershell -NoProfile -ExecutionPolicy Bypass -File "X:\External QQ\napcat-shell\launcher.ps1"'),
                process(22, 'cmd.exe', r'cmd /d /c "X:\External QQ\napcat.bat"')]
        self.assertEqual({p['pid'] for p in select_qq_targets(rows, self.paths.home, 100)}, {20, 21, 22})

    def test_qq_host_role_accepts_equal_and_quoted_args_without_touching_other_roles(self):
        rows = [process(10, 'python.exe', 'python -B -m persona_runtime.host --role=napcat --instance test'),
                process(11, 'python.exe', '"C:\\Python Path\\python.exe" -B -m "persona_runtime.host" "--role" "napcat"'),
                process(12, 'python.exe', 'python -m persona_runtime.host "--role=napcat" --instance="test"'),
                process(20, 'python.exe', 'python -m persona_runtime.host --role=service --instance=napcat'),
                process(21, 'python.exe', 'python -m persona_runtime.host --role=astrbot'),
                process(22, 'pythonw.exe', 'pythonw -m persona_runtime gui --role napcat'),
                process(23, 'python.exe', 'python -m persona_runtime.host --role=napcat --role=service')]
        self.assertEqual({p['pid'] for p in select_qq_targets(rows, self.paths.home, 100)}, {10, 11, 12})

    def test_qq_emergency_never_uses_ownership_or_stops_other_roles(self):
        from persona_runtime.config import ConfigStore
        from persona_runtime.lifecycle import ServiceController
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        with patch('persona_runtime.emergency.force_stop_qq_elevated', return_value={'success': True}) as force, \
                patch.object(controller, 'owned', side_effect=AssertionError('no ownership gate')), \
                patch.object(controller, 'stop', side_effect=AssertionError('no other stop')):
            controller.force_stop_qq()
        force.assert_called_once()
        self.assertFalse(controller.force_stopping.is_set())

    def test_qq_admin_path_has_elevation_and_cancelled_uac_is_not_success(self):
        from persona_runtime.emergency import force_stop_qq_elevated
        with patch('persona_runtime.emergency.is_administrator', return_value=True), \
                patch('persona_runtime.emergency.force_stop_qq_processes', return_value={'success': True}):
            self.assertTrue(force_stop_qq_elevated(self.paths)['elevated'])
        with patch('persona_runtime.emergency.is_administrator', return_value=False), \
                patch('persona_runtime.emergency.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, '管理员授权被取消'):
                force_stop_qq_elevated(self.paths)
            import base64
            command = run.call_args.args[0]
            decoded = base64.b64decode(command[-1]).decode('utf-16-le')
            self.assertIn('-Verb RunAs', decoded)
            self.assertIn('-WindowStyle Hidden', decoded)
            self.assertIn('force_stop_qq.py', decoded)
            self.assertNotIn('force_stop_related', decoded)


if __name__ == "__main__":
    unittest.main()
