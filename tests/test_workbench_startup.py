"""Startup errors remain visible under pythonw; no real UI/services/elevation."""
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from persona_runtime.desktop import run
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.workbench_errors import report_failure


class WorkbenchStartupTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='workbench-startup-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = SimpleNamespace(home=Path(self.temp.name), initialize=Mock())

    def errors(self):
        return [json.loads(p.read_text(encoding='utf-8'))
                for p in (self.paths.home / 'logs').glob('workbench-error-*.json')]

    def environment(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.show = stack.enter_context(patch('persona_runtime.workbench_errors.show_error', return_value=True))
        self.lock_type = stack.enter_context(patch('persona_runtime.desktop.InstanceLock'))
        self.lock = self.lock_type.return_value
        self.controller_type = stack.enter_context(patch('persona_runtime.desktop.ServiceController'))
        self.controller = self.controller_type.return_value
        self.controller.start_console.return_value = {'previous_stale': False}
        self.controller.has_resident_processes.return_value = False
        self.window_type = stack.enter_context(patch('persona_runtime.desktop.Workbench'))
        self.window = self.window_type.return_value
        self.window.shutdown_started = True
        self.force = stack.enter_context(patch('persona_runtime.emergency.force_stop_all_elevated', return_value={'success': True}))
        self.exit_process = stack.enter_context(patch('os._exit'))

    def test_diagnostic_omits_exception_text_locals_and_source_lines(self):
        private = 'SECRET_TEST_ONLY_CREDENTIAL_AND_CHAT_TEXT'
        try:
            raise ValueError(private)
        except ValueError as error:
            with patch('persona_runtime.workbench_errors.show_error', return_value=True) as show:
                result = report_failure(self.paths, error, 'create_workbench')
        raw = Path(result['path']).read_text(encoding='utf-8')
        self.assertNotIn(private, raw)
        self.assertNotIn(private, show.call_args.args[0])
        record = json.loads(raw)
        self.assertEqual(record['error_type'], 'ValueError')
        self.assertEqual(set(record['frames'][0]), {'file', 'line'})
        self.assertEqual(record['frames'][0]['file'], 'test_workbench_startup.py')
        self.assertGreater(record['frames'][0]['line'], 0)
        self.assertIn(result['path'], show.call_args.args[0])
        self.assertTrue(result['shown'])

    def test_unwritable_log_still_notifies_without_system_drive_fallback(self):
        with patch('persona_runtime.workbench_errors.atomic_json', side_effect=OSError('PRIVATE_IO_PATH')), \
                patch('persona_runtime.workbench_errors.show_error', return_value=True) as show:
            result = report_failure(self.paths, OSError('PRIVATE_IO_PATH'), 'initialize_paths')
        self.assertFalse(result['saved'])
        self.assertTrue(result['shown'])
        self.assertIn('未能写入', show.call_args.args[0])
        self.assertNotIn('PRIVATE_IO_PATH', show.call_args.args[0])
        self.assertEqual(Path(result['path']).parent, self.paths.home / 'logs')

    def test_notification_failure_is_recorded_without_hiding_original_error(self):
        with patch('persona_runtime.workbench_errors.show_error', side_effect=OSError('private-display-error')):
            result = report_failure(self.paths, RuntimeError('private-main-error'), 'mainloop')
        self.assertTrue(result['saved'])
        self.assertFalse(result['shown'])
        record = self.errors()[0]
        self.assertTrue(record['notification_failed'])
        self.assertNotIn('private-', json.dumps(record))

    def test_directory_failure_before_lock_is_visible(self):
        self.environment()
        self.paths.initialize.side_effect = OSError('private disk detail')
        with self.assertRaises(OSError):
            run(self.paths)
        self.lock_type.assert_not_called()
        self.controller_type.assert_not_called()
        self.force.assert_not_called()
        self.assertEqual(self.errors()[0]['stage'], 'initialize_paths')
        self.show.assert_called_once()

    def test_instance_lock_conflict_does_not_close_existing_console(self):
        self.environment()
        self.lock_type.side_effect = RuntimeErrorBase('already running private details')
        with self.assertRaises(RuntimeErrorBase):
            run(self.paths)
        self.controller_type.assert_not_called()
        self.force.assert_not_called()
        self.window_type.assert_not_called()
        self.assertEqual(self.errors()[0]['stage'], 'instance_lock')
        self.assertIn('实例锁', self.show.call_args.args[0])

    def test_console_lease_failure_closes_only_acquired_desktop_lock(self):
        self.environment()
        self.controller.start_console.side_effect = RuntimeErrorBase('private lease information')
        with self.assertRaises(RuntimeErrorBase):
            run(self.paths)
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.force.assert_not_called()
        self.window_type.assert_not_called()
        self.assertEqual(self.errors()[0]['stage'], 'console_lease')

    def test_cancelled_cleanup_elevation_is_visible_and_releases_lease(self):
        self.environment()
        self.controller.start_console.return_value = {'previous_stale': True}
        self.force.side_effect = RuntimeError('private cancelled-elevation details')
        with self.assertRaises(RuntimeError):
            run(self.paths)
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.window_type.assert_not_called()
        self.assertEqual(self.errors()[0]['stage'], 'cleanup_legacy')
        self.assertIn('管理员授权', self.show.call_args.args[0])

    def test_incomplete_cleanup_does_not_open_workbench_or_report_success(self):
        self.environment()
        self.controller.has_resident_processes.return_value = True
        self.force.return_value = {'success': False, 'report_file': 'private-control-report'}
        with self.assertRaises(RuntimeError):
            run(self.paths)
        self.window_type.assert_not_called()
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.assertEqual(self.errors()[0]['stage'], 'cleanup_legacy')
        self.assertNotIn('private-control-report', self.show.call_args.args[0])

    def test_workbench_construction_failure_is_logged_before_window_exists(self):
        self.environment()
        self.window_type.side_effect = RuntimeError('private Tk diagnostic')
        with self.assertRaises(RuntimeError):
            run(self.paths)
        self.assertEqual(self.errors()[0]['stage'], 'create_workbench')
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.exit_process.assert_not_called()

    def test_unexpected_mainloop_failure_keeps_cleanup_and_headless_exit(self):
        self.environment()
        self.window.shutdown_started = False
        self.window.mainloop.side_effect = RuntimeError('private event details')
        with self.assertRaises(RuntimeError):
            run(self.paths)
        self.assertEqual(self.errors()[0]['stage'], 'mainloop')
        self.force.assert_called_once_with(self.paths)
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.exit_process.assert_called_once_with(1)

    def test_successful_window_close_has_no_error_popup_or_duplicate_sweep(self):
        self.environment()
        run(self.paths)
        self.window.mainloop.assert_called_once()
        self.controller.end_console.assert_called_once()
        self.lock.close.assert_called_once()
        self.force.assert_not_called()
        self.show.assert_not_called()
        self.assertEqual(self.errors(), [])
        self.exit_process.assert_called_once_with(0)

    def test_cleanup_failure_is_visible_and_does_not_skip_lock_release(self):
        self.environment()
        self.controller.end_console.side_effect = OSError('private cleanup detail')
        with self.assertRaises(OSError):
            run(self.paths)
        self.assertEqual(self.errors()[0]['stage'], 'console_cleanup')
        self.lock.close.assert_called_once()
        self.exit_process.assert_called_once_with(1)

    def test_lock_release_failure_is_visible_and_cannot_leave_headless_process(self):
        self.environment()
        self.lock.close.side_effect = OSError('private lock detail')
        with self.assertRaises(OSError):
            run(self.paths)
        self.assertEqual(self.errors()[0]['stage'], 'lock_release')
        self.exit_process.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
