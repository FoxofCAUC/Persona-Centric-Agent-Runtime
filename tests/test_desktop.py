"""Tk layout and responsive-task checks, without model calls or QQ startup."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from persona_runtime.desktop import Workbench
from persona_runtime.paths import RuntimePaths
from persona_runtime.lifecycle import ServiceController


class DesktopTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))

    def test_six_pages_at_three_scales_and_responsive_callbacks(self):
        window = Workbench(self.paths)
        self.addCleanup(lambda: window.close_window() if not window.closed else None)
        # No OS process scans or HTTP during component rendering checks.
        with patch.object(window, "poll"), patch.object(window, "refresh_personas"), patch.object(window, "read_logs"):
            for scale in (1, 1.25, 1.5):
                window.tk.call("tk", "scaling", 96/72*scale)
                window.geometry("1000x700")
                for key in window.pages:
                    window.show_page(key)
                    window.update()
                    window.update_idletasks()
                    self.assertGreater(window.pages[key].body.winfo_width(), 500)
                    for button, _ in window.buttons:
                        parent = button.master
                        if getattr(parent, "flow_buttons", False) and button.winfo_ismapped():
                            self.assertLessEqual(button.winfo_x()+button.winfo_width(), parent.winfo_width()+2,
                                                 f"{scale}: {key}: {button.cget('text')}")
            marker = []
            window.run_task("slow simulated action", {"slow"}, lambda _: time.sleep(.4))
            window.after(50, lambda: marker.append(True))
            deadline = time.monotonic()+.2
            while time.monotonic() < deadline:
                window.update()
                time.sleep(.005)
            self.assertTrue(marker, "A slow background operation must not block Tk callbacks")

    def test_external_service_and_stale_pid_are_never_stopped(self):
        from persona_runtime.config import ConfigStore
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        with patch.object(controller, "owned", return_value=False), patch.object(controller, "health", return_value={"state": "外部程序占用"}):
            with self.assertRaises(ValueError):
                controller.stop("service")
        saved = {"instance_id": controller.config.load()["instance_id"], "host": {"ProcessId": 123, "Created": "old", "ExecutablePath": "python.exe"}}
        with patch.object(controller, "record", return_value=saved), patch("persona_runtime.lifecycle.process_info", return_value={"ProcessId": 123, "Created": "new", "ExecutablePath": "python.exe"}):
            self.assertFalse(controller.owned("service"))

    def test_dashboard_opens_with_stale_bridge_heartbeat_but_checks_listener(self):
        from persona_runtime.config import ConfigStore
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        identity = {"ProcessId": 123, "Created": "current", "ExecutablePath": "python.exe"}
        with patch.object(controller, "owned", return_value=True), \
             patch.object(controller, "record", return_value={"host": identity}), \
             patch.object(controller, "health", return_value={"state": "等待验证", "owned": True}) as health, \
             patch("persona_runtime.lifecycle.listener", return_value=identity) as listening:
            self.assertEqual(controller.management_url("astrbot"), "http://127.0.0.1:6285")
            health.assert_not_called()
            listening.return_value = identity | {"Created": "reused-pid"}
            with self.assertRaisesRegex(ValueError, "其他进程"):
                controller.management_url("astrbot")
            listening.return_value = None
            with self.assertRaisesRegex(ValueError, "尚未监听"):
                controller.management_url("astrbot")
        with patch.object(controller, "owned", return_value=False), \
             patch("persona_runtime.lifecycle.listener") as listening:
            with self.assertRaisesRegex(ValueError, "属于本项目"):
                controller.management_url("astrbot")
            listening.assert_not_called()

    def test_management_button_reports_browser_failure_and_runs_in_worker(self):
        window = Mock()
        window.controller.management_url.return_value = "http://127.0.0.1:6285"
        Workbench.open_panel(window, "astrbot")
        action = window.run_task.call_args.args[2]
        with patch("persona_runtime.desktop.webbrowser.open", return_value=True) as browser:
            action(None)
            browser.assert_called_once_with("http://127.0.0.1:6285")
        with patch("persona_runtime.desktop.webbrowser.open", return_value=False):
            with self.assertRaisesRegex(ValueError, "未接受"):
                action(None)
        with patch("persona_runtime.desktop.webbrowser.open", side_effect=OSError("sensitive detail")):
            with self.assertRaisesRegex(ValueError, "无法启动默认浏览器") as error:
                action(None)
            self.assertNotIn("sensitive", str(error.exception))

    def test_force_stop_is_available_on_both_pages_and_uses_urgent_lane(self):
        window = Workbench(self.paths)
        self.addCleanup(lambda: window.close_window() if not window.closed else None)
        force_buttons = [button for button, _ in window.buttons if button.cget("text") == "强制停止全部相关程序"]
        self.assertEqual(len(force_buttons), 2)
        with patch("persona_runtime.desktop.messagebox.askyesno", return_value=True), patch.object(window, "run_task") as task:
            window.force_stop_everything()
            self.assertTrue(task.call_args.kwargs["urgent"])
            self.assertEqual(task.call_args.args[1], {"emergency"})
            with patch.object(window.controller, "force_stop_all", return_value={"success": True}) as force:
                task.call_args.args[2](None)
                force.assert_called_once()


if __name__ == "__main__":
    unittest.main()
