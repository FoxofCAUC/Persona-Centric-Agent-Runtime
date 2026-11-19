"""The existing environment can add WebView2 without replacing business data."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.instance import InstanceLock
from persona_runtime.installation import ensure_webview
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


class WorkbenchInstallationTests(unittest.TestCase):
    def setUp(self):
        parent = RuntimePaths.from_environment().home / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.paths = RuntimePaths(Path(temporary.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.store = Store(self.paths)
        self.store.initialize()
        self.python = self.paths.home / ".venv/Scripts/python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.touch()

    def test_ready_dependency_skips_install_even_while_running(self):
        with patch("persona_runtime.installation.webview_ready", return_value=True), \
                patch("persona_runtime.installation.provisioning_environment") as provision:
            lock = InstanceLock(self.paths.home / "runtime/desktop.lock")
            try:
                self.assertFalse(ensure_webview(self.paths)["dependencies_changed"])
                provision.assert_not_called()
            finally:
                lock.close()

    def test_missing_dependency_waits_for_frontend_to_exit(self):
        lock = InstanceLock(self.paths.home / "runtime/desktop.lock")
        try:
            with patch("persona_runtime.installation.webview_ready", return_value=False), \
                    patch("persona_runtime.installation.provisioning_environment") as provision, \
                    self.assertRaisesRegex(ValueError, "请先正常退出"):
                ensure_webview(self.paths)
            provision.assert_not_called()
        finally:
            lock.close()

    def test_install_uses_lock_and_preserves_database_and_configuration(self):
        config = self.config.file.read_bytes()
        database = self.store.path.read_bytes()
        with patch("persona_runtime.installation.webview_ready", side_effect=[False, False, True]), \
                patch("persona_runtime.installation.provisioning_environment", return_value={}), \
                patch("persona_runtime.installation.shutil.which", return_value="uv"), \
                patch("persona_runtime.installation.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(ensure_webview(self.paths)["dependencies_changed"])
        args = run.call_args.args[0]
        self.assertIn("--frozen", args)
        self.assertIn("--inexact", args)
        self.assertIn("workbench-webview", args)
        self.assertEqual(self.config.file.read_bytes(), config)
        self.assertEqual(self.store.path.read_bytes(), database)
        lock = InstanceLock(self.paths.home / "runtime/desktop.lock")
        lock.close()

    def test_external_interpreter_is_never_modified(self):
        alternative = self.paths.home / "external/python.exe"
        alternative.parent.mkdir()
        alternative.touch()
        with patch("persona_runtime.installation.runtime_python", return_value=alternative), \
                patch("persona_runtime.installation.webview_ready", return_value=False), \
                patch("persona_runtime.installation.provisioning_environment") as provision, \
                self.assertRaisesRegex(ValueError, "当前指定的 Python"):
            ensure_webview(self.paths)
        provision.assert_not_called()
