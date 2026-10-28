"""AstrBot may start before QQ logs in; no real processes or platforms."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths


class StartupReadinessTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='startup-ready-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        config = ConfigStore(self.paths)
        self.cfg = config.initialize()
        config.update({'bot_qq': '456', 'owner_qq': '123'})
        self.cfg = config.load()
        self.ctl = ServiceController(self.paths)
        self.identity = {'ProcessId': 1234, 'Created': 'current-process', 'ExecutablePath': 'python.exe'}
        self.record = {'instance_id': self.cfg['instance_id'], 'host': self.identity}
        self.proof = {'at': time.time(), 'pid': 1234, 'instance_id': self.cfg['instance_id'],
                      'verified': False, 'online': False, 'plugin_version': '0.6.0',
                      'error_type': 'ApiNotAvailable', 'error_stage': 'probe'}
        self.proof_path = self.paths.home / 'runtime/astrbot-health.json'
        self.save()
        self.waiting = {'state': '等待验证', 'owned': True, 'port': self.cfg['astrbot_port'],
                        'login': None, 'onebot': None}

    def save(self, proof=None):
        atomic_json(self.proof_path, self.proof if proof is None else proof)

    def ready(self, state=None, listening=None):
        with patch.object(self.ctl, 'owned', return_value=True), \
                patch.object(self.ctl, 'record', return_value=self.record), \
                patch('persona_runtime.lifecycle.listener', return_value=self.identity) as listeners:
            if listening is not None:
                listeners.side_effect = listening
            return self.ctl._start_ready('astrbot', self.waiting if state is None else state)

    def test_unconnected_astrbot_does_not_claim_login_but_allows_napcat_start(self):
        started_roles = []
        def health(role):
            started_roles.append(role)
            return self.waiting.copy() if role == 'astrbot' else {'state': '运行', 'owned': True}
        with patch('persona_runtime.lifecycle.require_console', return_value={'token': 'console'}), \
                patch.object(self.ctl, 'health', side_effect=health), \
                patch.object(self.ctl, 'owned', return_value=True), \
                patch.object(self.ctl, 'record', return_value=self.record), \
                patch('persona_runtime.lifecycle.listener', return_value=self.identity), \
                patch('persona_runtime.lifecycle.subprocess.Popen') as spawn:
            result = self.ctl.start_all()
        self.assertEqual(started_roles, ['service', 'astrbot', 'napcat', 'astrbot'])
        self.assertEqual(result['state'], '等待验证')
        self.assertIsNone(result['login'])
        self.assertIsNone(result['onebot'])
        spawn.assert_not_called()

    def test_new_process_returns_when_host_ready_without_waiting_for_qq(self):
        python = self.paths.home / 'components/astrbot/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.touch()  # Existence only; Popen is mocked.
        stopped = {'state': '已停止', 'owned': False, 'port': self.cfg['astrbot_port']}
        with patch('persona_runtime.lifecycle.require_console', return_value={'token': 'console'}), \
                patch.object(self.ctl, 'health', side_effect=[stopped, self.waiting]), \
                patch.object(self.ctl, 'owned', return_value=True), \
                patch.object(self.ctl, 'record', return_value=self.record), \
                patch('persona_runtime.lifecycle.listener', return_value=self.identity), \
                patch('persona_runtime.lifecycle.port_open', return_value=False), \
                patch('persona_runtime.lifecycle.render_components'), \
                patch('persona_runtime.lifecycle.subprocess.Popen') as spawn, \
                patch('persona_runtime.lifecycle.time.sleep'), \
                patch('persona_runtime.lifecycle.time.monotonic', side_effect=[0, 1]):
            result = self.ctl.start('astrbot')
        self.assertEqual(result, self.waiting)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][4], '--role')
        self.assertIn('astrbot', spawn.call_args.args[0])

    def test_healthy_bridge_and_fresh_unconnected_bridge_are_both_start_ready(self):
        self.assertTrue(self.ready())
        self.save(self.proof | {'verified': True, 'online': True, 'error_type': None, 'error_stage': None})
        self.assertTrue(self.ready())

    def test_stale_future_wrong_instance_or_wrong_pid_never_unblock_start(self):
        for change in ({'at': time.time()-16}, {'at': time.time()+6},
                       {'instance_id': 'other'}, {'pid': 1235}, {'pid': '1234'}):
            with self.subTest(change=change):
                self.save(self.proof | change)
                self.assertFalse(self.ready())

    def test_malformed_missing_or_unreadable_proof_is_not_readiness(self):
        for change in ({'at': float('nan')}, {'at': float('inf')}, {'at': 'now'},
                       {'online': None}, {'verified': 'true'}):
            with self.subTest(change=change):
                self.save(self.proof | change)
                self.assertFalse(self.ready())
        self.proof_path.write_text('[]', encoding='utf-8')
        self.assertFalse(self.ready())
        self.proof_path.write_text('{', encoding='utf-8')
        self.assertFalse(self.ready())
        self.proof_path.unlink()
        self.assertFalse(self.ready())

    def test_platform_configuration_and_other_probe_errors_do_not_pass(self):
        for change in ({'error_type': 'ValueError', 'error_stage': 'config'},
                       {'error_type': 'ApiNotAvailable', 'error_stage': 'platform'},
                       {'error_type': 'TimeoutError', 'error_stage': 'probe'},
                       {'error_type': 'PermissionError', 'error_stage': 'publish'}):
            with self.subTest(change=change):
                self.save(self.proof | change)
                self.assertFalse(self.ready())

    def test_both_listeners_must_belong_to_current_host(self):
        invalid = [None, self.identity | {'ProcessId': 99}, self.identity | {'Created': 'recycled'},
                   self.identity | {'ExecutablePath': 'other.exe'}]
        for invalid_identity in invalid:
            for port_index in (0, 1):
                with self.subTest(identity=invalid_identity, port_index=port_index):
                    listeners = [self.identity, self.identity]
                    listeners[port_index] = invalid_identity
                    self.assertFalse(self.ready(listening=listeners))

    def test_external_or_wrong_record_host_never_unblocks_start(self):
        self.assertFalse(self.ready(state=self.waiting | {'owned': False}))
        for record in (None, [], {'host': self.identity}, self.record | {'instance_id': 'other'},
                       self.record | {'host': self.identity | {'Created': ''}}):
            with self.subTest(record=record), patch.object(self.ctl, 'owned', return_value=True), \
                    patch.object(self.ctl, 'record', return_value=record), \
                    patch('persona_runtime.lifecycle.listener') as listener:
                self.assertFalse(self.ctl._start_ready('astrbot', self.waiting))
                listener.assert_not_called()

    def test_current_error_health_remains_unknown_after_successful_readiness_check(self):
        with patch.object(self.ctl, 'owned', return_value=True), \
                patch.object(self.ctl, 'record', return_value=self.record), \
                patch('persona_runtime.lifecycle.port_open', return_value=True), \
                patch('persona_runtime.lifecycle.listener', return_value=self.identity):
            state = self.ctl.health('astrbot')
            self.assertTrue(self.ctl._start_ready('astrbot', state))
            after = self.ctl.health('astrbot')
        self.assertEqual(state, after)
        self.assertEqual(state['state'], '等待验证')
        self.assertIsNone(state['login'])
        self.assertIsNone(state['onebot'])

    def test_stale_owned_host_does_not_spawn_second_or_continue_start_all(self):
        self.save(self.proof | {'at': time.time()-300})
        calls = []
        def health(role):
            calls.append(role)
            return self.waiting.copy() if role == 'astrbot' else {'state': '运行', 'owned': True}
        with patch('persona_runtime.lifecycle.require_console', return_value={'token': 'console'}), \
                patch.object(self.ctl, 'health', side_effect=health), \
                patch.object(self.ctl, 'owned', return_value=True), \
                patch.object(self.ctl, 'record', return_value=self.record), \
                patch('persona_runtime.lifecycle.subprocess.Popen') as spawn:
            with self.assertRaises(RuntimeErrorBase):
                self.ctl.start_all()
        self.assertEqual(calls, ['service', 'astrbot'])
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
