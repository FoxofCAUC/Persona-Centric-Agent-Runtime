"""Isolated integration: mock primary model, local-only optional semantics."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch, Mock

from tests import test_m72 as prior
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_embedding import SemanticFailure, digest
from persona_runtime.character_policy import choose, read
from persona_runtime.character_retrieval import assemble
from persona_runtime.character_runtime import PreparationCoordinator, audit
from persona_runtime.models import identifier, RuntimeErrorBase


def lore(item='book',text='人工书店有蓝色阅读角',keywords=None):
    return {'id':item,'title':'人工书店','category':'place','keywords':keywords or ['书店'],
            'text':text,'priority':5,'enabled':True,'visibility':'conversation','dependencies':[]}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    turn=prior.ContextTests.turn
    raw=prior.ContextTests.raw
    policy=prior.ContextTests.policy

    def setUp(self):
        prior.ContextTests.setUp(self)
        self.rt=self.engine.cognitive.characters
        self.addCleanup(self.rt.close)
        self.characters=CharacterService(self.store)
        self.data=empty_character();self.data['lorebook']=[lore()]
        self.version=self.save(self.data)
        # Deterministic tests never load the real model or start preparers.
        self.notifier=patch.object(self.rt,'notify',return_value=[]);self.notifier.start()
        self.addCleanup(self.notifier.stop)
        self.store.character_committed=self.rt.notify

    def save(self,data):
        return self.characters.update(self.pid,data,self.characters.current(self.pid)['id'],identifier())

    def hybrid(self,version,policy,members,scenario,limit):
        result=assemble(version,members,{'lore':[{'item_id':'book'}],'example':[]},limit)
        return result|{'requested_mode':'hybrid','effective_mode':'hybrid','degrade_reason':None,
            'model_fingerprint':policy['config']['model_fingerprint'],'index_fingerprint':'fake-index',
            'candidates':{'lore':[{'item_id':'book','match_types':['semantic'],'semantic_member_index':len(members)-1,
                'lexical_hits':[{'member_index':0,'term':'不能进清单的命中词'}]}]},'rejected':[]}

    async def test_default_and_omitted_policy_preserved_and_idempotent(self):
        self.assertEqual(self.ingress.current_policy(self.pid,'desktop')['character_retrieval'],{'mode':'lexical'})
        old=self.ingress.current_policy(self.pid,'desktop');op=identifier()
        args=(self.pid,'desktop',old['mode'],old['id'],op)
        a=self.ingress.update_policy(*args,character_retrieval_mode='hybrid')
        self.assertEqual(a,self.ingress.update_policy(*args,character_retrieval_mode='hybrid'))
        self.assertEqual(a['character_retrieval'],self.policy(cross_session=True)['character_retrieval'])
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.ingress.update_policy(*args,character_retrieval_mode='lexical')
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):
            self.ingress.update_policy(*args[:-1],identifier(),character_retrieval_mode='lexical')

    async def test_queued_snapshot_and_registered_profile_change_do_not_rebind(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('阅读的地方')
        data=deepcopy(self.data);data['lorebook'][0]['text']='编辑后的新版本';self.save(data)
        self.policy(character_retrieval_mode='lexical')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid) as query,patch('persona_runtime.character_policy.load_config',side_effect=AssertionError('not current')):
            p=self.engine.cognitive.prepare(t['id'])
        self.assertEqual(query.call_args.args[0]['id'],self.version['id'])
        self.assertIn('蓝色阅读角',p['messages'][0]['content']);self.assertNotIn('编辑后的新版本',p['messages'][0]['content'])
        self.assertEqual(p['manifest']['character_retrieval']['effective_mode'],'hybrid')

    async def test_batch_first_member_freezes_policy_and_switch_requires_resolution(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),character_retrieval_mode='hybrid')
        with patch.object(self.engine,'_schedule'):
            r=self.ingress.accept({'text':'第一条','request_id':identifier(),'channel':'desktop'})
            with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):self.policy(character_retrieval_mode='lexical')
            self.policy(character_retrieval_mode='lexical',pending_action='seal')
        turn=self.raw(self.ingress.get(r['batch_id'])['turn_id'])
        self.assertEqual(read(json.loads(turn['context_policy_snapshot']))['mode'],'hybrid')
        self.assertEqual(self.ingress.current_policy(self.pid,'desktop')['character_retrieval']['mode'],'lexical')

    async def test_bad_snapshot_zero_call_and_old_missing_snapshot_no_current_fill(self):
        for mutation in ('foreign','missing','content','policy','empty'):
            t=await self.turn('书店 '+mutation);row=self.raw(t['id']);snap=json.loads(row['character_snapshot'])
            if mutation=='foreign':snap['persona_id']='someone-else'
            elif mutation=='missing':snap['id']='missing'
            elif mutation=='content':snap['data']['appearance']['height_cm']=199
            with self.store.connection(write=True) as db:
                if mutation=='policy':
                    pol=json.loads(row['context_policy_snapshot']);pol['character_retrieval']={'mode':'hybrid'}
                    db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(pol),t['id']))
                else:db.execute('UPDATE turns SET character_snapshot=? WHERE id=?',('' if mutation=='empty' else json.dumps(snap),t['id']))
            await self.engine._run(t['id'])
            self.assertEqual(self.engine.get(t['id'])['attempt_count'],0)
            self.assertIn(self.engine.get(t['id'])['error_code'],('character_snapshot_invalid','invalid_character_policy_snapshot'))
        self.assertFalse(self.model.calls)
        t=await self.turn('书店')
        with self.store.connection(write=True) as db:db.execute('UPDATE turns SET character_snapshot=NULL WHERE id=?',(t['id'],))
        self.assertIsNone(self.engine.cognitive.prepare(t['id'])['manifest']['character_retrieval'])

    async def test_old_policy_without_character_field_stays_lexical(self):
        t=await self.turn('书店');row=self.raw(t['id']);p=json.loads(row['context_policy_snapshot']);p.pop('character_retrieval')
        with self.store.connection(write=True) as db:db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(p),t['id']))
        with patch.object(self.rt,'_hybrid',side_effect=AssertionError('legacy must not embed')):
            p=self.engine.cognitive.prepare(t['id'])
        self.assertEqual(p['manifest']['character_retrieval']['effective_mode'],'lexical')

    async def test_fallbacks_one_primary_call_and_no_hidden_prepare(self):
        self.policy(character_retrieval_mode='hybrid')
        for code in ('embedding_busy','embedding_unavailable','character_cache_missing','character_cache_invalid'):
            t=await self.turn('书店 '+code)
            with patch.object(self.rt,'_hybrid',side_effect=SemanticFailure(code)),patch.object(self.rt,'enqueue',side_effect=AssertionError('no build')):
                await self.engine._run(t['id'])
            self.assertEqual(self.engine.get(t['id'])['status'],'completed')
            with self.store.connection() as db:
                manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(t['id'],)).fetchone()[0])
            self.assertEqual(manifest['character_retrieval']['degrade_reason'],code)
            self.assertEqual(manifest['input_snapshot_hash'],digest(self.model.calls[-1]))
        self.assertEqual(len(self.model.calls),4)

    async def test_timeout_late_result_does_not_change_prompt_or_add_call(self):
        self.policy(character_retrieval_mode='hybrid');self.rt.wait_seconds=.02
        gate=threading.Event();done=threading.Event();self.addCleanup(gate.set)
        def late(*args):
            gate.wait(2)
            try:return self.hybrid(*args)
            finally:done.set()
        t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=late):
            await self.engine._run(t['id'])
            before=deepcopy(self.model.calls);self.assertEqual(len(before),1)
            gate.set();done.wait(2)
        self.assertEqual(self.model.calls,before)
        with self.store.connection() as db:m=json.loads(db.execute('SELECT manifest_json FROM context_manifests').fetchone()[0])
        self.assertEqual(m['character_retrieval']['degrade_reason'],'character_semantic_timeout')

    async def test_final_recompose_no_embedding_and_semantic_integrity_falls_back(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('阅读的地方')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        p['guard']='force-history-recheck'
        with patch.object(self.rt,'_hybrid',side_effect=AssertionError('write must not embed')):
            with self.store.connection(write=True) as db:q=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertEqual(q['manifest']['character_retrieval']['effective_mode'],'hybrid')
        q['character_prepared']['parts'][0]['text']='tampered optional prepared text'
        with patch.object(self.rt,'_hybrid',side_effect=AssertionError('write must not embed')):
            with self.store.connection(write=True) as db:r=self.engine.cognitive.finalize(db,self.raw(t['id']),q)
        self.assertNotIn('tampered',str(r['messages']))
        self.assertEqual(r['manifest']['character_retrieval']['degrade_reason'],'character_preparation_invalidated')

    async def test_authority_recheck_failure_cannot_hide_as_fallback(self):
        t=await self.turn('书店');p=self.engine.cognitive.prepare(t['id'])
        row=self.raw(t['id']);snap=json.loads(row['character_snapshot']);snap['data']['appearance']['height_cm']=199
        row['character_snapshot']=json.dumps(snap)
        with self.store.connection(write=True) as db,self.assertRaisesRegex(RuntimeErrorBase,'character_snapshot_invalid'):
            self.engine.cognitive.finalize(db,row,p)

    async def test_manifest_no_raw_terms_and_final_budget_sources_match(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        meta=p['manifest']['character_retrieval']
        self.assertEqual(meta['selected']['lore'],['book'])
        self.assertNotIn('不能进清单的命中词',json.dumps(meta,ensure_ascii=False))
        self.assertNotIn('人工书店有蓝色阅读角',json.dumps(meta,ensure_ascii=False))
        selection=p['character_prepared'];removed=[{'kind':'lore_group','id':'selected-lore','reason':'input_budget'}]
        final=audit(selection,[],removed)
        self.assertEqual(final['selected']['lore'],[]);self.assertEqual(final['dependency_groups'],[])

    async def test_global_budget_removes_hybrid_group_from_prompt_and_both_audits(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('阅读的地方')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):
            p=self.engine.cognitive.prepare(t['id'])
            limit=sum(len(m['content']) for m in p['messages'])-p['manifest']['character_retrieval']['budgets']['total']
            row=self.raw(t['id']);model=json.loads(row['model_snapshot']);model['max_input_chars']=limit
            with self.store.connection(write=True) as db:db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),t['id']))
            with self.store.connection(write=True) as db:q=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertNotIn('人工书店有蓝色阅读角',str(q['messages']))
        self.assertEqual(q['manifest']['character_retrieval']['selected']['lore'],[])
        self.assertEqual(q['details']['character']['lore_ids'],[])
        self.assertEqual(q['manifest']['character_retrieval']['dependency_groups'],[])

    async def test_nonfirst_member_reaches_semantic_branch(self):
        self.policy(character_retrieval_mode='hybrid')
        p=self.ingress.current_policy(self.pid,'desktop');self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        with patch.object(self.engine,'_schedule'):
            r=self.ingress.accept({'text':'第一条','request_id':identifier(),'channel':'desktop'})
            self.ingress.accept({'text':'第二条阅读场所','request_id':identifier(),'channel':'desktop'})
            self.policy(character_retrieval_mode='lexical',pending_action='seal')
        tid=self.ingress.get(r['batch_id'])['turn_id']
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid) as query:p=self.engine.cognitive.prepare(tid)
        self.assertEqual(query.call_args.args[2],['第一条','第二条阅读场所'])
        self.assertEqual(p['manifest']['character_retrieval']['candidates']['lore'][0]['semantic_member_index'],1)

    async def test_retry_uses_current_policy_preserving_old_manifest(self):
        t=await self.turn('书店');await self.engine._run(t['id'])
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET status='failed' WHERE id=?",(t['id'],))
        self.policy(character_retrieval_mode='hybrid')
        with patch.object(self.engine,'_schedule'):await self.engine.retry(t['id'])
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):await self.engine._run(t['id'])
        with self.store.connection() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid',(t['id'],))]
        self.assertEqual([r['character_retrieval']['requested_mode'] for r in rows],['lexical','hybrid'])
        self.assertEqual(len(self.model.calls),2)

    async def test_manifest_disk_failure_zero_call(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('书店')
        with self.store.connection(write=True) as db:db.execute("CREATE TRIGGER reject_manifest BEFORE INSERT ON context_manifests BEGIN SELECT RAISE(ABORT,'fault'); END")
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['attempt_count'],0);self.assertFalse(self.model.calls)

    async def test_preview_read_only_and_legacy_compatibility(self):
        from persona_runtime.character_context import select_context
        self.assertEqual(self.rt.preview(self.pid,'书店',self.version['id']),select_context(self.version,'书店'))
        with self.store.connection() as db:before=list(db.iterdump())
        with patch.object(self.rt,'enqueue',side_effect=AssertionError('preview cannot prepare')),patch.object(self.rt,'_hybrid',side_effect=self.hybrid):
            result=self.rt.preview(self.pid,members=['阅读的地方'],channel='desktop',mode='hybrid')
        with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))
        self.assertEqual(result['effective_mode'],'hybrid');self.assertFalse(self.model.calls)

    async def test_preview_contextual_example_limit_follows_entry_not_retrieval_mode(self):
        self.policy(reply_mode='context_full')
        with patch.object(self.rt,'selection',return_value={}) as select:
            self.rt.preview(self.pid,members=['你好'],channel='desktop',mode='lexical')
        self.assertEqual(select.call_args.args[-1],4)
        self.policy(reply_mode='legacy_full')
        with patch.object(self.rt,'selection',return_value={}) as select:
            self.rt.preview(self.pid,members=['你好'],channel='desktop',mode='hybrid')
        self.assertEqual(select.call_args.args[-1],2)

    async def test_post_commit_edit_restore_and_import_notify(self):
        def committed(pid):
            # Can take another writer immediately: callback is outside write transaction.
            with self.store.connection(write=True) as db:db.execute('SELECT 1')
        self.store.character_committed=Mock(side_effect=committed)
        new=self.save(self.data)
        self.characters.restore(self.pid,self.version['id'],new['id'],identifier())
        self.characters.import_package(self.pid,{'format':'pcar_character_v1','persona_spec':self.service.show_persona(self.pid)['spec'],'character':self.data},identifier())
        self.assertEqual(self.store.character_committed.call_count,3)

    async def test_discovery_current_and_pending_old_versions_and_no_default_import(self):
        self.policy(character_retrieval_mode='hybrid');t=await self.turn('书店')
        data=deepcopy(self.data);data['interests']['notes']='新版';new=self.save(data)
        with patch.object(self.rt,'enqueue',return_value={'state':'queued'}) as enqueue:
            self.rt.discover(self.pid)
        versions={call.args[1] for call in enqueue.call_args_list}
        self.assertEqual(versions,{self.version['id'],new['id']})
        other=self.service.create_persona(prior.generic_persona())['id']
        with patch.object(self.rt,'enqueue') as enqueue:self.rt.discover(other)
        enqueue.assert_not_called()

    async def test_admin_api_and_bridge_permissions_and_health_during_preparation(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        app=create_app(self.paths,self.model);runtime=app['engine'].cognitive.characters
        entered=threading.Event();release=threading.Event()
        def build(*args):entered.set();release.wait(3)
        with patch.object(runtime.coordinator,'build',side_effect=build):
            async with TestServer(app) as server,ClientSession() as client:
                headers={'Authorization':'Bearer '+self.config.token()};bridge={'Authorization':'Bearer '+self.config.token('bridge')}
                url=str(server.make_url('/v1/manage'))
                async def request(action,**args):
                    async with client.post(url,headers=headers,json={'action':action,'args':args}) as r:
                        value=await r.json();self.assertEqual(r.status,200,value);return value
                try:
                    for action in ('character.preview','character.index-status','character.index-prepare','interaction.update'):
                        async with client.post(url,headers=bridge,json={'action':action,'args':{'persona_id':self.pid}}) as r:self.assertEqual(r.status,403)
                    p=await request('interaction.current',persona_id=self.pid,channel='desktop')
                    await request('interaction.update',persona_id=self.pid,channel='desktop',mode=p['mode'],expected_version=p['id'],operation_id=identifier(),character_retrieval_mode='hybrid')
                    self.assertTrue(await asyncio.to_thread(entered.wait,1))
                    async with client.get(str(server.make_url('/v1/health')),headers=headers,timeout=1) as r:self.assertEqual(r.status,200)
                    first=await request('character.index-prepare',persona_id=self.pid,version_id=self.version['id'])
                    second=await request('character.index-prepare',persona_id=self.pid,version_id=self.version['id'])
                    self.assertEqual(first['task_key'],second['task_key'])
                    with self.store.connection() as db:before=list(db.iterdump())
                    v=await request('character.preview',persona_id=self.pid,text='书店',mode='hybrid',channel='desktop')
                    self.assertEqual(v['effective_mode'],'lexical')
                    with self.store.connection() as db:self.assertEqual(before,list(db.iterdump()))
                    # A collector sealed by an HTTP policy switch schedules on the event loop.
                    p=await request('interaction.current',persona_id=self.pid,channel='desktop')
                    await request('interaction.update',persona_id=self.pid,channel='desktop',mode='natural_batch',expected_version=p['id'],operation_id=identifier())
                    async with client.post(str(server.make_url('/v2/ingress')),headers=headers,json={'text':'书店','request_id':identifier(),'channel':'desktop'}) as r:receipt=await r.json();self.assertEqual(r.status,200,receipt)
                    p=await request('interaction.current',persona_id=self.pid,channel='desktop')
                    await request('interaction.update',persona_id=self.pid,channel='desktop',mode=p['mode'],expected_version=p['id'],operation_id=identifier(),character_retrieval_mode='lexical',pending_action='seal')
                    await app['engine'].wait()
                    self.assertEqual(len(self.model.calls),1)
                    async with client.get(str(server.make_url('/v2/delivery/streams')),headers=headers,timeout=1) as r:self.assertEqual(r.status,200)
                finally:release.set()


class CoordinatorTests(unittest.TestCase):
    def test_ready_explicit_prepare_can_rebuild_and_diagnostics_are_bounded(self):
        calls=[];c=PreparationCoordinator(lambda:calls.append(1));self.addCleanup(c.close)
        c.submit('x',())
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:c.status('x')['state']=='ready',2))
        self.assertEqual(c.submit('x',())['state'],'ready');self.assertEqual(len(calls),1)
        c.submit('x',(),retry=True)
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:len(calls)==2 and c.status('x')['state']=='ready',2))

    def test_bounded_deduplicated_shutdown_and_no_automatic_retry(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def build(*args):calls.append(args);entered.set();release.wait(2)
        c=PreparationCoordinator(build);self.addCleanup(c.close);self.addCleanup(release.set)
        c.submit('running',(1,));self.assertTrue(entered.wait(1))
        self.assertEqual(c.submit('running',(1,))['state'],'running')
        for i in range(32):self.assertEqual(c.submit(str(i),(i,))['state'],'queued')
        self.assertEqual(c.submit('overflow',(99,))['state'],'queue_full')
        self.assertEqual(c.status('overflow')['state'],'queue_full')
        release.set();c.close();self.assertEqual(c.submit('after',())['state'],'stopped')
        self.assertLessEqual(len(calls),33)

    def test_failure_recorded_retry_only_explicit(self):
        count=[]
        def fail():count.append(1);raise SemanticFailure('embedding_unavailable')
        c=PreparationCoordinator(fail);self.addCleanup(c.close)
        c.submit('x',())
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:c.states['x']['state']=='failed',2))
        self.assertEqual(c.submit('x',())['reason'],'embedding_unavailable');self.assertEqual(len(count),1)
        c.submit('x',(),retry=True)
        with c.condition:self.assertTrue(c.condition.wait_for(lambda:len(count)==2 and c.states['x']['state']=='failed',2))


if __name__=='__main__':unittest.main()
