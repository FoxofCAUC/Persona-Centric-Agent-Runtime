"""v8 OneBot gate and delivery, with local state and fake platform receipts."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.delivery import DeliveryService
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.qq_bridge import QQBridge, allowed_message
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class OneBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='stage5-qq-unit-')
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.store = Store(self.paths)
        self.store.initialize()
        self.cfg = ConfigStore(self.paths)
        self.cfg.initialize()
        self.cfg.update({'model': {'name': 'deepseek-flash', 'base_url': 'https://offline.invalid/v1'},
                         'bot_qq': '456', 'owner_qq': '123'})
        self.reply = '**重点** ' + '人工长文本' * 150 + '\n```python\nx = 3.14\nprint(x**2)\n```\nhttps://example.invalid/a_b?q=1.25'
        self.calls = []
        async def complete(model, messages):
            self.calls.append((model, messages))
            return self.reply, {'total_tokens': 1, 'mock': True}
        self.engine = ChatEngine(self.store, self.cfg, SimpleNamespace(complete=complete))
        self.addAsyncCleanup(self.engine.wait)
        self.addCleanup(self.engine.cognitive.characters.close)
        self.addCleanup(self.engine.cognitive.shadow.close)
        self.enterContext(patch.object(self.engine.cognitive.characters, '_hybrid', side_effect=SemanticFailure('embedding_unavailable')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('no_external_network')))
        self.pid = self.engine.service.create_persona(generic_persona())['id']
        self.engine.service.use_persona(self.pid)
        self.ingress = IngressService(self.engine)
        old = self.ingress.current_policy(self.pid, 'qq')
        self.ingress.update_policy(self.pid, 'qq', 'natural_batch', old['id'], identifier(),
            companion_mode='minimal_im', reply_mode='context_full', expression_policy_version=3,
            conversation_policy_version=2)
        self.delivery = DeliveryService(self.store)

    def envelope(self, mid, text, reference=None):
        parts = [{'type': 'text', 'data': {'text': text}}]
        if reference:
            parts.insert(0, {'type': 'reply', 'data': {'id': reference}})
        raw = {'message_type': 'private', 'sub_type': 'friend', 'user_id': 123,
               'self_id': 456, 'message_id': mid, 'message': parts}
        event = SimpleNamespace(platform_meta=SimpleNamespace(name='aiocqhttp', id='persona-onebot'),
            message_obj=SimpleNamespace(message_id=mid, raw_message=raw),
            is_private_chat=lambda: True, get_self_id=lambda: '456', get_sender_id=lambda: '123')
        result = allowed_message(event, self.cfg.load())
        self.assertIsNotNone(result)
        return result

    async def seal(self, receipt):
        batch = self.ingress.get(receipt['batch_id'])
        self.ingress.action(batch['id'], 'seal', batch['revision'], identifier(), self.pid)
        await self.engine.wait()
        turn = self.engine.get(self.ingress.get(batch['id'])['turn_id'])
        self.assertEqual(turn['status'], 'completed', turn.get('error_code'))
        return turn

    def bridge(self):
        bridge = QQBridge(self.paths)
        origin = {'sender': '123', 'bot': '456'}
        async def request(method, path, value=None):
            if method == 'GET' and path.startswith('/v2/delivery/streams/'):
                return self.delivery.get(path.rsplit('/', 1)[1], origin=origin)
            stream = self.delivery.by_batch(path.split('/')[3])
            part = path.split('/')[5]
            if path.endswith('/claim'):
                return self.delivery.claim(stream['id'], part, value['expected_revision'], value['operation_id'], origin=origin)
            return self.delivery.result(stream['id'], part, value['claim_token'], value['state'],
                value['expected_revision'], value['operation_id'], platform_message_id=value.get('platform_message_id'),
                error=value.get('error'), origin=origin)
        bridge.request = request
        return bridge

    async def test_onebot_combined_duplicate_and_legal_reference(self):
        first = self.envelope('1001', '人工第一条')
        a = self.ingress.accept(first)
        b = self.ingress.accept(self.envelope('1002', '人工连续补充'))
        self.assertEqual(a['batch_id'], b['batch_id'])
        await self.seal(b)
        self.assertEqual(self.ingress.accept(first)['batch_id'], a['batch_id'])
        c = self.ingress.accept(self.envelope('1003', '接前面的引用', '1001'))
        await self.seal(c)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('人工第一条', str(self.calls[-1][1]))
        self.assertEqual(self.calls[-1][0]['_companion_profile']['version'], 'companion-runtime-v8')

    async def test_native_text_split_and_platform_receipts(self):
        turn = await self.seal(self.ingress.accept(self.envelope('2001', '人工技术文本检查')))
        stream = self.delivery.by_batch(turn['reply_batch']['id'])
        self.assertTrue(all(len(p['text']) <= 450 for p in stream['parts']))
        sent = []
        async def send(text):
            sent.append(text)
            return {'data': {'message_id': 'fake-platform-' + str(len(sent))}}
        self.assertTrue(await self.bridge().deliver_stream(stream, send))
        joined = ''.join(sent)
        for text in ('x = 3.14', 'print(x**2)', 'https://example.invalid/a_b?q=1.25'):
            self.assertIn(text, joined)
        self.assertTrue(all(p['state'] == 'sent' for p in self.delivery.get(stream['id'])['parts']))

    async def test_missing_receipt_becomes_uncertain_and_is_not_replayed(self):
        turn = await self.seal(self.ingress.accept(self.envelope('3001', '人工未知回执检查')))
        stream = self.delivery.by_batch(turn['reply_batch']['id'])
        sent = []
        async def send(text):
            sent.append(text)
            return None
        bridge = self.bridge()
        await bridge.deliver_stream(stream, send)
        view = self.delivery.get(stream['id'])
        self.assertEqual(view['parts'][0]['state'], 'uncertain')
        await bridge.deliver_stream(view, send)
        self.assertEqual(len(sent), 1)

    async def test_known_send_failure_is_recorded(self):
        turn = await self.seal(self.ingress.accept(self.envelope('4001', '人工发送失败检查')))
        stream = self.delivery.by_batch(turn['reply_batch']['id'])
        async def send(text):
            return False
        await self.bridge().deliver_stream(stream, send)
        with self.store.connection() as db:
            states = [r[0] for r in db.execute('SELECT state FROM reply_delivery_attempts')]
        self.assertIn('failed', states)
