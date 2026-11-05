"""Isolated trial lifecycle checks; no providers or private data."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments import companion_trial as trial
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class FakeClient:
    def __init__(self):
        self.calls = []

    async def complete(self, model, messages):
        self.calls.append((model, messages))
        if model.get('_character_selection'):
            return '{"schema_version":1,"lore_ids":[],"example_ids":[]}', None
        return '{"schema_version":3,"behavior_intent":"respond","messages":[{"content":"模拟接话","content_kind":"chat","cancel_if_new_input":false}]}', None


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home / 'tmp', prefix='trial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        store = Store(self.paths)
        store.initialize()
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({'model': {'name': 'mock', 'base_url': 'http://localhost:9/v1'}})
        service = RuntimeService(store)
        self.pid = service.create_persona(generic_persona())['id']
        service.use_persona(self.pid)
        trial.configure(self.paths, self.config.load(), self.pid)
        trial.configure_policy(store, self.config, self.pid, 'minimal', background_prepare=False)

    def test_trial_configuration_has_no_qq_or_automatic_organization(self):
        value = self.config.load()
        self.assertFalse(value['organization']['daily_enabled'])
        self.assertFalse(value['organization']['reuse_chat'])
        self.assertEqual(value['bot_qq'], '')
        self.assertEqual(value['owner_qq'], '')
        self.assertEqual(value['model']['hourly_limit'], 0)
        self.assertEqual(value['character_selector']['daily_limit'], 0)

    def _policy_semantics(self):
        with Store(self.paths).connection() as conn:
            row = conn.execute('SELECT p.mode,p.reply_mode,p.data_json FROM interaction_policies p '
                'JOIN interaction_heads h ON h.version_id=p.id '
                "WHERE h.persona_id=? AND h.channel='desktop'", (self.pid,)).fetchone()
        data = json.loads(row['data_json'])
        context = data['context']
        return {'mode': row['mode'], 'reply_mode': row['reply_mode'],
                'companion': data['companion'], 'expression_version': data['expression']['version'],
                'retrieval_mode': context['character_retrieval']['mode'],
                'disposition_mode': context['disposition_mode'],
                'observation_mode': context['situation_observation_mode']}

    def test_policy_configuration_can_suppress_background_prepare_without_changing_admission(self):
        with patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            trial.configure_policy(Store(self.paths), self.config, self.pid, 'current', background_prepare=False)
            notify.assert_not_called()
        value = self._policy_semantics()
        self.assertEqual(value['companion'], {'version': 'companion-runtime-v1', 'mode': 'current'})
        self.assertEqual(value['retrieval_mode'], 'hybrid_llm')
        self.assertEqual(value['mode'], 'natural_batch')
        self.assertEqual(value['reply_mode'], 'natural_structured')
        self.assertEqual(value['expression_version'], 3)
        self.assertEqual(value['disposition_mode'], 'baseline')
        self.assertEqual(value['observation_mode'], 'disabled')

    def test_default_background_notification_preserves_same_semantics_as_suppressed_setup(self):
        with patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            trial.configure_policy(Store(self.paths), self.config, self.pid, 'current')
            notify.assert_called_once_with(self.pid)
        normal = self._policy_semantics()
        with patch('persona_runtime.character_runtime.CharacterRuntime.notify'):
            trial.configure_policy(Store(self.paths), self.config, self.pid, 'minimal', background_prepare=False)
        minimal = self._policy_semantics()
        self.assertEqual(minimal['companion']['mode'], 'minimal')
        self.assertEqual(minimal['retrieval_mode'], 'hybrid')
        with patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            trial.configure_policy(Store(self.paths), self.config, self.pid, 'current', background_prepare=False)
            notify.assert_not_called()
        self.assertEqual(self._policy_semantics(), normal)

    def test_normal_ingress_worker_delivers_decoded_reply_and_closes(self):
        import asyncio
        events = queue.Queue()
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=FakeClient()):
            worker = trial.TrialWorker(self.paths.home, events)
            try:
                self.assertTrue(worker.ready.wait(10))
                async def submit_and_seal():
                    result = worker.ingress.accept({'channel': 'desktop', 'text': '人工测试', 'request_id': identifier()})
                    batch = worker.ingress.get(result['batch_id'])
                    worker.ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.pid)
                asyncio.run_coroutine_threadsafe(submit_and_seal(), worker.loop).result(10)
                until = time.monotonic() + 15
                messages = []
                while time.monotonic() < until:
                    item = events.get(timeout=15)
                    messages.append(item)
                    if item[0] in ('reply', 'error'):
                        break
                self.assertIn(('reply', '模拟接话'), messages)
            finally:
                worker.stopping = True
                worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())

    def test_worker_cancels_collecting_input_without_generation(self):
        import asyncio
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=FakeClient()):
            worker = trial.TrialWorker(self.paths.home, queue.Queue())
            try:
                self.assertTrue(worker.ready.wait(10))
                async def check():
                    result = worker.ingress.accept({'channel': 'desktop', 'text': '取消人工输入', 'request_id': identifier()})
                    await worker.cancel()
                    return worker.ingress.get(result['batch_id'])
                batch = asyncio.run_coroutine_threadsafe(check(), worker.loop).result(10)
                self.assertEqual(batch['state'], 'cancelled')
                self.assertIsNone(batch['turn_id'])
            finally:
                worker.stopping = True
                worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())

    def test_lock_failure_reports_terminal_state_instead_of_hanging_window(self):
        events = queue.Queue()
        with patch('persona_runtime.instance.InstanceLock', side_effect=RuntimeError('busy')):
            worker = trial.TrialWorker(self.paths.home, events)
            self.assertTrue(worker.ready.wait(5))
            worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual([events.get_nowait()[0] for _ in range(events.qsize())], ['error', 'closed'])

    def test_private_trial_rejects_unapproved_provider_without_explicit_decision(self):
        with patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
            {'id': 'artificial-id', 'state': 'ready', 'approved_models': {}}]}):
            with self.assertRaisesRegex(Exception, 'private_cloud_provider_not_authorized'):
                trial.prepare(self.paths.home, private=True)

    def _artificial_private_copy(self, role_id):
        """Exercise copy orchestration without reading or importing private data."""
        self.assertEqual(role_id, 'artificial-id')
        paths = RuntimePaths(self.paths.home / 'tmp/private-acceptance' / identifier())
        store = Store(paths)
        store.initialize()
        service = RuntimeService(store)
        pid = service.create_persona(generic_persona())['id']
        service.use_persona(pid)
        atomic_json(paths.home / 'runtime/private-copy-policy.json', {'approved_models': {}})
        return {'home': str(paths.home), 'persona_id': pid}

    def test_structural_candidate_prepares_one_copy_and_freezes_normal_policy(self):
        source_before = self.config.file.read_bytes()
        ready = {'status': 'ready', 'representative_hybrid': True,
                 'model_fingerprint': 'artificial-model', 'index_fingerprint': 'artificial-index'}
        with patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
                    {'id': 'artificial-id', 'state': 'ready', 'approved_models': {}}]}), \
                patch.object(trial.PrivatePersonas, 'create_sandbox', side_effect=self._artificial_private_copy) as create, \
                patch.object(trial, 'copy_weights') as copy_weights, \
                patch.object(trial, 'prepare_candidate_retrieval', return_value=ready) as local_ready, \
                patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            root = trial.prepare(self.paths.home, private=True, approve_current_provider=True,
                                 structural_candidate=True)
        create.assert_called_once_with('artificial-id')
        copy_weights.assert_called_once()
        local_ready.assert_called_once()
        notify.assert_not_called()
        manifest = trial.load_trial(root)
        self.assertEqual(set(manifest['arms']), {'C'})
        self.assertEqual(manifest['purpose'], 'continuous_chat_candidate')
        self.assertEqual(manifest['arms']['C']['mode'], 'minimal_structural')
        self.assertEqual(manifest['cleanup_homes'], [manifest['arms']['C']['home']])
        self.assertEqual(manifest['local_retrieval'], ready)
        remaining = datetime.fromisoformat(manifest['review_deadline']) - datetime.now(timezone.utc)
        self.assertLessEqual(remaining.total_seconds(), 7 * 24 * 3600)
        self.assertGreater(remaining.total_seconds(), 6 * 24 * 3600)
        self.assertEqual(self.config.file.read_bytes(), source_before)
        candidate_paths = RuntimePaths(Path(manifest['arms']['C']['home']))
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        engine = ChatEngine(Store(candidate_paths), ConfigStore(candidate_paths))
        try:
            policy = IngressService(engine).current_policy(manifest['arms']['C']['persona_id'], 'desktop')
            self.assertEqual(policy['companion'], {'version': 'companion-runtime-v2', 'mode': 'minimal',
                'format_contract': 'schema3-structural-v1'})
            self.assertEqual(policy['character_retrieval']['mode'], 'hybrid')
            self.assertEqual(policy['disposition_mode'], 'baseline')
            self.assertEqual(policy['situation_observation_mode'], 'disabled')
            self.assertEqual(policy['reply_mode'], 'natural_structured')
        finally:
            engine.cognitive.characters.close()
            engine.cognitive.shadow.close()

    def test_candidate_failure_keeps_ownership_and_terminal_reason(self):
        error = RuntimeErrorBase('comparison_local_preparation_blocked')
        error.preparation_reason = 'embedding_unavailable'
        with patch.object(trial.PrivatePersonas, 'load', return_value={'records': [
                    {'id': 'artificial-id', 'state': 'ready', 'approved_models': {}}]}), \
                patch.object(trial.PrivatePersonas, 'create_sandbox', side_effect=self._artificial_private_copy), \
                patch.object(trial, 'copy_weights'), \
                patch.object(trial, 'prepare_candidate_retrieval', side_effect=error):
            with self.assertRaises(RuntimeErrorBase) as caught:
                trial.prepare(self.paths.home, private=True, approve_current_provider=True,
                              structural_candidate=True)
        manifest = json.loads((Path(caught.exception.preparation_trial_root) / 'trial.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'preparation_blocked')
        self.assertEqual(set(manifest['arms']), {'C'})
        self.assertEqual(manifest['cleanup_homes'], [manifest['arms']['C']['home']])
        self.assertEqual(manifest['preparation_reason'], 'embedding_unavailable')
        with self.assertRaisesRegex(RuntimeErrorBase, 'trial_not_prepared'):
            trial.load_trial(caught.exception.preparation_trial_root)

    def test_candidate_requires_private_copy_before_reading_source(self):
        with patch.object(ConfigStore, 'load') as load:
            with self.assertRaisesRegex(RuntimeErrorBase, 'structural_trial_private_required'):
                trial.prepare(self.paths.home, structural_candidate=True)
        load.assert_not_called()

    def test_default_prepare_retains_two_old_randomized_arms(self):
        with patch.object(trial, 'copy_weights'), \
                patch.object(trial, 'prepare_candidate_retrieval') as ready, \
                patch('persona_runtime.character_runtime.CharacterRuntime.notify'):
            root = trial.prepare(self.paths.home)
        manifest = trial.load_trial(root)
        self.assertEqual(set(manifest['arms']), {'A', 'B'})
        self.assertEqual({arm['mode'] for arm in manifest['arms'].values()}, {'current', 'minimal'})
        self.assertNotIn('purpose', manifest)
        ready.assert_not_called()

    def _configure_candidate(self):
        trial.configure_policy(Store(self.paths), self.config, self.pid, 'minimal_structural',
                               background_prepare=False)

    def test_candidate_worker_is_not_ready_and_rejects_send_until_local_warmup_finishes(self):
        self._configure_candidate()
        entered, release = threading.Event(), threading.Event()
        client, events = FakeClient(), queue.Queue()
        def warmup(engine, persona_id, *, build):
            self.assertEqual(persona_id, self.pid)
            self.assertFalse(build)
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test_warmup_wait_expired')
            return {'status': 'ready'}
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=client), \
                patch.object(trial, 'candidate_retrieval_ready', side_effect=warmup) as ready, \
                patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            worker = trial.TrialWorker(self.paths.home, events)
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(worker.ready.is_set())
                self.assertFalse(worker.accepting.is_set())
                self.assertFalse(asyncio.run(worker.send('未准备好时的人工输入')))
                self.assertEqual(client.calls, [])
                self.assertEqual(worker.engine.requests(), [])
                release.set()
                self.assertTrue(worker.ready.wait(5))
                self.assertTrue(worker.accepting.is_set())
                ready.assert_called_once()
                notify.assert_not_called()
                self.assertEqual(client.calls, [])
            finally:
                release.set()
                worker.stopping = True
                worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())

    def test_candidate_worker_local_dependency_failure_is_terminal_without_generation(self):
        self._configure_candidate()
        client, events = FakeClient(), queue.Queue()
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=client), \
                patch.object(trial, 'candidate_retrieval_ready', side_effect=RuntimeErrorBase('embedding_busy')), \
                patch('persona_runtime.character_runtime.CharacterRuntime.notify') as notify:
            worker = trial.TrialWorker(self.paths.home, events)
            self.assertTrue(worker.ready.wait(5))
            worker.thread.join(5)
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(worker.accepting.is_set())
            self.assertFalse(asyncio.run(worker.send('失败后不能发送')))
            self.assertEqual(client.calls, [])
            notify.assert_not_called()
        collected = [events.get_nowait() for _ in range(events.qsize())]
        self.assertIn(('error', 'embedding_busy'), collected)
        self.assertEqual(sum(kind == 'closed' for kind, _ in collected), 1)

    def test_candidate_generation_uses_accepted_v2_snapshot_and_cancels_buffer(self):
        self._configure_candidate()
        client = FakeClient()
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=client), \
                patch.object(trial, 'candidate_retrieval_ready', return_value={'status': 'ready'}):
            worker = trial.TrialWorker(self.paths.home, queue.Queue())
            try:
                self.assertTrue(worker.ready.wait(5))
                async def accept_and_seal():
                    result = worker.ingress.accept({'channel': 'desktop', 'text': '人工候选输入', 'request_id': identifier()})
                    batch = worker.ingress.get(result['batch_id'])
                    worker.ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.pid)
                    await worker.engine.wait()
                    turn = worker.engine.requests()[0]
                    pending = worker.ingress.accept({'channel': 'desktop', 'text': '人工待取消', 'request_id': identifier()})
                    await worker.cancel()
                    return turn, worker.ingress.get(pending['batch_id'])
                turn, cancelled = asyncio.run_coroutine_threadsafe(accept_and_seal(), worker.loop).result(15)
                self.assertEqual(turn['status'], 'completed')
                self.assertEqual(cancelled['state'], 'cancelled')
                self.assertEqual(len(client.calls), 1)
                profile = client.calls[0][0]['_companion_profile']
                self.assertEqual(profile, {'version': 'companion-runtime-v2', 'mode': 'minimal',
                    'format_contract': 'schema3-structural-v1'})
                self.assertEqual(client.calls[0][0]['_generation_profile'], {'version': 'chat-stream-unlimited-v1'})
                self.assertFalse(client.calls[0][0].get('_character_selection'))
            finally:
                worker.stopping = True
                worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())

    def test_candidate_close_cancels_running_generation_and_reopen_never_replays(self):
        self._configure_candidate()
        entered, exited = threading.Event(), threading.Event()
        class WaitingClient(FakeClient):
            async def complete(self, model, messages):
                self.calls.append((model, messages))
                entered.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    exited.set()
        client = WaitingClient()
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=client), \
                patch.object(trial, 'candidate_retrieval_ready', return_value={'status': 'ready'}):
            worker = trial.TrialWorker(self.paths.home, queue.Queue())
            try:
                self.assertTrue(worker.ready.wait(5))
                async def submit():
                    result = worker.ingress.accept({'channel': 'desktop', 'text': '人工进行中取消', 'request_id': identifier()})
                    batch = worker.ingress.get(result['batch_id'])
                    return worker.ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.pid)
                asyncio.run_coroutine_threadsafe(submit(), worker.loop).result(5)
                self.assertTrue(entered.wait(5))
            finally:
                worker.stopping = True
                worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())
            self.assertTrue(exited.is_set())
            with Store(self.paths).connection() as conn:
                turn = conn.execute('SELECT status,error_code FROM turns').fetchone()
                self.assertEqual(turn['status'], 'interrupted')
                self.assertEqual(turn['error_code'], 'cancelled_by_user')
                self.assertEqual(conn.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0], 0)
            restored = trial.TrialWorker(self.paths.home, queue.Queue())
            try:
                self.assertTrue(restored.ready.wait(5))
                self.assertTrue(restored.accepting.is_set())
                self.assertEqual(len(client.calls), 1)
            finally:
                restored.stopping = True
                restored.thread.join(10)
            self.assertFalse(restored.thread.is_alive())

    def test_close_cancels_pending_batch_and_restart_does_not_generate(self):
        import asyncio
        events = queue.Queue()
        with patch('persona_runtime.chat.ChatCompletionsClient', return_value=FakeClient()):
            worker = trial.TrialWorker(self.paths.home, events)
            self.assertTrue(worker.ready.wait(10))
            async def accept():
                return worker.ingress.accept({'text': '关闭前的人工输入', 'request_id': identifier()})
            receipt = asyncio.run_coroutine_threadsafe(accept(), worker.loop).result(10)
            worker.stopping = True
            worker.thread.join(10)
            self.assertFalse(worker.thread.is_alive())
            restored = trial.TrialWorker(self.paths.home, queue.Queue())
            try:
                self.assertTrue(restored.ready.wait(10))
                async def check():
                    return restored.ingress.get(receipt['batch_id']), restored.engine.requests()
                batch, turns = asyncio.run_coroutine_threadsafe(check(), restored.loop).result(10)
                self.assertEqual(batch['state'], 'cancelled')
                self.assertEqual(turns, [])
            finally:
                restored.stopping = True
                restored.thread.join(10)
