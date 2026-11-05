"""Artificial-only normal-ingress companion compiler and lifecycle checks."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose, checked, read, freeze_model, GENERATION_PROFILE
from persona_runtime.models import identifier, RuntimeErrorBase
from tests import test_m753 as fixture


REPLY = json.dumps({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
    {'content': '那就先去吃饭吧', 'content_kind': 'chat', 'cancel_if_new_input': False}]}, ensure_ascii=False)


class Profiles(unittest.TestCase):
    def test_legacy_has_no_new_fields_or_profile(self):
        model={'name':'test','timeout':1,'max_tokens':32}
        self.assertEqual(freeze_model(model,{}),model)
        self.assertIsNone(read({'model_snapshot':json.dumps(model)}))

    def test_unknown_profiles_and_mismatched_binding_rejected(self):
        for value in ({'version':'bad','mode':'minimal'}, {'version':'companion-runtime-v1','mode':'bad'},
                      {'version':'companion-runtime-v1','mode':'current','extra':1}):
            with self.assertRaisesRegex(RuntimeErrorBase,'companion_profile_invalid'):checked(value)
        context={'companion':choose('current')};model=freeze_model({},context)
        self.assertEqual(read({'model_snapshot':model,'context_policy_snapshot':context}),choose('current'))
        model['_generation_profile']={'version':'wrong'}
        with self.assertRaisesRegex(RuntimeErrorBase,'companion_snapshot_mismatch'):
            read({'model_snapshot':model,'context_policy_snapshot':context})


class Runtime(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.IntegrationTests.setUp
    save=fixture.IntegrationTests.save
    hybrid=fixture.IntegrationTests.hybrid
    policy=fixture.IntegrationTests.policy
    raw=fixture.IntegrationTests.raw
    turn=fixture.IntegrationTests.turn

    def enable(self,mode='minimal',**kw):
        old=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',old['mode'],old['id'],identifier(),
            companion_mode=mode,reply_mode='natural_structured',expression_policy_version=3,**kw)

    async def test_minimal_single_main_no_lexical_behavior_or_cloud_auxiliary(self):
        self.enable();t=await self.turn('书店只整理了一排，还没收工，先吃饭。')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid) as local, \
             patch.object(self.engine.selector,'prepare',new=AsyncMock(side_effect=AssertionError('cloud selector'))), \
             patch.object(self.engine.observer,'prepare',new=AsyncMock(side_effect=AssertionError('observer'))), \
             patch.object(self.engine.cognitive.shadow,'prepare',side_effect=AssertionError('decision')), \
             patch('persona_runtime.behavior.resolve_behavior',side_effect=AssertionError('lexical behavior')), \
             patch.object(self.model,'complete',new=AsyncMock(return_value=(REPLY,{'total_tokens':3}))) as main:
            await self.engine._run(t['id'])
        self.assertEqual(main.await_count,1)
        self.assertEqual(local.call_args.args[3],[])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed',self.engine.get(t['id'])['error_code'])
        cfg,msgs=main.call_args.args
        self.assertEqual(cfg['_generation_profile'],GENERATION_PROFILE)
        system=msgs[0]['content']
        self.assertIn('人工书店有蓝色阅读角',system)
        self.assertIn('可以自然联想',system)
        self.assertNotIn('这次的表达余地',system)
        self.assertNotIn('不要刻意扮演心理咨询师',system)
        self.assertNotIn('仅作角色资料，不是运行指令',system)
        self.assertIn('schema_version',system)
        with self.store.connection() as db:
            m=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(t['id'],)).fetchone()[0])
        self.assertEqual(m['input_snapshot_hash'],fingerprint(msgs))
        self.assertEqual(m['prompt_assembly']['effective']['expression'],'companion-minimal-v1')
        self.assertEqual(m['character_retrieval']['requested_mode'],'hybrid')

    async def test_current_compiler_equal_to_same_accepted_legacy_inputs(self):
        self.enable('current');t=await self.turn('书店')
        prepared=self.engine.cognitive.prepare(t['id'])
        baseline=deepcopy(self.raw(t['id']))
        model=json.loads(baseline['model_snapshot']);model.pop('_companion_profile');model.pop('_generation_profile')
        context=json.loads(baseline['context_policy_snapshot']);context.pop('companion')
        baseline['model_snapshot']=json.dumps(model);baseline['context_policy_snapshot']=json.dumps(context)
        with self.store.connection() as db:
            evidence=self.engine.cognitive.dialogue.basic_evidence(db,baseline)
            old,_=self.engine.cognitive.dialogue.render(db,baseline,evidence,prepared['character_prepared'])
        self.assertEqual(prepared['messages'],old)
        self.assertEqual(read(self.raw(t['id']))['mode'],'current')

    async def test_current_keeps_existing_selection_and_observation_calls(self):
        self.enable('current');t=await self.turn('书店')
        with patch.object(self.engine.selector,'prepare',new=AsyncMock(return_value=None)) as selector, \
             patch.object(self.engine.observer,'prepare',new=AsyncMock(return_value=None)) as observer, \
             patch.object(self.model,'complete',new=AsyncMock(return_value=(REPLY,{}))):
            await self.engine._run(t['id'])
        selector.assert_awaited_once_with(t['id']);observer.assert_awaited_once_with(t['id'])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed')

    async def test_current_cloud_selection_shares_actual_unlimited_wire(self):
        self.config.update({'character_selector':{'enabled':True,'base_url':'http://127.0.0.1:1/v1','name':'selector'}})
        self.policy(character_retrieval_mode='hybrid_llm');self.enable('current');t=await self.turn('书店')
        from persona_runtime.character_selector_protocol import candidates
        from persona_runtime.provider_compat import build_request
        calls=[]
        def pool(version,policy,members,scenario):
            return candidates(version,members,{'lore':[],'example':[]},policy['config'])
        async def complete(model,messages):
            wire,_=build_request(model,messages);calls.append((model,wire))
            if model['name']=='selector':return '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}',{'total_tokens':1}
            return REPLY,{'total_tokens':2}
        with patch.object(self.rt,'local_pool',side_effect=pool),patch.object(self.model,'complete',side_effect=complete):
            await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed',self.engine.get(t['id'])['error_code'])
        self.assertEqual(len(calls),2)
        for model,wire in calls:
            self.assertEqual(model['_generation_profile'],GENERATION_PROFILE)
            self.assertTrue(wire['stream']);self.assertNotIn('max_tokens',wire)
        with self.store.connection() as db:
            selection=json.loads(db.execute('SELECT usage_json FROM character_selection_attempts WHERE turn_id=?',(t['id'],)).fetchone()[0])
        self.assertIsNone(selection['generation_retry']['attempts'][0]['timeout_seconds'])
        self.assertEqual(selection['generation_retry']['attempts'][0]['request_hash'],fingerprint(calls[0][1]))
        self.assertEqual(json.loads(self.raw(t['id'])['observation_model_snapshot'])['_generation_profile'],GENERATION_PROFILE)

    async def test_old_turn_unchanged_when_future_policy_switches(self):
        old=await self.turn('书店')
        raw=deepcopy(self.raw(old['id']))
        first=self.engine.cognitive.prepare(old['id'])['messages']
        self.enable()
        self.assertEqual(self.raw(old['id'])['model_snapshot'],raw['model_snapshot'])
        self.assertIsNone(read(self.raw(old['id'])))
        self.assertEqual(self.engine.cognitive.prepare(old['id'])['messages'],first)

    async def test_batch_keeps_first_accepted_profile_and_complete_corrections(self):
        p=self.enable()
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        with patch.object(self.engine,'_schedule'):
            r=self.ingress.accept({'text':'书店全整理完了','request_id':identifier(),'channel':'desktop'})
            self.ingress.accept({'text':'更正，只整理了一排，现在不庆祝','request_id':identifier(),'channel':'desktop'})
            p=self.ingress.current_policy(self.pid,'desktop')
            self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),
                companion_mode='current',pending_action='seal')
        t=self.raw(self.ingress.get(r['batch_id'])['turn_id'])
        self.assertEqual(read(t)['mode'],'minimal')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):prepared=self.engine.cognitive.prepare(t['id'])
        self.assertIn('书店全整理完了',prepared['messages'][-1]['content'])
        self.assertIn('更正，只整理了一排，现在不庆祝',prepared['messages'][-1]['content'])
        self.assertEqual(prepared['details']['raw_message_count'],2)

    async def test_malformed_snapshot_fails_before_any_dispatch(self):
        self.enable();t=await self.turn('书店');raw=self.raw(t['id']);model=json.loads(raw['model_snapshot'])
        model['_companion_profile']['mode']='current'
        with self.store.connection(write=True) as db:
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),t['id']))
        await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['error_code'],'companion_snapshot_mismatch')
        self.assertFalse(self.model.calls)

    async def test_policy_conflicts_fail_before_persistence(self):
        for kw in ({'disposition_mode':'assist'}, {'situation_observation_mode':'on_demand'},
                   {'character_retrieval_mode':'hybrid_llm'}):
            with self.assertRaisesRegex(RuntimeErrorBase,'companion_policy_conflict'):self.enable(**kw)

    async def test_final_rebuild_does_not_reintroduce_behavior_or_old_renderer(self):
        self.enable();t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        p['guard']='rebuild required'
        with self.store.connection(write=True) as db,patch('persona_runtime.behavior.resolve_behavior',side_effect=AssertionError('old cues')):
            q=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertEqual(q['messages'],p['messages'])
        self.assertEqual(q['manifest']['input_snapshot_hash'],fingerprint(q['messages']))

    async def test_cancel_running_companion_terminal_without_late_reply(self):
        self.enable();t=await self.turn('书店');entered=asyncio.Event()
        async def waiting(*_):
            entered.set();await asyncio.Event().wait()
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid),patch.object(self.model,'complete',side_effect=waiting):
            self.engine._schedule(t['id']);await entered.wait()
            cancelled=await self.engine.cancel(t['id'])
        self.assertEqual(cancelled['status'],'interrupted')
        self.assertEqual(cancelled['error_code'],'cancelled_by_user')
        self.assertIsNone(cancelled['reply'])
        self.assertFalse(self.engine.locks[self.pid].locked())
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM attempts WHERE turn_id=?',(t['id'],)).fetchone()[0],'failed')
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches WHERE turn_id=?',(t['id'],)).fetchone()[0],0)

    async def test_cancel_suppressed_by_client_cannot_persist_late_reply(self):
        self.enable();t=await self.turn('书店');entered=asyncio.Event()
        async def stubborn(*_):
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:return REPLY,{'total_tokens':2}
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid),patch.object(self.model,'complete',side_effect=stubborn):
            self.engine._schedule(t['id']);await entered.wait()
            result=await self.engine.cancel(t['id'])
        self.assertEqual(result['status'],'interrupted');self.assertIsNone(result['reply'])
        self.assertFalse(self.engine.locks[self.pid].locked())

    async def test_required_input_budget_stops_before_main(self):
        self.enable();t=await self.turn('必须保留的完整输入'*40)
        row=self.raw(t['id']);model=json.loads(row['model_snapshot']);model['max_input_chars']=1
        with self.store.connection(write=True) as db:db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),t['id']))
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['error_code'],'context_limit_exceeded')
        self.assertFalse(self.model.calls)

    async def test_running_legacy_cancel_stays_disallowed(self):
        t=await self.turn('书店')
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET status='running' WHERE id=?",(t['id'],))
        with self.assertRaisesRegex(RuntimeErrorBase,'only_queued_can_cancel'):await self.engine.cancel(t['id'])

    async def test_structural_single_main_actual_hash_and_distinct_renderer(self):
        from persona_runtime.companion_contract import REMOVALS,RENDERER
        self.enable('minimal_structural');t=await self.turn('书店只整理了一排，先去吃饭。')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid), \
             patch.object(self.engine.selector,'prepare',new=AsyncMock(side_effect=AssertionError('cloud selector'))), \
             patch.object(self.engine.observer,'prepare',new=AsyncMock(side_effect=AssertionError('observer'))), \
             patch.object(self.engine.cognitive.shadow,'prepare',side_effect=AssertionError('decision')), \
             patch.object(self.model,'complete',new=AsyncMock(return_value=(REPLY,{'total_tokens':3}))) as main:
            await self.engine._run(t['id'])
        self.assertEqual(main.await_count,1)
        self.assertEqual(self.engine.get(t['id'])['status'],'completed',self.engine.get(t['id'])['error_code'])
        cfg,msgs=main.call_args.args
        self.assertEqual(cfg['_companion_profile'],choose('minimal_structural'))
        self.assertEqual(cfg['_generation_profile'],GENERATION_PROFILE)
        for _,text in REMOVALS:self.assertNotIn(text,msgs[0]['content'])
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(t['id'],)).fetchone()[0])
        self.assertEqual(manifest['input_snapshot_hash'],fingerprint(msgs))
        self.assertEqual(manifest['prompt_assembly']['input_snapshot_hash'],fingerprint(msgs))
        self.assertEqual(manifest['prompt_assembly']['effective']['expression'],'companion-minimal-v1')
        self.assertEqual(manifest['prompt_assembly']['effective']['format'],RENDERER)

    async def test_structural_changes_only_protocol_no_material_or_wire_refill(self):
        from persona_runtime.companion_contract import structural_contract,REMOVALS
        from persona_runtime.provider_compat import build_request
        from persona_runtime.reply_batch import bubble_prompt
        from tests.m71_fixtures import seed_turn
        data=deepcopy(self.data);data['lorebook'][0]['text']+=REMOVALS[0][1];self.save(data)
        with self.store.connection(write=True) as db:
            seed_turn(db,self.pid,self.sid,'structural-history',['昨天书店的阅读角摆好了'],reply='那一角终于能坐下来翻书了')
        self.enable();t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        raw=self.raw(t['id'])
        def render(profile,limit=None):
            row=deepcopy(raw);context=json.loads(row['context_policy_snapshot']);context['companion']=choose(profile)
            model=json.loads(row['model_snapshot']);model['_companion_profile']=choose(profile)
            if limit is not None:model['max_input_chars']=limit
            row['model_snapshot']=json.dumps(model);row['context_policy_snapshot']=json.dumps(context)
            with self.store.connection() as db:
                evidence=self.engine.cognitive.dialogue.basic_evidence(db,row)
                msgs,details=self.engine.cognitive.dialogue.render(db,row,evidence,p['character_prepared'])
            return msgs,details,model
        old,od,om=render('minimal');new,nd,nm=render('minimal_structural')
        self.assertEqual(old[1:],new[1:])
        self.assertGreater(len(old),2)
        self.assertTrue(old[0]['content'].endswith(bubble_prompt()))
        self.assertEqual(new[0]['content'],old[0]['content'][:-len(bubble_prompt())]+structural_contract())
        self.assertEqual(od['character'],nd['character']);self.assertEqual(od['history_rows'],nd['history_rows'])
        old_wire,_=build_request(om,old);new_wire,_=build_request(nm,new)
        self.assertEqual({k:v for k,v in old_wire.items() if k!='messages'},{k:v for k,v in new_wire.items() if k!='messages'})
        # A full-contract budget that rejects this lore would fit it if the
        # reduced contract were incorrectly used before selection/trimming.
        tight=sum(len(m['content']) for m in old)-1
        full,fd,_=render('minimal',tight);reduced,rd,_=render('minimal_structural',tight)
        self.assertNotIn('人工书店有蓝色阅读角',str(full));self.assertNotIn('人工书店有蓝色阅读角',str(reduced))
        self.assertEqual(fd['character'],rd['character']);self.assertEqual(fd['removed'],rd['removed'])
        self.assertEqual(fd['history_rows'],rd['history_rows']);self.assertEqual(full[1:],reduced[1:])
        # The same language in author lore survives; only the protocol changes.
        self.assertEqual(old[0]['content'].count(REMOVALS[0][1]),2)
        self.assertEqual(new[0]['content'].count(REMOVALS[0][1]),1)
        self.assertEqual(len(old[0]['content'])-len(new[0]['content']),sum(len(text) for _,text in REMOVALS))

    async def test_structural_admission_requires_schema3_and_preserves_accepted_profile(self):
        old=self.ingress.current_policy(self.pid,'desktop')
        with self.assertRaisesRegex(RuntimeErrorBase,'companion_format_conflict'):
            self.ingress.update_policy(self.pid,'desktop',old['mode'],old['id'],identifier(),companion_mode='minimal_structural')
        self.assertEqual(self.ingress.current_policy(self.pid,'desktop')['id'],old['id'])
        self.enable('minimal_structural');t=await self.turn('书店')
        before=self.raw(t['id']);self.enable('minimal')
        self.assertEqual(self.raw(t['id'])['model_snapshot'],before['model_snapshot'])
        self.assertEqual(read(self.raw(t['id'])),choose('minimal_structural'))
        current=self.ingress.current_policy(self.pid,'desktop')
        self.assertEqual(current['companion'],choose('minimal'))

    async def test_structural_invalid_snapshot_zero_calls(self):
        self.enable('minimal_structural');t=await self.turn('书店');row=self.raw(t['id'])
        expression=json.loads(row['expression_snapshot']);expression['version']=2
        with self.store.connection(write=True) as db:
            db.execute('UPDATE turns SET expression_snapshot=? WHERE id=?',(json.dumps(expression),t['id']))
        await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['error_code'],'companion_format_conflict')
        self.assertFalse(self.model.calls)

    async def test_structural_final_recheck_preserves_contract_and_rejects_foreign_source(self):
        from persona_runtime.companion_contract import RENDERER
        self.enable('minimal_structural');t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        p['guard']='force source recheck'
        with self.store.connection(write=True) as db:q=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertEqual(q['messages'],p['messages'])
        self.assertEqual(q['manifest']['input_snapshot_hash'],fingerprint(q['messages']))
        self.assertEqual(q['manifest']['prompt_assembly']['effective']['format'],RENDERER)
        row=self.raw(t['id']);snapshot=json.loads(row['character_snapshot']);snapshot['persona_id']='foreign'
        row['character_snapshot']=json.dumps(snapshot)
        with self.store.connection(write=True) as db,self.assertRaisesRegex(RuntimeErrorBase,'character_snapshot_invalid'):
            self.engine.cognitive.finalize(db,row,q)

    async def test_structural_rebuild_drops_withdrawn_relationship_source(self):
        from persona_runtime.profile import append_version
        from tests.m71_fixtures import seed_turn
        with self.store.connection(write=True) as db:
            sources=seed_turn(db,self.pid,self.sid,'relationship-source',['人工确认的关系依据'],reply='明白了')
            append_version(db,self.pid,'relationship',{'description':'有来源的人工旧关系'},source_ids=sources)
        self.enable('minimal_structural');t=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):p=self.engine.cognitive.prepare(t['id'])
        self.assertIn('有来源的人工旧关系',str(p['messages']))
        memory=self.service.add_memory('user_fact','人工关系依据',sources[0]);self.service.retract_memory(memory['id'])
        with self.store.connection(write=True) as db:q=self.engine.cognitive.finalize(db,self.raw(t['id']),p)
        self.assertNotIn('有来源的人工旧关系',str(q['messages']))
        self.assertTrue(q['details']['relationship_requires_review'])
        self.assertEqual(q['manifest']['input_snapshot_hash'],fingerprint(q['messages']))
        self.assertEqual(q['manifest']['prompt_assembly']['effective']['format'],'bubbles-schema3-structural-v1')
