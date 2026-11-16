"""表达资产的来源、角色、撤回、版本、候选和学习状态；无收费调用。"""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import scope
from persona_runtime.expression_assets import ExpressionAssets, ExpressionAssetError
from persona_runtime.expression_candidates import ExpressionCandidates
from persona_runtime.expression_learning import ExpressionLearning, filter_candidates
from persona_runtime.models import generic_persona, identifier, LOCAL_USER_ID, timestamp, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests import test_m4 as fixture
from tests import test_expression_e1 as e1_transport_fixture


MODEL = {'base_url': 'https://api.deepseek.com', 'name': 'deepseek-flash', 'encrypted_key': ''}


class AssetFixture:
    def setup_assets(self):
        self.assets = ExpressionAssets(self.store)
        self.pid = self.persona['id']
        self.selected = scope(self.pid, 'desktop')

    def source(self, body='对方累了，轻轻接住眼前的疲惫。', permissions=('use', 'learn')):
        return self.assets.register_source(self.pid, self.selected, kind='authored_examples',
                    parts=[{'kind': 'text', 'text': body}], permissions=permissions,
                    attribution='synthetic public fixture', operation_id=identifier())

    def put(self, source=None, situation='对方很累想休息', style='轻轻接住眼前的疲惫', **extra):
        original = source or self.source()
        return self.assets.put(self.pid, self.selected, situation=situation, style=style,
                    keywords=['累', '休息'], source_refs=[{'source_id': original['id'], 'revision': original['revision']}],
                    operation_id=identifier(), **extra)

    def enable(self, **extra):
        current = self.assets.policy(self.pid)
        return self.assets.configure(self.pid, {'enabled': True, 'learning_enabled': True, **extra}, current['revision'], identifier())


class Assets(AssetFixture, unittest.TestCase):
    def setUp(self):
        temporary_root=RuntimePaths.from_environment().home/'tmp'
        temporary_root.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='pcar-expression-assets-',dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.store = Store(self.paths);self.store.initialize()
        self.service = RuntimeService(self.store)
        self.persona = self.service.create_persona(generic_persona())
        self.setup_assets()

    def test_defaults_off_and_zero_cloud_side_effects(self):
        self.assertFalse(self.assets.policy(self.pid)['data']['enabled'])
        self.put()
        selected = ExpressionCandidates(self.store).select(self.pid, self.selected, '累了休息')
        self.assertEqual(selected['items'], [])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_learning_jobs').fetchone()[0], 0)

    def test_default_mutation_does_not_leak_to_another_persona(self):
        first = self.assets.policy(self.pid)
        first['data']['learning_sessions'].append('not-a-session')
        self.assertEqual(self.assets.policy(self.pid)['data']['learning_sessions'], [])

    def test_source_and_asset_are_idempotent_by_operation_id(self):
        source = self.source()
        args = dict(situation='累了休息', style='说一句轻轻的回应', source_refs=[{'source_id': source['id'], 'revision': 1}], operation_id=identifier())
        one = self.assets.put(self.pid, self.selected, **args)
        two = self.assets.put(self.pid, self.selected, **args)
        self.assertEqual(one, two)
        with self.assertRaisesRegex(ExpressionAssetError, 'operation_conflict'):
            self.assets.put(self.pid, self.selected, **(args | {'style': '另一个说法'}))

    def test_cross_role_and_cross_peer_never_share_assets(self):
        self.enable();item = self.put()
        other = self.service.create_persona(generic_persona())['id']
        with self.assertRaises(ExpressionAssetError):self.assets.show(other, scope(other, 'desktop'), item['id'])
        with self.assertRaises(ExpressionAssetError):self.assets.show(self.pid, scope(self.pid, 'qq', 'bot', 'peer'), item['id'])
        wrong = dict(self.selected, persona_id=other)
        with self.assertRaises(ExpressionAssetError):self.assets.show(self.pid, wrong, item['id'])

    def test_only_top_level_text_and_approved_source_kinds(self):
        for kind in ('main_draft', 'assistant_reply', 'expression_artifact', 'maintenance_summary', 'test_answer'):
            with self.assertRaises(ExpressionAssetError):
                self.assets.register_source(self.pid, self.selected, kind=kind, parts=[{'kind': 'text', 'text': '材料'}], attribution='fixture', operation_id=identifier())
        for parts in ([{'kind': 'quote', 'text': '转发'}], [{'kind': 'image', 'text': '识图'}]):
            with self.assertRaises(ExpressionAssetError):
                self.assets.register_source(self.pid, self.selected, kind='public_human', parts=parts, attribution='fixture', operation_id=identifier())

    def test_style_prefix_and_prompt_example_guard_apply_at_ingest(self):
        source = self.source()
        item = self.put(source, style='使用轻轻的反问接话')
        self.assertEqual(self.assets.show(self.pid, self.selected, item['id'])['style'], '轻轻的反问接话')
        with self.assertRaisesRegex(ExpressionAssetError, 'prompt_example'):
            self.put(source, style='使用对对对')

    def test_source_permission_is_checked_even_when_role_enabled(self):
        self.enable();original = self.source(permissions=('learn',))
        with self.assertRaisesRegex(ExpressionAssetError, 'permission_denied'):self.put(original)

    def test_source_update_or_revocation_invalidates_snapshot(self):
        self.enable();original = self.source();self.put(original)
        engine = ExpressionCandidates(self.store);snapshot = engine.select(self.pid, self.selected, '累了休息')
        self.assertEqual(len(engine.validate(self.pid, self.selected, snapshot['id'])), 1)
        self.assets.revise_source(self.pid, self.selected, original['id'], expected_revision=1, permissions=['use','learn'], operation_id=identifier(), revoke=True)
        with self.assertRaises(ExpressionAssetError):engine.validate(self.pid, self.selected, snapshot['id'])
        self.assertEqual(engine.select(self.pid, self.selected, '累了休息')['items'], [])

    def test_asset_edit_and_restore_are_append_only(self):
        self.enable();original = self.source();one = self.put(original)
        two = self.put(original, style='顺着当下慢下来', asset_id=one['id'], expected_revision=1)
        restored = self.assets.restore(self.pid, self.selected, one['id'], 1, 2, identifier())
        self.assertEqual(restored['revision'], 3)
        self.assertEqual(self.assets.show(self.pid, self.selected, one['id'])['style'], '轻轻接住眼前的疲惫')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_asset_versions WHERE asset_id=?', (one['id'],)).fetchone()[0], 3)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as db:db.execute('UPDATE expression_asset_versions SET style=\'改写历史\' WHERE asset_id=?', (one['id'],))

    def test_restore_cannot_reactivate_revoked_material(self):
        original = self.source();one = self.put(original)
        self.assets.revise_source(self.pid, self.selected, original['id'], expected_revision=1, permissions=['use'], revoke=True, operation_id=identifier())
        with self.assertRaises(ExpressionAssetError):self.assets.restore(self.pid, self.selected, one['id'], 1, 1, identifier())

    def test_edit_cannot_silently_reenable_disabled_asset(self):
        original=self.source();one=self.put(original)
        self.assets.disable(self.pid,self.selected,one['id'],1,identifier())
        with self.assertRaisesRegex(ExpressionAssetError,'requires_restore'):
            self.put(original,style='另一个说法',asset_id=one['id'],expected_revision=1)

    def test_disable_invalid_asset_is_allowed_and_invalidates_old_selection(self):
        self.enable();original = self.source();item = self.put(original)
        engine = ExpressionCandidates(self.store);snapshot = engine.select(self.pid, self.selected, '休息')
        self.assets.revise_source(self.pid, self.selected, original['id'], expected_revision=1, permissions=['use'], revoke=True, operation_id=identifier())
        self.assets.disable(self.pid, self.selected, item['id'], 1, identifier())
        with self.assertRaises(ExpressionAssetError):engine.validate(self.pid, self.selected, snapshot['id'])

    def test_no_match_is_empty_and_candidate_budget_never_truncates_style(self):
        self.enable(max_context_chars=1);self.put()
        engine = ExpressionCandidates(self.store)
        self.assertEqual(engine.select(self.pid, self.selected, '天文望远镜')['items'], [])
        self.assertEqual(engine.select(self.pid, self.selected, '休息')['items'], [])

    def test_generic_subject_cannot_activate_an_unrelated_style_and_explicit_single_char_keyword_works(self):
        self.enable();self.put()
        engine=ExpressionCandidates(self.store)
        self.assertEqual(engine.select(self.pid,self.selected,'对方拒绝解释时回应')['items'],[])
        self.assertEqual(engine.select(self.pid,self.selected,'对方')['items'],[])
        self.assertTrue(engine.select(self.pid,self.selected,'累了')['items'])

    def test_canonical_candidate_can_fill_E0_scoped_contract(self):
        self.enable();self.put()
        engine = ExpressionCandidates(self.store);snapshot = engine.select(self.pid, self.selected, '休息')
        values = engine.scoped_texts(self.pid, self.selected, snapshot['id'], snapshot['data_hash'])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].persona_id, self.pid)
        with self.assertRaises(ExpressionAssetError):engine.validate(self.pid, self.selected, snapshot['id'], 'wrong-hash')

    def test_local_embedding_failure_falls_back_without_cloud(self):
        self.enable();self.put()
        class Broken:
            def identity(self):raise ValueError('unavailable')
        result = ExpressionCandidates(self.store, Broken()).select(self.pid, self.selected, '休息')
        self.assertTrue(result['items'])
        self.assertEqual(result['embedding_fallback'], 'local_embedding_unavailable')

    def test_vectors_invalidated_and_rechecked_during_embedding(self):
        self.enable();original = self.source();self.put(original)
        parent = self
        class Provider:
            def identity(self):return {'fingerprint': 'fixture-model', 'dimension': 2}
            def embed_documents(self, values):
                parent.assets.revise_source(parent.pid, parent.selected, original['id'], expected_revision=1, permissions=['use'], revoke=True, operation_id=identifier())
                return [[1,0] for _ in values]
            def embed_query(self, values):return [], [[1,0]]
        with self.assertRaises(ExpressionAssetError):ExpressionCandidates(self.store, Provider()).select(self.pid, self.selected, '休息')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM expression_asset_vectors').fetchone()[0], 0)


class LearningClient:
    def __init__(self, unknown=False, after_receipt=False):
        self.calls = 0;self.unknown = unknown;self.after_receipt = after_receipt
        self.started = asyncio.Event();self.release = None

    async def complete(self, model, messages, *, save_raw):
        self.calls += 1;self.started.set()
        if self.release:await self.release.wait()
        if self.unknown:
            from persona_runtime.expression_transport import ExpressionCallFailure
            raise ExpressionCallFailure('provider_transport_interrupted', {'request_headers_sent': True, 'response_headers_received': False})
        content = json.dumps({'candidates': [{'title':'对方累了想休息','description':'使用轻轻接住当下疲惫的说法','source_id':1}]}, ensure_ascii=False)
        raw = json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}],
                          'usage':{'prompt_tokens':100,'completion_tokens':50}}, ensure_ascii=False).encode('utf-8')
        save_raw(raw)
        if self.after_receipt:raise RuntimeError('local post-receipt interruption')
        return {'diagnostics':{'request_headers_sent':True,'response_headers_received':True}}


class Learning(AssetFixture, unittest.IsolatedAsyncioTestCase):
    setUp = fixture.M4Tests.setUp
    asyncTearDown = fixture.M4Tests.asyncTearDown

    def setup_learning(self, **extra):
        self.setup_assets();self.enable(**extra)
        self.original = self.source();self.client = LearningClient();self.learning = ExpressionLearning(self.store, self.client)
        self.job = self.learning.prepare(self.pid, self.selected, [self.original['id']], MODEL, identifier())

    async def test_learning_is_proposal_only_and_publishing_is_idempotent(self):
        self.setup_learning()
        result = await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.assets.status(self.pid)['assets'], 0)
        proposal = result['result']['proposals'][0];op = identifier()
        one = self.learning.publish(self.pid, self.selected, proposal, op)
        two = self.learning.publish(self.pid, self.selected, proposal, op)
        self.assertEqual(one, two)
        await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(self.client.calls, 1)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM expression_asset_observations').fetchone()[0], 1)

    async def test_unknown_is_not_replayed_and_usage_stays_unknown(self):
        self.setup_learning();self.client.unknown = True
        first = await self.learning.run(self.job['id'], MODEL)
        second = await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(first['state'], 'unknown');self.assertFalse(second['new_call'])
        self.assertEqual(self.client.calls, 1)
        self.assertIsNone(self.learning.get(self.job['id'])['usage_json'])

    async def test_cancel_records_unknown_and_propagates_shutdown_without_replay(self):
        self.setup_learning();self.client.release=asyncio.Event()
        task=asyncio.create_task(self.learning.run(self.job['id'],MODEL));await self.client.started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.learning.get(self.job['id'])['state'],'unknown')
        await self.learning.run(self.job['id'],MODEL)
        self.assertEqual(self.client.calls,1)

    async def test_received_body_recovers_after_local_failure(self):
        self.setup_learning();self.client.after_receipt = True
        result = await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['result']['proposals'])
        await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(self.client.calls, 1)

    async def test_withdrawal_before_dispatch_uses_zero_calls(self):
        self.setup_learning()
        self.assets.revise_source(self.pid, self.selected, self.original['id'], expected_revision=1, permissions=['use'], revoke=True, operation_id=identifier())
        result = await self.learning.run(self.job['id'], MODEL)
        self.assertEqual(result['state'], 'stale');self.assertEqual(self.client.calls, 0)

    async def test_withdrawal_in_flight_keeps_paid_receipt_without_proposals(self):
        self.setup_learning();self.client.release = asyncio.Event()
        task = asyncio.create_task(self.learning.run(self.job['id'], MODEL))
        await asyncio.wait_for(self.client.started.wait(), 10)
        self.assets.revise_source(self.pid, self.selected, self.original['id'], expected_revision=1, permissions=['use'], revoke=True, operation_id=identifier())
        self.client.release.set();result = await task
        self.assertEqual(result['state'], 'stale');self.assertEqual(result['result']['proposals'], [])
        self.assertEqual(json.loads(self.learning.get(self.job['id'])['usage_json'])['completion_tokens'], 50)

    async def test_budget_reservation_blocks_second_job_before_call(self):
        self.setup_learning()
        await self.learning.run(self.job['id'], MODEL)
        next_job = self.learning.prepare(self.pid, self.selected, [self.original['id']], MODEL, identifier())
        with self.assertRaisesRegex(ExpressionAssetError, 'budget_reached'):await self.learning.run(next_job['id'], MODEL)
        self.assertEqual(self.client.calls, 1)

    async def test_concurrent_owners_send_only_once(self):
        self.setup_learning();self.client.release = asyncio.Event()
        first = asyncio.create_task(self.learning.run(self.job['id'], MODEL));await self.client.started.wait()
        second = await self.learning.run(self.job['id'], MODEL);self.client.release.set();await first
        self.assertEqual(second['state'], 'dispatched');self.assertEqual(self.client.calls, 1)

    async def test_raw_receipt_is_append_only(self):
        self.setup_learning();await self.learning.run(self.job['id'], MODEL)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection(write=True) as db:db.execute('UPDATE expression_learning_jobs SET raw_response=? WHERE id=?', (b'{}', self.job['id']))

    async def test_private_purpose_guard_runs_before_dispatch(self):
        self.setup_learning()
        with patch('persona_runtime.private_personas.guard_cloud_dispatch',side_effect=RuntimeErrorBase('private_cloud_provider_not_authorized')) as guard:
            with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):await self.learning.run(self.job['id'],MODEL)
        self.assertEqual(guard.call_args.args[2],'expression_learning')
        self.assertEqual(self.client.calls,0)
        self.assertIsNone(self.learning.get(self.job['id'])['attempt_id'])

    async def test_repeated_learning_counts_observation_once_per_job_not_per_bubble(self):
        self.setup_learning(learning_hourly_limit=3)
        first=await self.learning.run(self.job['id'],MODEL)
        one=self.learning.publish(self.pid,self.selected,first['result']['proposals'][0],identifier())
        next_job=self.learning.prepare(self.pid,self.selected,[self.original['id']],MODEL,identifier())
        second=await self.learning.run(next_job['id'],MODEL)
        two=self.learning.publish(self.pid,self.selected,second['result']['proposals'][0],identifier())
        self.assertEqual(one['id'],two['id'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_asset_observations').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],0)

    async def test_unknown_and_unsent_are_different_usage_classes(self):
        self.setup_learning()
        class Unsent:
            async def complete(self,model,messages,*,save_raw):
                from persona_runtime.expression_transport import ExpressionCallFailure
                raise ExpressionCallFailure('provider_transport_interrupted',{'request_headers_sent':False,'response_headers_received':False})
        self.learning.client=Unsent();result=await self.learning.run(self.job['id'],MODEL)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['result']['usage_class'],'not_applicable')
        self.assertIsNone(self.learning.get(self.job['id'])['usage_json'])

    async def test_authorized_session_collection_is_incremental_without_self_learning(self):
        self.setup_assets()
        turn = await self.chat.submit('desktop','累了，先缓一口气。',identifier());await self.chat.wait();current = self.chat.get(turn['id'])
        sid = current['session_id'];self.enable(learning_sessions=[sid])
        self.client=LearningClient();worker=ExpressionLearning(self.store,self.client)
        queued=worker.collect_authorized(self.pid,MODEL);self.assertTrue(queued)
        self.assertIsNone(worker.collect_authorized(self.pid,MODEL))
        with self.store.connection() as db:
            rows=db.execute('SELECT ref_json FROM expression_asset_source_versions').fetchall();self.assertEqual(len(rows),1)
            event=json.loads(rows[0][0])['event_id'];self.assertEqual(db.execute('SELECT role FROM events WHERE id=?',(event,)).fetchone()[0],'user')
        await worker.run(queued['id'],MODEL)
        self.assertEqual(self.client.calls,1)

    async def test_persona_output_and_withdrawn_user_event_cannot_be_sources(self):
        self.setup_assets();turn=await self.chat.submit('desktop','先歇一会儿。',identifier());await self.chat.wait();current=self.chat.get(turn['id'])
        for event in (current['reply_event_id'],):
            with self.assertRaises(ExpressionAssetError):
                self.assets.register_source(self.pid,self.selected,kind='user_event',event_id=event,permissions=['use','learn'],attribution='fixture',operation_id=identifier())
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(current['user_event_id'],self.pid,LOCAL_USER_ID,'fixture withdrawal',timestamp()))
        with self.assertRaises(ExpressionAssetError):
            self.assets.register_source(self.pid,self.selected,kind='user_event',event_id=current['user_event_id'],permissions=['use','learn'],attribution='fixture',operation_id=identifier())

    async def test_removing_authorized_session_withdraws_derived_asset(self):
        self.setup_assets();turn=await self.chat.submit('desktop','累了，先缓一口气。',identifier());await self.chat.wait()
        sid=self.chat.get(turn['id'])['session_id'];self.enable(learning_sessions=[sid]);worker=ExpressionLearning(self.store,LearningClient())
        job=worker.collect_authorized(self.pid,MODEL);result=await worker.run(job['id'],MODEL)
        worker.publish(self.pid,self.selected,result['result']['proposals'][0],identifier())
        candidates=ExpressionCandidates(self.store);snapshot=candidates.select(self.pid,self.selected,'累了想休息');self.assertTrue(snapshot['items'])
        configured=self.assets.policy(self.pid);self.assets.configure(self.pid,{'learning_sessions':[]},configured['revision'],identifier())
        with self.assertRaises(ExpressionAssetError):candidates.validate(self.pid,self.selected,snapshot['id'])
        self.assertEqual(candidates.select(self.pid,self.selected,'累了想休息')['items'],[])


class UpstreamFilter(unittest.TestCase):
    def test_source_id_self_blacklist_and_example_contamination(self):
        sources=[{'source_id':'one','revision':1,'body':'外部原文'}, {'source_id':'two','revision':1,'body':'自己原文','speaker':'SELF'}]
        rows=[{'title':'休息','description':'使用轻轻接话','source_id':1},
              {'title':'休息','description':'轻轻接话','source_id':1},
              {'title':'休息','description':'对对对','source_id':1},
              {'title':'休息','description':'换说法','source_id':2},
              {'title':'休息','description':'换说法','source_id':99}]
        accepted,rejected=filter_candidates(json.dumps({'candidates':rows}),sources)
        self.assertEqual(len(accepted),1);self.assertEqual(len(rejected),4)
        self.assertEqual(accepted[0]['source_refs'],[{'source_id':'one','revision':1}])


class RuntimeTransport(e1_transport_fixture.ExpressionE1TransportTests):
    async def test_one_http_call_keeps_invalid_first_content_and_usage(self):
        from persona_runtime.expression_transport import ExpressionHTTPClient
        model=MODEL|{'base_url':f'http://127.0.0.1:{self.port}','timeout':20,'max_tokens':1024,'_json_output':True}
        result=await ExpressionHTTPClient(allow_loopback_fixture=True).complete(model,[{'role':'user','content':'return json'}],save_raw=self.responses.append)
        self.assertEqual(self.count,1);self.assertEqual(result['content'],'  bad JSON  ')
        self.assertEqual(json.loads(self.responses[0])['choices'][0]['message']['content'],result['content'])

    async def test_proxy_preflight_failure_is_proven_not_dispatched(self):
        from persona_runtime.expression_transport import ExpressionHTTPClient,ExpressionCallFailure
        model=MODEL|{'timeout':20,'max_tokens':1024,'_json_output':True}
        with patch('persona_runtime.expression_transport.outbound_proxy',side_effect=RuntimeErrorBase('system_proxy_required')):
            with self.assertRaises(ExpressionCallFailure) as caught:
                await ExpressionHTTPClient().complete(model,[{'role':'user','content':'return json'}],save_raw=self.responses.append)
        self.assertIs(caught.exception.diagnostics['request_headers_sent'],False)
        self.assertIs(caught.exception.diagnostics['response_headers_received'],False)
        self.assertEqual(self.responses,[])


if __name__ == '__main__':unittest.main()
