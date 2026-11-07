"""Stage 5-C orchestration; fake generation and fake platform receipts only."""
import asyncio
import json
import socket
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments import companion_qq_acceptance as run
from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.chat import ChatEngine, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.delivery import DeliveryService
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.qq_testing import QQTesting
from persona_runtime.storage import Store


class Channel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='qq-acceptance-unit-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.out = self.home / 'evidence'
        cfg = ConfigStore(RuntimePaths(self.home)); cfg.initialize()
        cfg.update({'bot_qq': '456', 'owner_qq': '123',
                    'model': {'base_url': 'https://offline.invalid', 'name': 'deepseek-flash', 'hourly_limit': 0}})
        QQTesting(RuntimePaths(self.home)).configure('123', '789')
        run.prepare(self.out, self.home, fixture=True, stage_components=False)
        self.owner, self.paths = run.verify(self.out)
        self.calls = 0
        self.engine = None

    def start_engine(self, target=None):
        outer = self
        class Client:
            async def complete(self, model, messages):
                outer.calls += 1
                if outer.recorder.current['kind'] == 'cancel': await asyncio.sleep(30)
                return (run.cases()[3]['expected_visible'] if outer.recorder.current['kind'] == 'long' else '收到。'), {'total_tokens': 1, 'mock': True}
        self.engine = ChatEngine(Store(self.paths), ConfigStore(self.paths))
        self.recorder = run.Recorder(self.out, self.engine, target or Client())
        self.engine.client = self.recorder
        self.engine.evaluation_session = self.recorder
        self.engine.generation_audit_sink = self.recorder.save
        self.enterContext(patch.object(self.engine.cognitive.characters, '_hybrid', side_effect=SemanticFailure('embedding_unavailable')))
        original = socket.socket.connect
        def local_only(sock, address):
            if address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('external_network_disabled')
            return original(sock, address)
        self.enterContext(patch('socket.socket.connect', new=local_only))
        return IngressService(self.engine)

    async def asyncTearDown(self):
        if self.engine:
            await self.engine.wait()
            self.engine.cognitive.characters.close(); self.engine.cognitive.shadow.close()

    def accept(self, ingress, case):
        cfg = ConfigStore(self.paths).load()
        for index, text in enumerate(case['inputs']):
            value = {'channel': 'qq', 'text': text, 'request_id': case['id'] + '-' + str(index),
                     'bot': cfg['bot_qq'], 'sender': cfg['owner_qq'], 'platform_message_id': case['id'] + '-' + str(index)}
            if case['kind'] == 'reference':
                value['reference'] = {'kind': 'platform_message', 'message_id': 'Q01-0'}
            receipt = ingress.accept(value)
            self.assertEqual(receipt['batch_id'], ingress.accept(value)['batch_id'])
        batch = ingress.get(receipt['batch_id'])
        if batch['state'] in ('collecting', 'interrupted_buffer', 'sealed_waiting_capacity'):
            ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.owner['persona_id'])
        return ingress.get(batch['id'])['turn_id']

    async def deliver(self, turn_id):
        now = [0.0]
        def clock():
            now[0] += 10
            return now[0]
        delivery = DeliveryService(self.engine.store, monotonic=clock)
        turn = self.engine.get(turn_id)
        if turn.get('reply_batch'):
            stream = delivery.by_batch(turn['reply_batch']['id'])
            for part in stream['parts']:
                if part['state'] in ('sent', 'skipped', 'resolved'):
                    continue
                claim = delivery.claim(stream['id'], part['id'], stream['revision'], identifier(),
                    capabilities=['reply_bubbles_v3', 'reply_batch_v2', 'reply_rhythm_v1', 'reply_display_messages_v1'])
                self.assertTrue(claim['claimed'], claim)
                value = delivery.result(stream['id'], part['id'], claim['claim_token'], 'sent',
                                        claim['part_revision'], identifier(), platform_message_id='fake-' + part['id'])
                stream = delivery.get(stream['id'])

    async def test_six_positions_cancel_and_clean_http_app_reopen(self):
        ingress = self.start_engine()
        for case in run.cases():
            if case['id'] == 'Q06':
                before = run.checkpoint(self.paths)
                await self.engine.wait()
                self.engine.cognitive.characters.close(); self.engine.cognitive.shadow.close()
                self.engine = None
                runtime = run.Runtime(self.out, target=SimpleNamespace(complete=self.recorder.target.complete))
                with patch('persona_runtime.character_runtime.CharacterRuntime._hybrid', side_effect=SemanticFailure('embedding_unavailable')):
                    await runtime.start()
                    self.assertEqual(before, run.checkpoint(self.paths))
                    await runtime.stop()
                    runtime.loop.close()
                run.append(self.out / 'reopen.json', {'history_unchanged': True, 'startup_generation_calls': 0})
                ingress = self.start_engine()
            turn_id = self.accept(ingress, case)
            await self.engine.wait()
            await self.deliver(turn_id)
            row = self.recorder.settle()
            self.assertTrue(row['coverage_passed'], row)
            self.assertFalse(any(row['auxiliary_counts'].values()))
        self.assertEqual(self.calls, 6)
        self.assertTrue(run.report(self.out)['real_channel_complete'])
        self.assertFalse(any(run.unresolved(self.paths).values()))
        self.assertEqual(run.read(self.out / 'positions/Q05.json')['status'], 'interrupted')
        with self.assertRaisesRegex(Exception, 'fixture_cannot_listen'):
            run.listen(self.out)

    async def test_wrong_input_stops_before_provider(self):
        ingress = self.start_engine()
        case = dict(run.cases()[0], inputs=['不在冻结清单中的消息'])
        tid = self.accept(ingress, case)
        await self.engine.wait()
        self.assertEqual(self.calls, 0)
        self.assertNotEqual(self.engine.get(tid)['status'], 'completed')
        self.assertFalse(self.recorder.blocked)
        self.assertFalse(self.engine.draining)
        self.assertEqual(len(list((self.recorder.body/'input-rejections').glob('*.json'))),1)

    async def test_new_correct_input_after_rejection_needs_no_restart_or_replay(self):
        ingress=self.start_engine()
        wrong=dict(run.cases()[0],id='operator-mistake',inputs=['人工清单外输入'])
        rejected=self.accept(ingress,wrong);await self.engine.wait()
        self.assertEqual(self.calls,0)
        correct=self.accept(ingress,run.cases()[0]);await self.engine.wait()
        await self.deliver(correct);row=self.recorder.settle()
        self.assertTrue(row['coverage_passed'])
        self.assertNotEqual(rejected,correct)
        self.assertEqual(self.calls,1)
        self.assertFalse(self.engine.draining)
        self.assertEqual(self.engine.get(rejected)['error_code'],'qq_acceptance_unexpected_input')
        request=run.read(self.recorder.body/'Q01/request.json')
        self.assertNotIn('人工清单外输入',str(request['messages']))

    async def test_reference_position_without_actual_quote_rejected(self):
        ingress = self.start_engine()
        for case in run.cases()[:2]:
            tid = self.accept(ingress, case); await self.engine.wait()
            await self.deliver(tid); self.recorder.settle()
        missing_quote = dict(run.cases()[2], kind='single')
        tid = self.accept(ingress, missing_quote); await self.engine.wait()
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(self.engine.get(tid)['status'], 'completed')
        self.assertTrue(self.recorder.blocked)

    async def test_changed_author_or_policy_rejected(self):
        cfg = ConfigStore(self.paths)
        cfg.update({'model': {'name': 'changed'}})
        with self.assertRaisesRegex(Exception, 'frozen_conditions_changed'):
            run.verify(self.out)

    async def test_prepare_inherits_dedicated_bot_and_same_sender(self):
        source=QQTesting(RuntimePaths(self.home))
        with patch.object(source,'_stopped'):
            source.designate_bots('456','987','123')
        target=self.home/'bot-v2-evidence'
        run.prepare(target,self.home,fixture=True,stage_components=False)
        owner,paths=run.verify(target)
        cfg=ConfigStore(paths).load()
        self.assertEqual((cfg['bot_qq'],cfg['owner_qq']),('987','123'))
        self.assertEqual(QQTesting(paths).load()['version'],2)
        QQTesting(paths).require_ready('123')

    async def test_success_and_completed_position_not_replayed(self):
        ingress = self.start_engine()
        tid = self.accept(ingress, run.cases()[0]); await self.engine.wait()
        await self.deliver(tid); self.recorder.settle()
        self.assertEqual(self.calls, 1)
        duplicate = self.accept(ingress, run.cases()[0]); await self.engine.wait()
        self.assertEqual(duplicate, tid)
        self.assertEqual(self.calls, 1)
        with self.assertRaises(FileExistsError):
            run.append(self.out / 'positions/Q01.json', {'overwritten': True})

    async def test_native_retry_recovers_without_new_position(self):
        outer = self
        class Client:
            async def complete(self, model, messages):
                outer.calls += 1
                if outer.calls == 1: raise ModelError('provider_empty_response')
                return '收到。', {'total_tokens': 1, 'mock': True}
        ingress = self.start_engine(Client())
        with patch('persona_runtime.generation_retry.retry_delay', return_value=None):
            tid = self.accept(ingress, run.cases()[0]); await self.engine.wait()
        await self.deliver(tid)
        row = self.recorder.settle()
        self.assertTrue(row['coverage_passed'])
        self.assertEqual((self.calls, row['physical_attempts']), (2, 2))

    async def test_retries_exhausted_are_terminal_and_no_automatic_replay(self):
        outer = self
        class Client:
            async def complete(self, model, messages):
                outer.calls += 1
                raise ModelError('provider_empty_response')
        ingress = self.start_engine(Client())
        with patch('persona_runtime.generation_retry.retry_delay', return_value=None):
            self.accept(ingress, run.cases()[0]); await self.engine.wait()
        row = self.recorder.settle()
        self.assertFalse(row['coverage_passed'])
        self.assertTrue(self.recorder.blocked)
        self.assertEqual((self.calls, row['physical_attempts']), (4, 4))

    async def test_receipt_write_failure_blocks_next_dispatch(self):
        ingress = self.start_engine()
        original = run.append
        def fail(path, value):
            if str(path).endswith('.reserve.json'): raise OSError('simulated_storage_error')
            return original(path, value)
        with patch.object(run, 'append', side_effect=fail):
            self.accept(ingress, run.cases()[0]); await self.engine.wait()
        self.assertEqual(self.calls, 0)
        self.assertTrue(self.recorder.blocked)

    async def test_cleanup_requires_review_and_remote_confirmation(self):
        with self.assertRaisesRegex(Exception, 'review_and_qq_cleanup_required'):
            run.close(self.out)
        self.assertTrue(self.paths.home.exists())

    async def test_staging_includes_napcat_host_python_entry(self):
        run.stage_launcher(self.paths)
        self.assertTrue((self.paths.home / '.venv/Scripts/python.exe').is_file())
        self.assertEqual((self.paths.home / '.venv/Lib/site-packages/pcar-owned-dependencies.pth').read_text(encoding='utf-8').strip(),
                         str(self.paths.home / 'components/astrbot/.venv/Lib/site-packages'))

    async def test_public_owner_does_not_copy_key_or_persona_body(self):
        owner = (self.out / 'owner.json').read_text(encoding='utf-8')
        self.assertNotIn('encrypted_key', owner)
        self.assertNotIn('core_traits', owner)
        self.assertNotIn('expression_style', owner)
        self.assertEqual(self.owner['startup_generation_calls'], 0)

    async def test_closed_report_preserves_confirmed_settlement(self):
        settled={'cleanup':'complete','mobile_confirmation':'confirmed','stage_complete':True}
        run.atomic_json(self.out/'settlement.json',settled)
        self.assertEqual(run.report(self.out),settled)

    async def test_closed_verify_checks_evidence_and_absent_roots_without_recreating_them(self):
        artifact=self.out/'body-free.json';run.atomic_json(artifact,{'count':6})
        absent=self.home/'tmp/private-acceptance/removed-copy'
        seal={'version':run.VERSION,'stage_complete':True,'source_home':str(self.home),
              'evidence_hashes':{'body-free.json':run.sha(artifact)},'cleanup_roots':[str(absent)],
              'closing_code_hashes':run.code_hashes()}
        run.atomic_json(self.out/'closure.json',seal)
        self.assertTrue(run.verify_closed(self.out)['cleanup_verified'])
        self.assertFalse(absent.exists())
        absent.mkdir(parents=True)
        with self.assertRaisesRegex(Exception,'closed_cleanup_incomplete'):run.verify_closed(self.out)
        absent.rmdir()
        run.atomic_json(artifact,{'count':7})
        with self.assertRaisesRegex(Exception,'closed_evidence_changed'):run.verify_closed(self.out)
