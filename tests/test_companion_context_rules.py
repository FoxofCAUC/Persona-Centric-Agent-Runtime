"""Final field consumption, old requests and the owned normal replay flow."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import companion_consumption as consumer
from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose, checked, read
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.prompt_assembly import bounded
from experiments import companion_context_rules as run
from experiments import companion_c2 as replay
from tests import test_companion_chat_contract as runtime_fixture
from tests import test_companion_c2 as replay_fixture
from tests.companion_persona_fixture import legacy_requests
prepare_original = replay.prepare


class Consumption(unittest.TestCase):
    def test_exact_field_both_kinds_no_mutation_or_keyword_removal(self):
        parts = [{'kind':kind, 'id':key, 'text':'Artificial preserved string', 'required':True}
                 for kind,key in (('anchor',consumer.FIELD),('fact',consumer.FIELD),
                                  ('anchor','social.humor'),('example',consumer.FIELD))]
        before = deepcopy(parts)
        kept, info = consumer.consume(parts)
        self.assertEqual(parts,before)
        self.assertEqual(kept,parts[2:])
        self.assertIs(kept[0],parts[2])
        self.assertEqual(len(info['excluded_parts']),2)
        self.assertNotIn('Artificial',json.dumps(info))
        self.assertEqual(consumer.consume([])[1]['status'],'no_consumption_difference')

    def test_profile_diagnostic_bounds_and_desktop_identification(self):
        from persona_runtime.desktop_context import companion_candidate, companion_description, companion_activation_allowed
        profile=choose('minimal_context_rules_off')
        self.assertEqual(profile['version'],'companion-runtime-v5')
        with self.assertRaises(RuntimeErrorBase):checked(profile|{'character_consumer':'unknown'})
        policy={'companion':profile}
        self.assertTrue(companion_candidate(policy))
        self.assertFalse(companion_activation_allowed(policy))
        self.assertIn('4-E.3',companion_description(policy))
        info=consumer.consume([{'kind':'anchor','id':consumer.FIELD,'text':'x'}])[1]
        info['final_input_hash']='artificial'
        info['budget_retained_parts']*=2000
        value=bounded({'blocks':[],'character_consumption':info})
        self.assertTrue(value['character_consumption']['details_omitted'])


class NormalRuntime(unittest.IsolatedAsyncioTestCase):
    setUp=runtime_fixture.Runtime.setUp
    save=runtime_fixture.Runtime.save
    hybrid=runtime_fixture.Runtime.hybrid
    policy=runtime_fixture.Runtime.policy
    raw=runtime_fixture.Runtime.raw
    turn=runtime_fixture.Runtime.turn
    enable=runtime_fixture.Runtime.enable

    async def test_normal_admission_diagnostic_and_saved_snapshot(self):
        data=deepcopy(self.data);data['social']['context_rules']='Artificial specific rule';self.save(data)
        self.enable('minimal_context_rules_off')
        turn=await self.turn('你有什么社交习惯，social.context_rules')
        frozen=self.raw(turn['id'])
        self.enable('minimal_chat')
        self.assertEqual(self.raw(turn['id'])['model_snapshot'],frozen['model_snapshot'])
        self.assertEqual(read(frozen),choose('minimal_context_rules_off'))
        raw=json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'各有自己的想法。','content_kind':'chat','cancel_if_new_input':False}]})
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid), \
             patch.object(self.model,'complete',new=AsyncMock(return_value=(raw,None))) as client:
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        self.assertEqual(client.await_count,1)
        _,messages=client.call_args.args
        self.assertNotIn('Artificial specific rule',messages[0]['content'])
        self.assertTrue(messages[0]['content'].endswith(chat_contract()))
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(turn['id'],)).fetchone()[0])
        info=manifest['prompt_assembly']['character_consumption']
        self.assertEqual(info['final_input_hash'],fingerprint(messages))
        self.assertEqual({p['kind'] for p in info['excluded_parts']},{'anchor','fact'})
        self.assertNotIn(consumer.FIELD,manifest['prompt_assembly']['material_selection']['fact_paths'])

    async def test_budget_does_not_refill_and_only_registered_field_changes(self):
        data=deepcopy(self.data);data['social']['context_rules']='Artificial specific rule';self.save(data)
        self.enable('minimal_chat');turn=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid): prepared=self.engine.cognitive.prepare(turn['id'])
        row=self.raw(turn['id'])
        def render(mode,limit):
            changed=deepcopy(row);model=json.loads(changed['model_snapshot']);policy=json.loads(changed['context_policy_snapshot'])
            model['_companion_profile']=policy['companion']=choose(mode)
            model['max_input_chars']=limit
            changed.update(model_snapshot=json.dumps(model),context_policy_snapshot=json.dumps(policy))
            with self.store.connection() as db:
                evidence=self.engine.cognitive.dialogue.basic_evidence(db,changed)
                return self.engine.cognitive.dialogue.render(db,changed,evidence,prepared['character_prepared'])
        full = render('minimal',24000)[0]
        for limit in (24000, sum(len(m['content']) for m in full)-1):
            before,old=render('minimal_chat',limit)
            after,new=render('minimal_context_rules_off',limit)
            self.assertEqual(before[1:],after[1:])
            self.assertEqual(old['removed'],new['removed'])
            self.assertEqual(old['character']['lore_ids'],new['character']['lore_ids'])
            self.assertEqual(old['character']['example_ids'],new['character']['example_ids'])
            self.assertEqual([r for r in old['character']['removed'] if r.get('reason')!=consumer.REASON],
                             [r for r in new['character']['removed'] if r.get('reason')!=consumer.REASON])
            self.assertNotIn('Artificial specific rule',after[0]['content'])

    async def test_old_v4_requests_match_frozen_before_implementation(self):
        self.assertEqual(await legacy_requests(('minimal_persona',)),
                         json.loads((Path(__file__).parent/'fixtures/companion-pre-v5.json').read_text(encoding='utf-8'))['requests'])

    async def test_final_source_recheck_rebuilds_consumer_diagnostic(self):
        from persona_runtime.profile import append_version
        from tests.m71_fixtures import seed_turn
        with self.store.connection(write=True) as db:
            sources=seed_turn(db,self.pid,self.sid,'rules-relationship-source',['人工确认关系'],reply='明白了')
            append_version(db,self.pid,'relationship',{'description':'人工有来源的旧关系'},source_ids=sources)
        data=deepcopy(self.data);data['social']['context_rules']='Artificial specific rule';self.save(data)
        self.enable('minimal_context_rules_off');turn=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):prepared=self.engine.cognitive.prepare(turn['id'])
        self.assertIn('人工有来源的旧关系',str(prepared['messages']))
        memory=self.service.add_memory('user_fact','人工关系依据',sources[0]);self.service.retract_memory(memory['id'])
        with self.store.connection(write=True) as db:final=self.engine.cognitive.finalize(db,self.raw(turn['id']),prepared)
        self.assertNotIn('人工有来源的旧关系',str(final['messages']))
        self.assertNotIn('Artificial specific rule',str(final['messages']))
        info=final['manifest']['prompt_assembly']['character_consumption']
        self.assertEqual(info['final_input_hash'],fingerprint(final['messages']))
        self.assertNotEqual(info['final_input_hash'],prepared['manifest']['input_snapshot_hash'])

    async def test_expired_state_rebuilds_consumer_hash(self):
        from datetime import datetime, timedelta, timezone
        from persona_runtime.profile import append_version
        expiry=datetime.now(timezone.utc)+timedelta(minutes=1)
        with self.store.connection(write=True) as db:
            append_version(db,self.pid,'state',{'text':'人工即将过期状态','expires_at':expiry.isoformat()})
        self.enable('minimal_context_rules_off');turn=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):prepared=self.engine.cognitive.prepare(turn['id'])
        self.assertIn('人工即将过期状态',str(prepared['messages']))
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            with self.store.connection(write=True) as db:final=self.engine.cognitive.finalize(db,self.raw(turn['id']),prepared)
        self.assertNotIn('人工即将过期状态',str(final['messages']))
        self.assertEqual(final['manifest']['prompt_assembly']['character_consumption']['final_input_hash'],fingerprint(final['messages']))

    async def test_upgrade_keeps_initial_restore_and_restores_through_normal_policy(self):
        from persona_runtime.models import identifier
        before=self.ingress.current_policy(self.pid,'desktop')
        self.enable('minimal_chat');saved=json.loads(self.ingress.current_policy(self.pid,'desktop')['data_json'])['companion_restore']
        self.enable('minimal_context_rules_off');current=self.ingress.current_policy(self.pid,'desktop')
        self.assertEqual(json.loads(current['data_json'])['companion_restore'],saved)
        restored=self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],identifier(),companion_mode='disabled')
        self.assertEqual(restored['reply_mode'],before['reply_mode'])
        self.assertNotIn('companion',json.loads(restored['data_json']))


class RulesReplay(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=replay_fixture.Replay.asyncSetUp

    async def prepare_compat(self,*args,**kwargs):
        return await prepare_original(self.home,version=run.VERSION)

    async def test_missing_field_does_not_hide_an_incomplete_pair(self):
        with patch.object(run,'artificial_persona',return_value=None):root=await run.prepare(self.home)
        _,plan=replay.verify(root)
        self.assertTrue(all(not p['actual_consumption_difference'] for p in plan['pair_checks']))
        summary=run.report_trial(root)
        self.assertEqual({p['attribution'] for p in summary['pairs']},{'incomplete_pair'})

    async def test_all24_actual_runtime_requests_and12_report_pairs(self):
        root=await run.prepare(self.home)
        owner,plan=replay.verify(root)
        self.assertEqual(len(plan['positions']),24)
        self.assertEqual(len(plan['pair_checks']),12)
        client=replay.base.FixtureClient()
        summary=await replay.execute(root,simulated=True,client=client)
        self.assertEqual((summary['completed'],summary['physical_calls'],summary['simulated_calls']),(24,0,24))
        self.assertEqual(len(summary['pairs']),12)
        self.assertEqual(len(client.calls),24)
        audit=run.audit_trial(root)
        self.assertTrue(all(r['manifest_matches'] and r['wire_matches'] and r['audit_matches'] for r in audit['rows']))
        with self.assertRaisesRegex(Exception,'no_auto_resume'):await replay.execute(root,simulated=True)

    async def test_retry_recovery_and_exhaustion(self):
        with patch.object(replay,'prepare',self.prepare_compat):
            await replay_fixture.Replay.test_retry_recovery_and_exhaustion_preserve_input(self)

    async def test_cancellation_has_terminal(self):
        with patch.object(replay,'prepare',self.prepare_compat):
            await replay_fixture.Replay.test_cancellation_has_terminal_and_stops_following_positions(self)

    async def test_write_failure_prevents_dispatch(self):
        with patch.object(replay,'prepare',self.prepare_compat):
            await replay_fixture.Replay.test_reservation_write_failure_never_dispatches(self)

    async def test_terminal_failure_keeps_breakpoint(self):
        with patch.object(replay,'prepare',self.prepare_compat):
            await replay_fixture.Replay.test_terminal_failure_keeps_breakpoint_and_actual_call_count(self)
