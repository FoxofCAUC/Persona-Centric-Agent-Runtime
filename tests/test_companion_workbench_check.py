"""Artificial acceptance-launcher boundaries, separate from product behavior."""
import asyncio
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from experiments.companion_workbench_check import (
    BASE, VERSION, ArtificialClient, NetworkFence, OfflineRuntime, OwnedController,
    checked_root, supplementary_check,
)
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths


class FixtureBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='unit-', dir=BASE)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        atomic_json(self.root / 'ownership.json', {'version': VERSION, 'root': str(self.root), 'artificial_only': True})

    def test_formal_root_and_sibling_are_rejected_before_read(self):
        for path in (DEFAULT_HOME, DEFAULT_HOME / 'data', BASE, BASE.parent / 'other'):
            with self.assertRaises(RuntimeErrorBase): checked_root(path)
        self.assertEqual(checked_root(self.root), self.root)

    def test_foreign_ownership_cannot_authorize_existing_data(self):
        atomic_json(self.root / 'ownership.json', {'version': VERSION, 'root': str(self.root), 'artificial_only': False})
        with self.assertRaisesRegex(RuntimeErrorBase, 'ownership_invalid'): checked_root(self.root)

    async def test_real_client_and_foreign_network_are_blocked(self):
        from persona_runtime.chat import ChatCompletionsClient
        with NetworkFence() as fence:
            fence.port = 12345
            for address in (('192.0.2.1', 443), ('127.0.0.1', 12346)):
                with socket.socket() as sock, self.assertRaisesRegex(RuntimeErrorBase, 'external_network_blocked'):
                    sock.connect(address)
            with self.assertRaisesRegex(RuntimeErrorBase, 'external_network_blocked'):
                socket.getaddrinfo('example.invalid', 443)
            with self.assertRaisesRegex(RuntimeErrorBase, 'real_client_blocked'):
                await ChatCompletionsClient().complete({}, [])
        self.assertEqual(len(fence.blocked), 4)

    def test_only_owned_loopback_port_can_connect(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            with NetworkFence() as fence:
                fence.port = listener.getsockname()[1]
                with socket.create_connection(('127.0.0.1', fence.port), timeout=1):
                    accepted, _ = listener.accept(); accepted.close()
                self.assertEqual(fence.blocked, [])

    async def test_async_numeric_ip_blocked_before_transport_and_socket_dispatch(self):
        loop = asyncio.get_running_loop()
        from asyncio.selector_events import BaseSelectorEventLoop
        classes = [BaseSelectorEventLoop]
        try:
            from asyncio.proactor_events import BaseProactorEventLoop
            classes.append(BaseProactorEventLoop)
        except ImportError:
            pass
        for cls in classes:
            with patch.object(cls, 'sock_connect', new_callable=AsyncMock) as connect, \
                 patch('asyncio.BaseEventLoop.create_connection', new_callable=AsyncMock) as transport:
                with NetworkFence() as fence:
                    fence.port = 12345
                    for address in (('192.0.2.1', 443), ('127.0.0.1', 12346)):
                        with self.assertRaisesRegex(RuntimeErrorBase, 'external_network_blocked'):
                            await asyncio.BaseEventLoop.create_connection(loop, lambda: None, *address)
                        with socket.socket() as sock, self.assertRaisesRegex(RuntimeErrorBase, 'external_network_blocked'):
                            await cls.sock_connect(loop, sock, address)
                connect.assert_not_awaited()
                transport.assert_not_awaited()

    async def test_mock_cancel_writes_terminal_record_without_reply(self):
        client = ArtificialClient(self.root)
        task = asyncio.create_task(client.complete({'name': 'grok-4.7', 'max_tokens': 100},
                    [{'role': 'user', 'content': '[等待取消]'}]))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        rows = [json.loads(p.read_text(encoding='utf-8')) for p in (self.root / 'calls').glob('*.json')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'cancelled')
        self.assertNotIn('response', rows[0])
        self.assertFalse(rows[0]['real_call'])

    def test_controller_cannot_delegate_global_lifecycle(self):
        runtime = type('Runtime', (), {'running': True})()
        controller = OwnedController(runtime)
        self.assertTrue(controller.owned('service'))
        self.assertFalse(controller.owned('napcat'))
        for name in ('start', 'stop', 'start_all', 'stop_all', 'force_stop_all', 'force_stop_qq', 'management_url'):
            with self.assertRaises(RuntimeErrorBase): getattr(controller, name)('napcat')
        self.assertEqual(controller.snapshot()['service']['state'], '运行')

    def test_credentials_fail_before_app_or_worker_creation(self):
        cfg = ConfigStore(RuntimePaths(self.root / 'home')); cfg.initialize()
        value = cfg.load(); value['model']['encrypted_key'] = 'not-a-provider-key'
        atomic_json(cfg.file, value)
        atomic_json(self.root / 'preparation.json', {'status': 'prepared'})
        runtime = OfflineRuntime(self.root)
        with self.assertRaisesRegex(RuntimeErrorBase, 'credentials_or_qq_forbidden'):
            with runtime: self.fail('must not start')
        self.assertIsNone(runtime.app)
        self.assertFalse(runtime.thread.is_alive())

    def test_close_settles_scheduled_turn_before_run_has_entered(self):
        from persona_runtime.models import generic_persona, identifier
        from persona_runtime.storage import Store
        paths = RuntimePaths(self.root / 'home')
        cfg = ConfigStore(paths); cfg.initialize()
        cfg.update({'model': {'base_url': 'https://offline.invalid/v1', 'name': 'grok-4.7'}})
        atomic_json(self.root / 'preparation.json', {'status': 'prepared'})
        with patch('experiments.companion_workbench_check.configure_local'), OfflineRuntime(self.root) as runtime:
            engine = runtime.app['engine']
            pid = engine.service.create_persona(generic_persona())['id']; engine.service.use_persona(pid)
            async def not_started(*args): await asyncio.Event().wait()
            with patch.object(engine, '_run', not_started):
                turn = asyncio.run_coroutine_threadsafe(engine.submit('desktop', '人工取消检查', identifier()), runtime.loop).result(5)
                self.assertEqual(engine.get(turn['id'])['status'], 'queued')
        with Store(paths).connection() as db:
            state = db.execute('SELECT status,cancelled_at FROM turns WHERE id=?', (turn['id'],)).fetchone()
            self.assertEqual(state['status'], 'interrupted')
            self.assertTrue(state['cancelled_at'])
        self.assertFalse(runtime.thread.is_alive())

    def test_completed_supplement_returns_saved_evidence_without_new_runtime(self):
        saved = {'status': 'passed', 'turn_id': 'already-completed', 'mock_calls': 1}
        atomic_json(self.root / 'supplementary-check.json', saved)
        with patch('experiments.companion_workbench_check.OfflineRuntime', side_effect=AssertionError('must not replay')):
            self.assertEqual(supplementary_check(self.root), saved)

    def test_unknown_or_stopped_supplement_is_not_automatically_resent(self):
        for status in ('preparing', 'stopped'):
            atomic_json(self.root / 'supplementary-check.json', {'status': status})
            with patch('experiments.companion_workbench_check.OfflineRuntime', side_effect=AssertionError('must not resume')):
                with self.assertRaisesRegex(RuntimeErrorBase, 'requires_review_no_replay'):
                    supplementary_check(self.root)


if __name__ == '__main__': unittest.main()
