"""Targeted regression checks for the opt-in desktop preview."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from persona_runtime.__main__ import parser
from persona_runtime.config import atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.workbench_instance import acquire_or_wake, next_wakeup
from persona_runtime.workbench_webview import WebViewHost, local_document_matches, workbench_html


class BrowserWorkbenchTests(unittest.TestCase):
    def make_host(self):
        controller = Mock()
        controller.paths = SimpleNamespace(home=Path.cwd())
        controller.console_lease = {"token": "current-lease"}
        controller.closing.is_set.return_value = False
        return WebViewHost(controller)

    def test_webview_is_an_explicit_opt_in(self):
        cli = parser()
        self.assertEqual(cli.parse_args(["gui"]).frontend, "tk")
        self.assertEqual(cli.parse_args(["gui", "--frontend", "webview"]).frontend, "webview")
        self.assertEqual(cli.parse_args(["prepare", "--frontend", "webview"]).frontend, "webview")

    def test_unconfirmed_release_schedules_a_recheck_without_a_second_click(self):
        host=self.make_host();host.closing=True
        host.finish_close(False)
        self.assertTrue(host.closing)
        self.assertFalse(host.closed)
        self.assertEqual(host.queue.get_nowait(),("retry_exit",(5,"component_ownership_or_release_unconfirmed")))

    def test_renderer_fallback_does_not_start_services(self):
        host = self.make_host()
        self.assertFalse(host.check_renderer("mshtml"))
        self.assertIsNotNone(host.failure)
        self.assertIsNone(host.runtime)
        host.controller.start.assert_not_called()

    def test_local_page_rejects_non_local_navigation(self):
        encoded = "QUJD"
        self.assertTrue(local_document_matches("about:blank#help", encoded))
        self.assertTrue(local_document_matches("data:text/html;charset=utf-8;base64,QUJD", encoded))
        self.assertFalse(local_document_matches("data:text/html;base64,REVG", encoded))
        self.assertFalse(local_document_matches("https://example.invalid", encoded))

    def test_status_bridge_returns_only_the_label(self):
        host = self.make_host()
        host.runtime = Mock()
        host.controller.health.return_value = {"state": "运行", "diagnostic": "INTERNAL-METADATA"}
        self.assertEqual(host.controls.runtime_status(), {"label": "本机人格服务已运行。"})
        self.assertNotIn("controller", dir(host.controls))

    def test_page_preserves_network_isolation_and_loads_desktop_controls(self):
        html = workbench_html()
        self.assertIn("connect-src 'none'", html)
        self.assertIn('id="app-menu"', html)
        self.assertIn("window.pywebview.api.exit_workbench()", html)
        self.assertLess(html.index("window.pcarDesktop ="), html.rindex("</body>"))

    def test_alternate_frontend_wakes_existing_owner_instead_of_acquiring(self):
        parent = RuntimePaths.from_environment().home / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            paths = RuntimePaths(Path(temporary))
            owner = InstanceLock(paths.home / "runtime/desktop.lock")
            try:
                with patch("persona_runtime.workbench_instance.require_console",
                           return_value={"token": "current-lease"}) as verify:
                    self.assertIsNone(acquire_or_wake(paths))
                    verify.assert_called_once_with(paths)
                wake = next_wakeup(paths, "current-lease", None)
                self.assertIsNotNone(wake)
                self.assertEqual(wake[1], "show")
                self.assertIsNone(next_wakeup(paths, "current-lease", wake[0]))
                self.assertIsNone(next_wakeup(paths, "old-lease", None))
                atomic_json(paths.home / "runtime/wakeup.json", {
                    "token": "current-lease", "id": "shutdown-id", "action": "exit",
                })
                self.assertEqual(next_wakeup(paths, "current-lease", wake[0]), ("shutdown-id", "exit"))
            finally:
                owner.close()


if __name__ == "__main__":
    unittest.main()
