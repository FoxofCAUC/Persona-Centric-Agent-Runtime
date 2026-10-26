"""Tk layout and responsive-task checks, without model calls or QQ startup."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
