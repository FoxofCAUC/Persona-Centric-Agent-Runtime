"""Native continuity is local business data, never a cloud-window dependency."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from persona_runtime.chat import ChatEngine, ModelError
from persona_runtime.knowledge_native_memory import previous, snapshot, validate_snapshot
from persona_runtime.models import identifier, RuntimeErrorBase
from persona_runtime.organization import OrganizationEngine
from tests import test_general_retrieval as general_fixture
from tests import test_m9r_native_retrieval as native_fixture
from tests.test_m9r0_native_contract import local_temp


class Continuity(unittest.IsolatedAsyncioTestCase):
    setUp = native_fixture.Integration.setUp
    asyncTearDown = native_fixture.Integration.asyncTearDown
    enable = native_fixture.Integration.enable
    ask = native_fixture.Integration.ask
    decision_transport = native_fixture.Integration.decision_transport
    transport = native_fixture.Integration.transport

    async def seed(self):
        self.enable()
        turn = await self.ask(public={'question': '游戏攻略和走位装备'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        with self.store.connection() as db:
            record = json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',
                ('knowledge.conversation.record.' + turn['id'],)).fetchone()[0])['data']
        return turn, record

    async def stop_worker(self):
        self.org.draining = True
        self.worker.cancel(); await asyncio.gather(self.worker, return_exceptions=True)
        await self.chat.wait(); self.chat.knowledge.close()
        self.worker = None

    async def test_real_engine_recreation_reuses_readable_material_without_native_redispatch(self):
        await self.seed(); await self.stop_worker()
        self.chat = ChatEngine(self.store, self.cfg, general_fixture.Main())
        self.main = self.chat.client; self.provider = native_fixture.Provider()
        self.org = OrganizationEngine(self.store, self.cfg, self.provider, self.chat)
        self.need_web = False; self.org.routing.transport = self.decision_transport
        self.worker = asyncio.create_task(self.org.worker())
        turn = await self.ask('继续刚才的游戏走位攻略')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(self.provider.calls, [])
        self.assertIn('先练走位', self.main.calls[-1][0]['content'])

    async def test_new_session_recovers_matching_material_locally(self):
        await self.seed(); self.chat.session('desktop', fresh=True); self.need_web = False
        turn = await self.ask('上次的游戏走位和换装备细节继续说说')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIn('先练走位', self.main.calls[-1][0]['content'])

    async def test_same_session_new_web_task_replays_whole_protocol_unit(self):
        await self.seed()
        turn = await self.ask('再查一处细节', public={'question': '继续查询公开游戏装备的另一种方案'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(len(self.provider.calls), 2)
        messages = self.provider.calls[-1][1]
        self.assertEqual([item['role'] for item in messages], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(messages[2]['server_tool_calls'][0]['calls'][0]['signature'], 'synthetic-opaque-signature')

    async def messages_after_seed(self, *, budget=24000, key=None):
        turn, record = await self.seed()
        inputs = {'public_task': {'question': '继续公开查询', 'intent': 'search', 'depth': 'ordinary'},
            'public_context': record['organized']['notes'], 'protocol_parent': record['native']['native_attempt_id']}
        model = self.cfg.load()['model'] | {'max_input_chars': budget}
        if key is not None: model['encrypted_key'] = key
        task = {'id': 'new', 'persona_id': self.persona['id'], 'channel': 'desktop',
            'session_id': turn['session_id'], 'model_snapshot': json.dumps(model)}
        payload = {'knowledge_processing': {'input': inputs}}
        with self.store.connection() as db:
            return self.org.processing.native.messages_for_task(db, task, payload)

    async def test_context_crop_drops_a_complete_unit_and_uses_existing_readable_summary(self):
        messages, meta = await self.messages_after_seed(budget=500)
        self.assertFalse(meta['replayed']); self.assertTrue(meta['causal_unit_dropped'])
        self.assertEqual(len(messages), 2)
        self.assertIn('先练走位', messages[1]['content'])
        self.assertNotIn('signature', json.dumps(messages))

    async def test_changed_account_loses_only_protocol_not_public_knowledge(self):
        messages, meta = await self.messages_after_seed(key='other-account-binding')
        self.assertFalse(meta['replayed']); self.assertIn('先练走位', messages[1]['content'])
        self.assertNotIn('signature', json.dumps(messages))

    async def test_correction_clears_blended_old_note_and_supplies_corrected_readable_material(self):
        _, record = await self.seed(); source = record['sources'][0]
        self.org.processing.native.update_source(self.persona['id'], source['id'], 'corrected', 0,
            identifier(), replacement='更正：先调整站位，暂时不要换装备。')
        self.need_web = False
        turn = await self.ask('按更正后的资料再说一下')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        system = self.main.calls[-1][0]['content']
        self.assertIn('先调整站位', system); self.assertNotIn('公开任务资料：先练走位', system)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_withdrawal_and_source_exclusion_do_not_resurrect_notes(self):
        first, record = await self.seed(); source = record['sources'][0]
        self.org.processing.native.update_source(self.persona['id'], source['id'], 'withdrawn', 0, identifier())
        self.need_web = False
        turn = await self.ask('刚才资料先不采用，聊点别的')
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertNotIn('公开任务资料：先练走位', self.main.calls[-1][0]['content'])
        self.assertEqual(self.chat.get(first['id'])['status'], 'completed')

    async def test_portable_readable_data_excludes_protocol_and_rebinds_persona(self):
        _, record = await self.seed()
        with self.store.connection() as db: exported = snapshot(db, self.persona['id'])
        validate_snapshot(exported)
        text = json.dumps(exported)
        self.assertNotIn('signature', text); self.assertNotIn('reasoning_content', text)
        self.assertNotIn('account_binding', text); self.assertNotIn('native_attempt', text)
        from persona_runtime.models import generic_persona
        owner = self.service.create_persona(generic_persona())['id']
        from persona_runtime.knowledge_native_memory import merge_snapshot
        with self.store.connection(write=True) as db:
            merge_snapshot(db, exported, owner)
            result = previous(db, {'id': 'new', 'persona_id': owner, 'channel': 'desktop', 'session_id': 'new-session'}, '游戏走位装备')
        self.assertIn('先练走位', result['organized']['notes'])
        self.assertNotEqual(result['sources'][0]['id'], record['sources'][0]['id'])
        self.assertFalse(self.chat.knowledge.current_policy(owner)['data']['enabled'])
        self.assertFalse(self.org.processing.policy(owner)['data']['enabled'])

    async def test_persona_business_export_contains_readable_material_without_execution_packets(self):
        await self.seed()
        result = self.service.export_persona('m9r-readable-export')
        package = json.loads(Path(result['path']).read_text(encoding='utf-8'))
        self.assertTrue(package['knowledge']['native_readable']['records'])
        self.assertNotIn('server_tool_calls', json.dumps(package['knowledge']))
        from persona_runtime.portable_memory import import_export
        imported = import_export(self.store, package, self.persona['id'], identifier())
        with self.store.connection() as db:
            material = previous(db, {'id': 'new', 'persona_id': imported['id'], 'channel': 'desktop', 'session_id': 's'}, '游戏走位装备')
        self.assertIn('先练走位', material['organized']['notes'])

    async def test_received_native_result_can_finish_publication_without_a_second_call(self):
        self.enable(); real_accept = self.org.processing.native.accept
        def fail(*args): raise RuntimeErrorBase('synthetic_publication_failure')
        real_main = self.main.complete
        async def failed_main(*args): raise ModelError('provider_http_400')
        self.main.complete = failed_main
        with patch.object(self.org.processing.native, 'accept', side_effect=fail):
            turn = await self.ask(public={'question': '公开游戏攻略'})
        self.assertEqual(turn['status'], 'failed'); self.assertEqual(len(self.provider.calls), 1)
        self.main.complete = real_main
        await self.chat.retry(turn['id']); await asyncio.wait_for(self.chat.wait(), 30)
        self.assertEqual(self.chat.get(turn['id'])['status'], 'completed')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIn('先练走位', self.main.calls[-1][0]['content'])

    async def test_journal_draft_identity_includes_public_task(self):
        from persona_runtime.submissions import SubmissionJournal
        journal = SubmissionJournal(self.paths, 'desktop')
        base = {'channel': 'desktop', 'persona_id': self.persona['id'], 'text': '相同聊天草稿'}
        first = journal.remember(base | {'request_id': 'a', 'public_task': {'question': '公开甲'}}, match_draft=True)
        second = journal.remember(base | {'request_id': 'b', 'public_task': {'question': '公开乙'}}, match_draft=True)
        self.assertNotEqual(first['request_id'], second['request_id'])
        repeated = journal.remember(base | {'request_id': 'c', 'public_task': {'question': '公开甲'}}, match_draft=True)
        self.assertEqual(repeated['request_id'], first['request_id'])

    async def test_source_exclusion_prevents_future_native_material_reuse(self):
        turn, _ = await self.seed()
        from persona_runtime.memory_policy import exclude_sources
        with self.store.connection(write=True) as db:
            event = db.execute('SELECT user_event_id FROM turns WHERE id=?', (turn['id'],)).fetchone()[0]
            exclude_sources(db, self.persona['id'], [event], 'explicit_withdrawal')
        self.need_web = False
        await self.ask('游戏走位之前那条不再采用')
        self.assertNotIn('公开任务资料：先练走位', self.main.calls[-1][0]['content'])

    async def test_full_business_migration_preserves_material_but_pauses_native_authority(self):
        self.enable()
        self.cfg.update({'model': {'api_key': 'synthetic-m9r-main-key'}})
        turn = await self.ask(public={'question': '公开游戏走位装备'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        await self.stop_worker()
        from persona_runtime.migration import export_instance, import_instance
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        exported = export_instance(self.paths, 'm9r-artificial-business.zip')
        with local_temp() as folder:
            target = RuntimePaths(Path(folder) / '中文 新实例')
            import_instance(target, Path(exported['path']), apply=True)
            migrated = Store(target)
            with migrated.connection() as db:
                from persona_runtime.knowledge_search_notice import read as read_search_notice
                migrated_turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
                self.assertEqual(read_search_notice(db,migrated_turn)['text'],turn['search_notice']['text'])
                policy = json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',
                    ('knowledge.native.policy.' + self.persona['id'],)).fetchone()[0])
                self.assertFalse(policy['data']['enabled'])
                self.assertTrue(policy['data']['migration_requires_review'])
                material = previous(db, {'id': 'new', 'persona_id': self.persona['id'], 'channel': 'desktop', 'session_id': 'new'}, '走位装备')
                self.assertIn('先练走位', material['organized']['notes'])
                row = db.execute("SELECT t.*,a.id AS aid FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
                from persona_runtime.knowledge_native_store import load_receipt
                with self.assertRaisesRegex(RuntimeErrorBase, 'scope_mismatch'):
                    load_receipt(db, dict(row), row['aid'])

    async def test_v2_buffered_public_projection_never_collects_other_private_messages(self):
        self.enable()
        from persona_runtime.ingress import IngressService
        ingress = IngressService(self.chat)
        first = ingress.accept({'channel': 'desktop', 'text': '不用于检索的私聊片段', 'request_id': identifier()})
        second = ingress.accept({'channel': 'desktop', 'text': '再查公开主题', 'request_id': identifier(),
            'public_task': {'question': '公开的游戏走位装备'}})
        with self.store.connection() as db:
            raw = dict(db.execute('SELECT * FROM turns WHERE id=?', (second['turn_id'],)).fetchone()) if second.get('turn_id') else {
                'id': 'pending', 'persona_id': self.persona['id'], 'channel': 'desktop', 'session_id': second['session_id'],
                'input_batch_id': second['batch_id'], 'user_event_id': None}
            projected = self.org.processing.native.projection(db, raw, '不用于检索的私聊片段 再查公开主题', self.org.processing.native.policy(self.persona['id']))
        self.assertEqual(projected['question'], '公开的游戏走位装备')
        self.assertNotIn('私聊片段', json.dumps(projected, ensure_ascii=False))
        await self.chat.wait()

    async def test_global_network_switch_suppresses_jev_and_native_tools(self):
        self.enable(); pid = self.persona['id']
        policy = self.chat.knowledge.current_policy(pid)
        self.chat.knowledge.update_policy(pid, {'enabled': False, 'channels': ['desktop']}, policy['revision'], identifier())
        turn = await self.ask(public={'question': '公开查询'})
        self.assertEqual(turn['status'], 'completed', turn['error_code'])
        self.assertEqual(self.decisions, []); self.assertEqual(self.provider.calls, []); self.assertEqual(self.tools, [])

    async def test_private_copy_requires_explicit_projection_instead_of_automatic_current_message(self):
        self.enable()
        from persona_runtime.config import atomic_json
        atomic_json(self.paths.home / 'runtime/private-copy-policy.json', {'persona_id': self.persona['id'], 'approved_models': {}})
        policy = self.org.processing.native.policy(self.persona['id'])
        with self.assertRaisesRegex(RuntimeErrorBase, 'private_projection_required'):
            self.org.processing.native.update(self.persona['id'], True, policy['revision'], identifier(), allow_current_public_question=True)

    async def gated_native(self):
        self.enable(); entered = asyncio.Event()
        async def gate(model, messages):
            self.provider.calls.append((deepcopy(model), deepcopy(messages))); entered.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.usage = None
                error.diagnostics = {'request_headers_sent': True, 'response_headers_received': True,
                    'http_status': 200, 'native_terminal': 'unknown'}
                error.native = {'version': 'deepseek-native-web-v1', 'outcome': 'unknown',
                    'assistant': {'role': 'assistant', 'content': ''}, 'last_observed_usage': {'total_tokens': 3}}
                raise
        self.provider.complete = gate
        turn = await self.chat.submit('desktop', '公开查询', identifier(), public_task={'question': '公开查询'})
        await asyncio.wait_for(entered.wait(), 5)
        return turn

    async def test_root_cancellation_releases_owned_native_transport_and_preserves_unknown(self):
        turn = await self.gated_native()
        await self.chat.cancel(turn['id'])
        async def settled():
            while self.org.running: await asyncio.sleep(.01)
        await asyncio.wait_for(settled(), 5)
        self.assertEqual(len(self.provider.calls), 1); self.assertEqual(self.main.calls, [])
        with self.store.connection() as db:
            row = db.execute("SELECT a.id,a.status FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
            self.assertEqual(row[1], 'interrupted')
            packet = json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',
                ('knowledge.native.receipt.' + row[0],)).fetchone()[0])['data']['packet']
            self.assertEqual(packet['outcome'], 'unknown'); self.assertEqual(packet['last_observed_usage']['total_tokens'], 3)

    async def test_application_drain_does_not_wait_forever_for_native_or_redispatch(self):
        turn = await self.gated_native()
        await asyncio.wait_for(self.org.drain(), 5)
        await self.chat.wait()
        self.assertEqual(len(self.provider.calls), 1)
        with self.store.connection() as db:
            row = db.execute("SELECT status FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.stage')='conversation_native'").fetchone()
            self.assertEqual(row[0], 'blocked_unknown')


class ReadableBudget(unittest.TestCase):
    def test_native_details_use_available_budget_without_overwriting_full_notes(self):
        from persona_runtime.knowledge_translation import context
        notes = '步骤细节。' * 2500
        result = {'organized': {'notes': notes, 'source_ids': []}, 'sources': [],
            'retrieval': {'executor': 'deepseek-native-web-v1'}}
        body, _, meta = context(result, 18000)
        self.assertGreater(meta['notes_characters'], 8000)
        self.assertLessEqual(len(body), 18000)
        self.assertEqual(result['organized']['notes'], notes)

    def test_followup_can_find_a_late_passage_in_retained_original_without_another_model(self):
        from persona_runtime.knowledge_translation import context
        original='与问题无关的前言。'*2000+'\n黄豆浸泡时间：需要按豆子状态调整，目标段落必须保留。\n'+'末尾内容。'*1000
        result={'organized':{},'sources':[{'id':'s','title':'公开食谱','url':'https://example.org/','kind':'page_content','content':original}],
            'source_query':'黄豆浸泡时间怎么安排','retrieval':{'executor':'deepseek-native-web-v1'}}
        body,_,_=context(result,6500)
        self.assertIn('目标段落必须保留',body);self.assertLessEqual(len(body),6500)
        self.assertEqual(result['sources'][0]['content'],original)


if __name__ == '__main__':
    unittest.main()
