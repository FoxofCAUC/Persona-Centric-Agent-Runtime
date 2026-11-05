"""Artificial-only native reference data-flow, accounting and ownership tests."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_native_reference as native
from experiments.companion_reference import _setup
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.chat import ModelCompletion, ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import provider_identity
from persona_runtime.profile import ProfileService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class NativeFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='native-unit-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'tmp/private-acceptance/artificial'
        self.paths = RuntimePaths(self.home)
        self.store = Store(self.paths); self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(generic_persona())['id']; service.use_persona(self.pid)
        self.config = ConfigStore(self.paths); self.config.initialize()
        self.config.update({'model': {'base_url': 'http://127.0.0.1:9/v1', 'name': 'grok-4.7',
            'hourly_limit': 0, 'max_input_chars': 24000}})
        atomic_json(self.home / 'private-sandbox.json', {'formal_home': str(self.base), 'persona_id': self.pid})
        atomic_json(self.home / 'runtime/private-copy-policy.json', {'persona_id': self.pid,
            'approved_models': {'main': provider_identity(self.config.load()['model'])}})
        self.reference = self.base / 'reference/dafeiyu'; self.reference.mkdir(parents=True)
        self.source = {'source_ready': True, 'text_patches_applied': True, 'version': 'artificial-fixed'}
        self.enterContext(patch.object(native, 'source_status', return_value=self.source))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('offline_network_blocked')))
        self.root = self.base / 'tmp/companion-native-reference/trial'; self.root.mkdir(parents=True)
        self.plan = {'version': native.VERSION, 'status': 'prepared', 'source_home': str(self.base),
            'home': str(self.home), 'persona_id': self.pid, 'reference_root': str(self.reference),
            'reasoning_profile': native.REASONING_PROFILE, 'development_material': True,
            'frozen': native.freeze_trial(self.home, self.pid, self.reference)}
        atomic_json(self.root / 'trial.json', self.plan)
        self.data = native.data_root(self.plan)
        self.data.mkdir(parents=True, exist_ok=True)
        self.order = []

    def input(self, turn_id='0001', history=None):
        self.order.append(turn_id)
        folder = self.data / 'turns' / turn_id; folder.mkdir(parents=True)
        atomic_json(folder / 'input.json', {'seq': len(self.order), 'text': '人工新输入', 'history': history or []})
        atomic_json(self.data / 'session.json', {'version': 1, 'order': self.order})
        return folder

    async def upstream(self, root, *, persona, user, history, complete, output_contract, runtime_name, owned_home):
        self.assertEqual(output_contract, '')
        self.assertEqual(owned_home, self.home)
        messages = [{'role': 'system', 'content': '# Persona Instructions\n' + persona}] + history + [{'role': 'user', 'content': user}]
        text = await complete(messages)
        return {'messages': messages, 'raw_text': text, 'after_response_hook': text,
            'processed_text': text, 'captured_messages': [[{'type': 'Plain', 'text': text}]],
            'native_history': history + [{'role': 'user', 'content': user}, {'role': 'assistant', 'content': text}],
            'provider_dispatches': 1}

    def read(self, folder, name='result.json'):
        return json.loads((folder / name).read_text(encoding='utf-8'))

    async def test_plain_flow_and_native_history_without_pcar_contract(self):
        folder = self.input()
        result = await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['simulated_calls'], 1)
        self.assertNotIn('schema_version', result['display'])
        req = self.read(folder, 'request.json')
        self.assertNotIn('response_format', req['wire'])
        self.assertEqual(req['wire']['reasoning_effort'], 'low')
        self.assertNotIn('max_tokens', req['wire'])
        self.assertNotIn('encrypted_key', req['model'])
        self.assertEqual(self.read(folder, 'audit.json')['generation_retry']['attempts'][0]['request_hash'], req['wire_hash'])
        self.assertTrue((folder / 'terminal.json').is_file())
        cleanup = self.read(self.data, 'reference-cleanup.json')
        self.assertTrue(Path(cleanup['paths'][0]).is_relative_to(self.home))
        second = self.input('0002', result['native_history'])
        self.assertEqual((await native.run_turn(self.root, '0002', fake=True, upstream=self.upstream))['status'], 'completed')
        self.assertEqual(self.read(second, 'request.json')['messages'][1:-1], result['native_history'])
        with self.assertRaisesRegex(Exception, 'already_started'):
            await native.run_turn(self.root, '0002', fake=True, upstream=self.upstream)

    async def test_hidden_disabled_and_invalid_dependencies_never_reach_native_materials(self):
        character = CharacterService(self.store); data = empty_character()
        def lore(key, text, enabled=True, visibility='conversation', dependencies=None):
            return {'id': key, 'title': key, 'category': '人工', 'keywords': [], 'text': text,
                'priority': 0, 'enabled': enabled, 'visibility': visibility, 'dependencies': dependencies or []}
        data['lorebook'] = [lore('shown', '公开材料'), lore('hidden', '隐藏原稿', visibility='creator_only'),
            lore('disabled', '禁用材料', enabled=False), lore('depends', '依赖禁用不可用', dependencies=['disabled'])]
        data['examples'] = [{'id': 'yes', 'scenario': '人工', 'keywords': [], 'text': '公开样例', 'enabled': True},
                            {'id': 'no', 'scenario': '人工', 'keywords': [], 'text': '禁用样例', 'enabled': False}]
        old = character.current(self.pid)
        character.update(self.pid, data, old['id'], identifier())
        text, details = native.visible_persona(self.store, self.pid)
        for excluded in ('隐藏原稿', '禁用材料', '依赖禁用不可用', '禁用样例'):
            self.assertNotIn(excluded, text)
        self.assertIn('公开材料', text); self.assertIn('公开样例', text)
        self.assertEqual(details['visible_lore_count'], 1)

    async def test_profile_change_rejects_before_start(self):
        folder = self.input()
        profiles = ProfileService(self.store); old = profiles.current('expression')
        profiles.update('expression', {'style': '另一个人工表达版本'}, old['id'], identifier())
        with self.assertRaisesRegex(Exception, 'frozen_conditions_changed'):
            await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream)
        self.assertFalse((folder / 'started.json').exists())

    async def test_unfinished_and_changed_history_block_new_turn(self):
        self.input()
        folder = self.input('0002')
        with self.assertRaisesRegex(Exception, 'previous_turn_unfinished'):
            await native.run_turn(self.root, '0002', fake=True, upstream=self.upstream)
        self.assertFalse((folder / 'started.json').exists())

    async def test_invalid_root_rejected_before_lock_and_failed_prior_stops(self):
        outside = self.base / 'unowned'
        with self.assertRaises(Exception):
            await native.run_turn(outside, '0001', fake=True, upstream=self.upstream)
        self.assertFalse(outside.exists())
        first = self.input()
        atomic_json(first / 'terminal.json', {'status': 'failed'})
        atomic_json(first / 'result.json', {'status': 'failed'})
        second = self.input('0002')
        with self.assertRaisesRegex(Exception, 'previous_turn_failed'):
            await native.run_turn(self.root, '0002', fake=True, upstream=self.upstream)
        self.assertFalse((second / 'started.json').exists())

    async def test_fake_scope_cannot_be_run_as_paid(self):
        folder = self.input()
        with self.assertRaisesRegex(Exception, 'simulation_scope_mismatch'):
            await native.run_turn(self.root, '0001', fake=False, upstream=self.upstream)
        self.assertFalse((folder / 'started.json').exists())

    async def test_transport_retry_recovers_once_without_quality_retry(self):
        folder = self.input(); calls = []
        class Client:
            async def complete(self, model, messages):
                calls.append(messages)
                if len(calls) == 1: raise ModelError('provider_network_error')
                return ModelCompletion('合法但风格可能很差的人工正文', None, {'finish_reason': 'stop'})
        async def no_delay(_): pass
        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            result = await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream, client=Client())
        self.assertEqual(result['status'], 'completed'); self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(list(folder.glob('*.reserve.json'))), 2)

    async def test_retry_exhaustion_has_four_attempts_and_terminal(self):
        self.input()
        class Client:
            async def complete(self, model, messages): raise ModelError('provider_network_error')
        async def no_delay(_): pass
        with patch('persona_runtime.generation_retry.retry_delay', no_delay):
            result = await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream, client=Client())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'provider_retries_exhausted')
        self.assertEqual(result['generation_attempts'], 4)

    async def test_cancel_marker_settles_active_physical_and_never_delivers(self):
        folder = self.input(); entered = asyncio.Event()
        class Client:
            async def complete(self, model, messages):
                entered.set(); await asyncio.Event().wait()
        task = asyncio.create_task(native.run_turn(self.root, '0001', fake=True, upstream=self.upstream, client=Client()))
        await entered.wait(); atomic_json(folder / 'cancel.json', {'reason': 'user'})
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result['status'], 'cancelled'); self.assertNotIn('display', result)
        self.assertEqual(result['usage']['generation_retry']['attempts'][0]['status'], 'interrupted')

    async def test_postprocessing_failure_does_not_repeat_completed_generation(self):
        folder = self.input()
        async def bad(*args, **kwargs):
            value = await self.upstream(*args, **kwargs)
            value['captured_messages'] = [[{'type': 'Image', 'url': 'artificial'}]]
            return value
        result = await native.run_turn(self.root, '0001', fake=True, upstream=bad)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['generation_attempts'], 1)
        self.assertEqual(result['usage']['generation_retry']['attempts'][0]['status'], 'completed')
        self.assertTrue((folder / 'generation-result.json').is_file())

    async def test_write_failure_never_creates_success_terminal(self):
        folder = self.input(); real = native.atomic_json
        def fail(path, value):
            if Path(path).name == 'terminal.json': raise OSError('artificial-write-failure')
            real(path, value)
        with patch.object(native, 'atomic_json', fail):
            with self.assertRaises(OSError):
                await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream)
        self.assertFalse((folder / 'terminal.json').exists())
        with self.assertRaisesRegex(Exception, 'already_started'):
            await native.run_turn(self.root, '0001', fake=True, upstream=self.upstream)

    async def test_face_display_preserves_upstream_components(self):
        value = {'after_response_hook': '人工[微笑]', 'captured_messages': [[{'type': 'Plain', 'text': '人工'}, {'type': 'Face', 'id': 123}]]}
        view = native.native_display(value, self.reference)
        self.assertEqual(view['display'], '人工[QQ表情:123]')
        self.assertEqual(view['captured_messages'], value['captured_messages'])

    async def test_only_proven_native_suppression_is_successful_without_display(self):
        self.input()
        async def suppressed(*args, **kwargs):
            value = await self.upstream(*args, **kwargs)
            value.update(captured_messages=[], processed_text='', suppressed=True,
                suppression_reason='upstream_no_reply_marker', after_response_hook='[NO_REPLY]')
            return value
        result = await native.run_turn(self.root, '0001', fake=True, upstream=suppressed)
        self.assertEqual(result['status'], 'completed'); self.assertTrue(result['suppressed'])
        self.assertEqual(result['display'], '')
        self.assertEqual(result['generation_attempts'], 1)
        with self.assertRaisesRegex(Exception, 'capture_missing'):
            native.native_display({'captured_messages': [], 'processed_text': ''}, self.reference)

    async def test_wrong_owned_runtime_scope_rejected_before_upstream_import(self):
        with patch('experiments.companion_reference.source_status', return_value=self.source):
            with self.assertRaisesRegex(Exception, 'private_path_outside_root'):
                _setup(self.reference, self.base / 'outside', owned_home=self.home)

    async def incident(self):
        folder = self.input()
        class Client:
            async def complete(self, model, messages):
                return ModelCompletion('人工第一段\n人工第二段\n人工第三段', None, {'finish_reason': 'stop'})
        async def broken(*args, **kwargs):
            await self.upstream(*args, **kwargs)
            raise IndexError('artificial interval index')
        result = await native.run_turn(self.root, '0001', fake=True, upstream=broken, client=Client())
        self.assertEqual(result['error_code'], 'IndexError')
        self.plan['frozen']['source_hashes'].update(native.RECOVERY_OLD_HASHES)
        atomic_json(self.root / 'trial.json', self.plan)
        return folder

    async def recovered_upstream(self, *args, **kwargs):
        value = await self.upstream(*args, **kwargs)
        value['captured_messages'] = [[{'type': 'Plain', 'text': line}]
                                     for line in value['raw_text'].splitlines()]
        return value

    async def test_explicit_recovery_keeps_originals_and_continuous_actual_history(self):
        from experiments.companion_native_trial import read_session
        folder = await self.incident()
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        with patch.object(native.ChatCompletionsClient, 'complete', side_effect=AssertionError('no_generation')):
            result = await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
        self.assertEqual(len(result['bubbles']), 3)
        self.assertEqual(result['simulated_calls'], 1)
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['native_history'][-1]['content'], '人工第一段\n人工第二段\n人工第三段')
        self.assertNotEqual(result['display'], result['native_history'][-1]['content'])
        for name, value in before.items(): self.assertEqual((folder / name).read_bytes(), value)
        self.assertEqual(self.read(folder)['status'], 'failed')
        self.assertEqual(native.effective_result(folder)['status'], 'completed')
        state, history, visible, interrupted = read_session(self.data)
        self.assertFalse(interrupted); self.assertEqual(history, result['native_history'])
        self.assertEqual(visible[0]['display'], result['display'])
        async def forbidden(*args, **kwargs): raise AssertionError('replayed successful recovery')
        self.assertEqual(await native.recover_turn(self.root, '0001', upstream=forbidden), result)
        self.input('0002', history)
        self.assertEqual((await native.run_turn(self.root, '0002', fake=True, upstream=self.upstream))['status'], 'completed')

    async def test_recovery_rejects_changed_response_request_and_cancel_before_replay(self):
        folder = await self.incident()
        for filename, mutate in (
            ('generation-result.json', lambda d: d.update(raw_text='tampered')),
            ('request.json', lambda d: d['messages'][0].update(content='changed')),
            ('audit.json', lambda d: d['generation_retry']['attempts'].append(deepcopy(d['generation_retry']['attempts'][0]))),
        ):
            with self.subTest(filename=filename):
                saved = (folder / filename).read_bytes(); value = self.read(folder, filename); mutate(value)
                atomic_json(folder / filename, value)
                with self.assertRaises(Exception):
                    await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
                self.assertFalse((folder / 'postprocess-recovery').exists())
                (folder / filename).write_bytes(saved)
        atomic_json(folder / 'cancel.json', {'reason': 'artificial'})
        with self.assertRaisesRegex(Exception, 'cancelled'):
            await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
        self.assertFalse((folder / 'postprocess-recovery').exists())

    async def test_recovery_rejects_non_source_and_unregistered_source_changes(self):
        folder = await self.incident()
        original_config = self.config.load()
        self.config.update({'model': {'max_input_chars': 25000}})
        with self.assertRaisesRegex(Exception, 'non_source_conditions_changed'):
            await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
        atomic_json(self.config.file, original_config)
        self.plan['frozen']['source_hashes']['persona_runtime/chat.py'] = '0' * 64
        atomic_json(self.root / 'trial.json', self.plan)
        with self.assertRaisesRegex(Exception, 'migration_not_allowed'):
            await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
        self.assertFalse((folder / 'postprocess-recovery').exists())

    async def test_recovery_changed_replay_messages_stops_without_retry(self):
        folder = await self.incident(); calls = []
        async def changed(root, **kwargs):
            calls.append(1)
            return await kwargs['complete']([{'role': 'system', 'content': 'different'}])
        with self.assertRaisesRegex(Exception, 'replay_request_changed'):
            await native.recover_turn(self.root, '0001', upstream=changed)
        self.assertEqual(self.read(folder, 'postprocess-recovery/result.json')['status'], 'failed')
        with self.assertRaisesRegex(Exception, 'receipt_invalid'):
            await native.recover_turn(self.root, '0001', upstream=changed)
        self.assertEqual(calls, [1]); self.assertEqual(self.read(folder)['status'], 'failed')

    async def test_recovery_migration_write_failure_is_checkpoint_not_replay(self):
        folder = await self.incident(); write = native.atomic_json
        def fail(path, value):
            if Path(path).name == 'source-migration.json': raise OSError('artificial-write-failure')
            write(path, value)
        with patch.object(native, 'atomic_json', side_effect=fail):
            with self.assertRaises(OSError):
                await native.recover_turn(self.root, '0001', upstream=self.recovered_upstream)
        receipt = (folder / 'postprocess-recovery/result.json').read_bytes()
        async def forbidden(*args, **kwargs): raise AssertionError('replay forbidden')
        with self.assertRaisesRegex(Exception, 'frozen_conditions_changed'):
            await native.recover_turn(self.root, '0001', upstream=forbidden)
        self.assertEqual((folder / 'postprocess-recovery/result.json').read_bytes(), receipt)

    async def test_recovery_blocks_async_network_before_upstream_dispatch(self):
        folder = await self.incident()
        async def network(root, **kwargs):
            await asyncio.open_connection('invalid.example', 443)
            raise AssertionError('network was not blocked')
        with self.assertRaisesRegex(Exception, 'offline_network_blocked'):
            await native.recover_turn(self.root, '0001', upstream=network)
        record = self.read(folder, 'postprocess-recovery/result.json')
        self.assertEqual(record['new_generation_calls'], 0)
        self.assertEqual(record['stored_response_replays'], 0)


if __name__ == '__main__':
    unittest.main()
