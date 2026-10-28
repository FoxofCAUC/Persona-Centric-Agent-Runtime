"""Console lifetime contracts; no real services, QQ, or models are started."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.instance import current_process_identity, process_identity_alive
from persona_runtime.lifecycle import ServiceController, require_console
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths


class ConsoleLifecycleTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="console-lifetime-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.cfg = ConfigStore(self.paths).initialize()
        self.ctl = ServiceController(self.paths)
        self.addCleanup(self.ctl.end_console)

    def lease(self, token="test-token", state="active"):
        return {"token": token, "state": state, "instance_id": self.cfg["instance_id"],
                "process": {"pid": 100, "created": 1234}}

    def write_lease(self, lease):
        atomic_json(self.paths.home / "runtime/console.json", lease)

    def test_native_identity_rejects_recycled_pid_stamp(self):
        identity = current_process_identity()  # Read-only query of this test process.
        self.assertTrue(process_identity_alive(identity))
        self.assertFalse(process_identity_alive(identity | {"created": identity["created"] + 1}))

    def test_start_rejects_missing_closed_or_wrong_identity_lease_before_spawning(self):
        with patch("persona_runtime.lifecycle.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeErrorBase, "控制窗口"):
                self.ctl.start("service")
            self.write_lease(self.lease(state="closed"))
            with self.assertRaisesRegex(RuntimeErrorBase, "控制窗口"):
                self.ctl.start("service")
            self.write_lease(self.lease())
            with patch("persona_runtime.lifecycle.process_identity_alive", return_value=False):
                with self.assertRaisesRegex(RuntimeErrorBase, "控制窗口"):
                    self.ctl.start("service")
            spawn.assert_not_called()

    def test_console_end_invalidates_lease_and_releases_lock_for_next_console(self):
        with patch("persona_runtime.lifecycle.current_process_identity", return_value={"pid": 100, "created": 1234}), \
                patch("persona_runtime.lifecycle.process_identity_alive", return_value=True):
            first = self.ctl.start_console()
            self.assertEqual(require_console(self.paths)["token"], first["token"])
            self.ctl.end_console();self.ctl.end_console()
            with self.assertRaises(RuntimeErrorBase):
                require_console(self.paths)
            second = self.ctl.start_console()
            self.assertNotEqual(second["token"], first["token"])
            self.assertFalse(second["previous_stale"])

    def test_stale_console_record_is_replaced_without_trusting_recycled_pid(self):
        self.write_lease(self.lease())
        with patch("persona_runtime.lifecycle.current_process_identity", return_value={"pid": 100, "created": 5678}), \
                patch("persona_runtime.lifecycle.process_identity_alive", return_value=False):
            fresh = self.ctl.start_console()
        self.assertTrue(fresh["previous_stale"])
        self.assertEqual(fresh["process"]["created"], 5678)

    def test_second_console_rejected_and_old_end_cannot_close_replacement(self):
        with patch("persona_runtime.lifecycle.current_process_identity", return_value={"pid": 100, "created": 1234}), \
                patch("persona_runtime.lifecycle.process_identity_alive", return_value=True):
            self.ctl.start_console()
            other = ServiceController(self.paths)
            with self.assertRaises(RuntimeErrorBase):
                other.start_console()
            replacement = self.lease(token="replacement")
            self.write_lease(replacement)
            self.ctl.end_console()
            self.assertEqual(require_console(self.paths)["token"], "replacement")

    def test_start_passes_console_token_and_rechecks_it_before_spawn(self):
        lease = self.lease()
        with patch("persona_runtime.lifecycle.require_console", return_value=lease) as require, \
                patch.object(self.ctl, "health", side_effect=[{"state": "已停止", "owned": False, "port": 1}, {"state": "运行"}]), \
                patch("persona_runtime.lifecycle.port_open", return_value=False), \
                patch("persona_runtime.lifecycle.time.sleep"), \
                patch("persona_runtime.lifecycle.subprocess.Popen") as spawn:
            self.ctl.start("service")
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index("--console-token") + 1], lease["token"])
        self.assertEqual(require.call_count, 2)
        self.assertEqual(require.call_args.args, (self.paths, lease["token"]))

    def test_dead_host_live_qq_record_is_detected_for_startup_cleanup(self):
        record = {"instance_id": self.cfg["instance_id"], "host": {"ProcessId": 1}, "qq_listener": {"ProcessId": 2}}
        with patch.object(self.ctl, "record", side_effect=lambda role: record if role == "napcat" else None), \
                patch("persona_runtime.lifecycle.same_process", side_effect=lambda saved: bool(saved and saved["ProcessId"] == 2)):
            self.assertTrue(self.ctl.has_resident_processes())
        record["instance_id"] = "different-project"
        with patch.object(self.ctl, "record", return_value=record), patch("persona_runtime.lifecycle.same_process") as same:
            self.assertFalse(self.ctl.has_resident_processes());same.assert_not_called()

    def test_watchdog_exits_on_closed_replaced_or_dead_console(self):
        from persona_runtime.host import start_console_watchdog
        lease = self.lease()
        for current, alive in ((self.lease(state="closed"), True), (self.lease(token="new"), True), (lease, False)):
            with self.subTest(current=current["state"], token=current["token"], alive=alive):
                owner = Mock();owner.alive.return_value = alive
                exited = threading.Event()
                with patch("persona_runtime.host.require_console", return_value=lease), \
                        patch("persona_runtime.host.ProcessIdentityHandle", return_value=owner), \
                        patch("persona_runtime.host.read_console", return_value=current):
                    finished, thread = start_console_watchdog(self.paths, lease["token"], exit_process=lambda code: exited.set(), interval=.001)
                    self.assertTrue(exited.wait(1))
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                owner.close.assert_called_once()

    def test_watchdog_monitor_failure_cannot_leave_services_resident(self):
        from persona_runtime.host import start_console_watchdog
        lease = self.lease();owner = Mock();exited = threading.Event();codes = []
        def exit_process(code):
            codes.append(code);exited.set()
        with patch("persona_runtime.host.require_console", return_value=lease), \
                patch("persona_runtime.host.ProcessIdentityHandle", return_value=owner), \
                patch("persona_runtime.host.read_console", side_effect=OSError("unreadable")):
            _, thread = start_console_watchdog(self.paths, lease["token"], exit_process=exit_process, interval=.001)
            self.assertTrue(exited.wait(1));thread.join(1)
        self.assertEqual(codes, [1]);owner.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
