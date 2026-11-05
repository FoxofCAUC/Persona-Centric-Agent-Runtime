"""Artificial lifecycle tests for the native text reference window."""
import asyncio
import json
from pathlib import Path
import queue
import socket
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from experiments import companion_native_trial as trial
from experiments.companion_native_reference import effective_result
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='native-window-unit-')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        self.paths = RuntimePaths(self.source)
        self.config = ConfigStore(self.paths); self.config.initialize()
        self.config.update({'model': {'base_url': 'http://127.0.0.1:9/v1', 'name': 'grok-4.7'}})
        self.reference = self.source / 'reference/dafeiyu'
        python = self.reference / '.venv/Scripts/python.exe'; python.parent.mkdir(parents=True)
        python.write_bytes(b'artificial-placeholder')
        self.adapter = SimpleNamespace(
            source_status=lambda root: {'source_ready': True, 'text_patches_applied': True},
            freeze_trial=lambda *args: {'artificial': True},
            verify_trial=lambda *args: None,
            effective_result=effective_result,
            load_trial=lambda root: json.loads((root / 'trial.json').read_text(encoding='utf-8')))
        self.enterContext(patch.object(trial, 'backend', return_value=self.adapter))
        connect = socket.socket.connect
        def local_only(sock, address):
            if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
                return connect(sock, address)
            raise AssertionError('external_network_blocked')
        self.enterContext(patch('socket.socket.connect', new=local_only))

    def prepared(self):
        return trial.prepare(self.source, reference_root=self.reference, development_material=True)

    def private_copy(self, _):
        home = self.source / 'tmp/private-acceptance' / identifier()
        store = Store(RuntimePaths(home)); store.initialize()
        service = RuntimeService(store); pid = service.create_persona(generic_persona())['id']
        service.use_persona(pid)
        atomic_json(home / 'runtime/private-copy-policy.json', {'persona_id': pid, 'approved_models': {}})
        return {'home': str(home), 'persona_id': pid}

    def worker(self, root):
        worker = trial.NativeTrialWorker(root, queue.Queue())
        self.addCleanup(self.stop, worker)
        self.assertTrue(worker.ready.wait(5))
        return worker

    @staticmethod
    def stop(worker):
        worker.stopping = True
        worker.thread.join(5)
        if worker.thread.is_alive():
            raise AssertionError('worker did not settle')

    @staticmethod
    def completed(folder, display='显示正文', history='上游历史正文'):
        value = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
        atomic_json(folder / 'result.json', {'status': 'completed', 'display': display,
            'history_reply': history, 'physical_calls': 0, 'simulated_calls': 1,
            'native_history': value['history'] + [{'role': 'user', 'content': value['text']},
                {'role': 'assistant', 'content': history}]})
        atomic_json(folder / 'terminal.json', {'status': 'completed'})


class Preparation(Fixture):
    def test_private_scope_requires_one_ready_and_provider_approval(self):
        with patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
                {'id': 'artificial', 'state': 'ready', 'approved_models': {}}]}), \
             patch.object(trial.PrivatePersonas, 'create_sandbox') as copy:
            with self.assertRaisesRegex(Exception, 'provider_not_authorized'):
                trial.prepare(self.source, private=True, reference_root=self.reference)
            copy.assert_not_called()

    def test_private_prepare_registers_owner_and_freezes_without_generation(self):
        with patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
                {'id': 'artificial', 'state': 'ready', 'approved_models': {}}]}), \
             patch.object(trial.PrivatePersonas, 'create_sandbox', side_effect=self.private_copy):
            root = trial.prepare(self.source, private=True, approve_current_provider=True,
                reference_root=self.reference)
        plan = self.adapter.load_trial(root)
        self.assertEqual(plan['status'], 'prepared')
        self.assertEqual(plan['reasoning_profile'], {'version': 'native-text-low-v1'})
        self.assertEqual(plan['cleanup_homes'], [plan['home']])
        self.assertEqual(plan['planned_rounds'], [8, 12])
        self.assertEqual(plan['startup_paid_calls'], 0)
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['trial.json'])
        self.assertEqual(trial.read_session(trial.data_root(plan)), ({'version': 1, 'order': []}, [], [], False))
        cfg = ConfigStore(RuntimePaths(Path(plan['home']))).load()
        self.assertEqual(cfg['bot_qq'], '')
        self.assertFalse(cfg['organization']['daily_enabled'])

    def test_failure_after_copy_keeps_cleanup_ownership(self):
        self.adapter.freeze_trial = lambda *args: (_ for _ in ()).throw(RuntimeError('freeze_failed'))
        with self.assertRaises(RuntimeError) as caught:
            self.prepared()
        root = Path(caught.exception.preparation_trial_root)
        plan = self.adapter.load_trial(root)
        self.assertEqual(plan['status'], 'preparation_blocked')
        self.assertEqual(plan['cleanup_homes'], [plan['home']])
        self.assertTrue(Path(plan['home']).exists())

    def test_invalid_material_scope_and_missing_public_runtime_fail_before_copy(self):
        with self.assertRaisesRegex(Exception, 'material_scope_invalid'):
            trial.prepare(self.source)
        self.adapter.source_status = lambda root: {'source_ready': False}
        with self.assertRaisesRegex(Exception, 'source_not_ready'):
            self.prepared()
        self.assertFalse((self.source / 'tmp/companion-native-reference').exists())

    def test_development_copy_never_carries_source_encrypted_credentials(self):
        source = self.config.load()
        source['model']['encrypted_key'] = 'artificial-main-ciphertext-placeholder'
        source['character_selector']['encrypted_key'] = 'artificial-selector-ciphertext-placeholder'
        atomic_json(self.config.file, source)
        root = self.prepared()
        plan = self.adapter.load_trial(root)
        copied = ConfigStore(RuntimePaths(Path(plan['home']))).load()
        self.assertEqual(copied['model']['encrypted_key'], '')
        self.assertEqual(copied['character_selector']['encrypted_key'], '')
        self.assertEqual(self.config.load(), source)


class WorkerFlow(Fixture):
    def test_two_turns_keep_native_history_and_do_not_use_display_as_history(self):
        root = self.prepared(); seen = []
        async def fake_child(worker, folder, turn_id):
            seen.append(json.loads((folder / 'input.json').read_text('utf-8')))
            self.completed(folder)
        with patch.object(trial.NativeTrialWorker, 'run_child', new=fake_child):
            worker = self.worker(root)
            self.assertTrue(worker.accepting.is_set())
            self.assertEqual(seen, [])
            for text in ('第一轮人工输入', '第二轮人工输入'):
                self.assertTrue(asyncio.run_coroutine_threadsafe(worker.send(text), worker.loop).result(5))
            self.assertEqual(seen[1]['history'], [
                {'role': 'user', 'content': '第一轮人工输入'},
                {'role': 'assistant', 'content': '上游历史正文'}])
            items = list(worker.events.queue)
            self.assertEqual([v for k, v in items if k == 'reply'], ['显示正文', '显示正文'])
            self.stop(worker)
            reopened = self.worker(root)
            self.assertTrue(reopened.accepting.is_set())
            self.assertEqual(len(seen), 2)
            self.assertEqual(sum(k == 'history' for k, _ in list(reopened.events.queue)), 2)

    def test_single_flight_cancel_settles_without_reply_or_replay(self):
        root = self.prepared(); started = threading.Event(); calls = []
        async def waiting_child(worker, folder, turn_id):
            calls.append(turn_id); started.set()
            while not (folder / 'cancel.json').exists():
                await asyncio.sleep(.01)
            atomic_json(folder / 'result.json', {'status': 'cancelled', 'error_code': 'request_interrupted'})
            atomic_json(folder / 'terminal.json', {'status': 'cancelled'})
        with patch.object(trial.NativeTrialWorker, 'run_child', new=waiting_child):
            worker = self.worker(root)
            first = asyncio.run_coroutine_threadsafe(worker.send('人工等待'), worker.loop)
            self.assertTrue(started.wait(3))
            self.assertFalse(asyncio.run_coroutine_threadsafe(worker.send('不得并发'), worker.loop).result(3))
            asyncio.run_coroutine_threadsafe(worker.cancel(), worker.loop).result(3)
            self.assertFalse(first.result(5))
            self.assertEqual(calls, ['0001'])
            self.assertFalse(worker.accepting.is_set())
            self.stop(worker)
            reopened = self.worker(root)
            self.assertFalse(reopened.accepting.is_set())
            self.assertEqual(calls, ['0001'])

    def test_close_waits_for_cancel_receipt(self):
        root = self.prepared(); started = threading.Event(); settled = threading.Event()
        async def child(worker, folder, turn_id):
            started.set()
            while not (folder / 'cancel.json').exists(): await asyncio.sleep(.01)
            await asyncio.sleep(.05)
            atomic_json(folder / 'result.json', {'status': 'cancelled'})
            atomic_json(folder / 'terminal.json', {'status': 'cancelled'}); settled.set()
        with patch.object(trial.NativeTrialWorker, 'run_child', new=child):
            worker = self.worker(root)
            future = asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop)
            self.assertTrue(started.wait(3)); self.stop(worker)
            self.assertTrue(settled.is_set()); self.assertFalse(future.result(1))

    def test_child_without_terminal_remains_unknown_and_blocks_reopen(self):
        root = self.prepared()
        async def child(*args): return None
        with patch.object(trial.NativeTrialWorker, 'run_child', new=child):
            worker = self.worker(root)
            self.assertFalse(asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop).result(3))
            folder = worker.data / 'turns/0001'
            self.assertFalse((folder / 'terminal.json').exists())
            self.assertFalse(worker.accepting.is_set())
            self.stop(worker)
            reopened = self.worker(root)
            self.assertFalse(reopened.accepting.is_set())

    def test_source_change_before_send_prevents_input_and_child(self):
        root = self.prepared(); worker = self.worker(root)
        self.adapter.verify_trial = lambda *args: (_ for _ in ()).throw(RuntimeError('changed'))
        with patch.object(worker, 'run_child') as child:
            self.assertFalse(asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop).result(3))
            child.assert_not_called()
        self.assertFalse((worker.data / 'turns').exists())

    def test_session_write_failure_preserves_unregistered_checkpoint(self):
        root = self.prepared(); worker = self.worker(root)
        original = trial.atomic_json
        def fail_session(path, value):
            if path.name == 'session.json': raise OSError('artificial_write_failure')
            return original(path, value)
        with patch.object(trial, 'atomic_json', side_effect=fail_session), patch.object(worker, 'run_child') as child:
            self.assertFalse(asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop).result(3))
            child.assert_not_called()
        with self.assertRaisesRegex(Exception, 'unregistered_turn'):
            trial.read_session(worker.data)

    def test_double_window_lock_does_not_acquire_same_copy(self):
        root = self.prepared(); first = self.worker(root); second = self.worker(root)
        self.assertTrue(first.accepting.is_set())
        self.assertFalse(second.accepting.is_set())
        second.thread.join(3)
        self.assertFalse(second.thread.is_alive())

    def test_unknown_or_changed_history_is_rejected(self):
        root = self.prepared(); data = trial.data_root(self.adapter.load_trial(root))
        folder = data / 'turns/0001'; folder.mkdir(parents=True)
        atomic_json(folder / 'input.json', {'seq': 1, 'text': '人工输入', 'history': [{'role': 'assistant', 'content': '注入'}]})
        atomic_json(data / 'session.json', {'version': 1, 'order': ['0001']})
        self.completed(folder)
        with self.assertRaisesRegex(Exception, 'history_changed'):
            trial.read_session(data)

    def test_native_no_reply_is_success_without_fake_persona_text(self):
        root = self.prepared()
        async def child(worker, folder, turn_id):
            self.completed(folder, display='', history='NO_REPLY')
            path = folder / 'result.json'
            result = json.loads(path.read_text(encoding='utf-8'))
            result['suppressed'] = True
            atomic_json(path, result)
        with patch.object(trial.NativeTrialWorker, 'run_child', new=child):
            worker = self.worker(root)
            self.assertTrue(asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop).result(3))
            self.assertTrue(worker.accepting.is_set())
            items = list(worker.events.queue)
            self.assertFalse(any(k == 'reply' for k, _ in items))
            self.assertIn(('suppressed', '本轮没有发送文本'), items)
            _, history, visible, interrupted = trial.read_session(worker.data)
            self.assertEqual(history[-1]['content'], 'NO_REPLY')
            self.assertFalse(interrupted)
            self.assertEqual(visible[0]['display'], '')

    def test_cancel_after_generation_before_display_keeps_receipt_but_never_delivers_late(self):
        root = self.prepared()
        async def child(worker, folder, turn_id):
            self.completed(folder)
            atomic_json(folder / 'cancel.json', {'reason': 'user_cancelled'})
        with patch.object(trial.NativeTrialWorker, 'run_child', new=child):
            worker = self.worker(root)
            self.assertTrue(asyncio.run_coroutine_threadsafe(worker.send('人工输入'), worker.loop).result(3))
            self.assertFalse(worker.accepting.is_set())
            self.assertFalse(any(k == 'reply' for k, _ in list(worker.events.queue)))
            result = json.loads((worker.data / 'turns/0001/result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'], 'completed')
            self.stop(worker)
            reopened = self.worker(root)
            self.assertFalse(reopened.accepting.is_set())
            visible = [v for k, v in list(reopened.events.queue) if k == 'history']
            self.assertEqual(visible[0]['display'], '')


if __name__ == '__main__':
    unittest.main()
