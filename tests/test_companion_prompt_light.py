"""Light project wording, actual admission, bounded inputs and paid ownership."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import companion_prompt_light as light
from persona_runtime.archive import fingerprint, serialize_reference_block
from persona_runtime.companion import MINIMAL_INSTRUCTION, checked, choose, read
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.prompt_assembly import PromptBlock
from persona_runtime.reply_batch import expression_snapshot, validate_reply
from experiments import companion_c2 as replay
from experiments import companion_prompt_light as run
from tests import test_companion_chat_contract as runtime_fixture
from tests import test_companion_c2 as replay_fixture
from tests.companion_persona_fixture import legacy_requests


class Templates(unittest.TestCase):
    def test_bound_reference_and_summary_keep_bodies_and_invalid_reference_still_fails(self):
        from tests.prompt_baseline_fixture import capture
        from persona_runtime.context_m4 import build_context
        from persona_runtime.companion import freeze_model
        def builder(mode):
            def render(engine,db,turn,**kwargs):
                row=deepcopy(turn)
                policy={'companion':choose(mode),'character_retrieval':{'mode':'hybrid'},
                        'disposition_mode':'baseline','situation_observation_mode':'disabled'}
                row['context_policy_snapshot']=json.dumps(policy)
                row['model_snapshot']=json.dumps(freeze_model(json.loads(row['model_snapshot']),policy))
                return build_context(engine,db,row,character=kwargs['character'])
            return render
        case={'reference':True,'summary':True,'mode':'natural_structured','expression':3}
        old=capture(case,builder('minimal_chat'));new=capture(case,builder('minimal_experience'))
        self.assertNotIn('error_code',new)
        self.assertEqual(old['messages'][1:],new['messages'][1:])
        self.assertIn(light.NEUTRAL_REFERENCE_HEADER,new['messages'][0]['content'])
        self.assertIn('\n前文概要：',new['messages'][0]['content'])
        self.assertIn('人工青禾摘要',new['messages'][0]['content'])
        self.assertNotIn(light.REFERENCE_HEADER,new['messages'][0]['content'])
        self.assertIn('不虚构共同经历',new['messages'][0]['content'])
        bad=dict(case,blocked=True)
        self.assertEqual(capture(bad,builder('minimal_chat')),capture(bad,builder('minimal_experience')))

    def test_exact_owned_templates_preserve_negative_words_in_material_and_quotes(self):
        author='{"values_and_boundaries":["不要改写作者原文"]}'
        quoted='{"turns":[{"text":"不代表已经执行了现实操作。"}]}'
        summary='[{"text":"有损摘要：这是正文"}]'
        blocks=[PromptBlock('persona_expression',MINIMAL_INSTRUCTION+author,'old','v1'),
                PromptBlock('character','不要删除这里的材料','old','v1'),
                PromptBlock('explicit_references',light.REFERENCE_HEADER+quoted,'old','v1'),
                PromptBlock('memory_summary','\n有损摘要：'+summary,'old','v1'),
                PromptBlock('reply_protocol',chat_contract(),'old','v1')]
        before=deepcopy(blocks)
        result=light.render_blocks(blocks,legacy_instruction=MINIMAL_INSTRUCTION,legacy_contract=chat_contract())
        self.assertEqual(blocks,before)
        self.assertEqual(result[0].content,light.INSTRUCTION+author)
        self.assertEqual(result[1].content,blocks[1].content)
        self.assertEqual(result[2].content,light.NEUTRAL_REFERENCE_HEADER+quoted)
        self.assertEqual(result[3].content,'\n前文概要：'+summary)
        self.assertEqual(result[4].content,light.CONTRACT)
        self.assertTrue(serialize_reference_block([{}],[]).startswith(light.REFERENCE_HEADER))

    def test_mismatched_template_stops_instead_of_rewriting_arbitrary_text(self):
        blocks=[PromptBlock('persona_expression','changed','owner','v1'),
                PromptBlock('reply_protocol',chat_contract(),'owner','v1')]
        with self.assertRaisesRegex(RuntimeErrorBase,'instruction_boundary_changed'):
            light.render_blocks(blocks,legacy_instruction=MINIMAL_INSTRUCTION,legacy_contract=chat_contract())
        blocks[0]=PromptBlock('persona_expression',MINIMAL_INSTRUCTION,'owner','v1')
        blocks[1]=PromptBlock('reply_protocol','changed','owner','v1')
        with self.assertRaisesRegex(RuntimeErrorBase,'contract_boundary_changed'):
            light.render_blocks(blocks,legacy_instruction=MINIMAL_INSTRUCTION,legacy_contract=chat_contract())

    def test_profile_desktop_and_existing_parser_capacity(self):
        from persona_runtime.desktop_context import companion_candidate, companion_description, companion_activation_allowed
        from persona_runtime.desktop_client import LocalClient
        profile=choose('minimal_experience')
        self.assertEqual(profile['version'],'companion-runtime-v6')
        self.assertEqual(profile['format_contract'],'schema3-companion-v3')
        with self.assertRaises(RuntimeErrorBase):checked(profile|{'instruction_version':'unknown'})
        self.assertTrue(companion_candidate({'companion':profile}))
        self.assertFalse(companion_activation_allowed({'companion':profile}))
        self.assertIn('4-E.5',companion_description({'companion':profile}))
        client=LocalClient.__new__(LocalClient)
        with patch.object(client,'require_capability') as capability, patch.object(client,'manage'):
            client.update_companion_policy(companion_mode='minimal_experience')
        capability.assert_called_once_with('companion_prompt_light_v1')
        bodies=['好','一段解释'*80,'```python\nprint(168)\n```']
        value={'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':body,'content_kind':'code' if i==2 else 'chat','cancel_if_new_input':i==2}
            for i,body in enumerate(bodies)]}
        batch=validate_reply(json.dumps(value),'natural_structured',expression_snapshot('natural_structured'))
        self.assertEqual([s.text for s in batch.segments],bodies)
        self.assertFalse(batch.segments[-1].cancel_if_new_input)


class NormalRuntime(unittest.IsolatedAsyncioTestCase):
    setUp=runtime_fixture.Runtime.setUp
    save=runtime_fixture.Runtime.save
    hybrid=runtime_fixture.Runtime.hybrid
    policy=runtime_fixture.Runtime.policy
    raw=runtime_fixture.Runtime.raw
    turn=runtime_fixture.Runtime.turn
    enable=runtime_fixture.Runtime.enable

    async def test_actual_admission_snapshot_manifest_and_single_generation(self):
        self.enable('minimal_experience');turn=await self.turn('书店')
        frozen=self.raw(turn['id'])
        self.enable('minimal_chat')
        self.assertEqual(self.raw(turn['id'])['model_snapshot'],frozen['model_snapshot'])
        self.assertEqual(read(frozen),choose('minimal_experience'))
        response=json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'这里挺有意思。','content_kind':'chat','cancel_if_new_input':False}]})
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid),patch.object(self.model,'complete',new=AsyncMock(return_value=(response,None))) as client:
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        self.assertEqual(client.await_count,1)
        _,messages=client.call_args.args
        self.assertTrue(messages[0]['content'].startswith(light.INSTRUCTION))
        self.assertTrue(messages[0]['content'].endswith(light.CONTRACT))
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(turn['id'],)).fetchone()[0])
        self.assertEqual(manifest['input_snapshot_hash'],fingerprint(messages))
        self.assertEqual(manifest['prompt_assembly']['effective']['expression'],light.INSTRUCTION_VERSION)
        self.assertEqual(manifest['prompt_assembly']['effective']['format'],light.FORMAT_RENDERER)

    async def test_registered_bundle_only_under_normal_and_tight_budget(self):
        self.enable('minimal_chat');turn=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):prepared=self.engine.cognitive.prepare(turn['id'])
        original=self.raw(turn['id'])
        def render(mode,limit):
            row=deepcopy(original);model=json.loads(row['model_snapshot']);policy=json.loads(row['context_policy_snapshot'])
            model['_companion_profile']=policy['companion']=choose(mode);model['max_input_chars']=limit
            row.update(model_snapshot=json.dumps(model),context_policy_snapshot=json.dumps(policy))
            with self.store.connection() as db:
                evidence=self.engine.cognitive.dialogue.basic_evidence(db,row)
                return self.engine.cognitive.dialogue.render(db,row,evidence,prepared['character_prepared'])
        full=render('minimal',24000)[0]
        for limit in (24000,sum(len(m['content']) for m in full)-1):
            old,od=render('minimal_chat',limit);new,nd=render('minimal_experience',limit)
            self.assertEqual(old[1:],new[1:])
            self.assertEqual(od['removed'],nd['removed'])
            self.assertEqual(od['character'],nd['character'])
            self.assertTrue(new[0]['content'].startswith(light.INSTRUCTION))
            self.assertTrue(new[0]['content'].endswith(light.CONTRACT))

    async def test_old_five_versions_match_prechange_artificial_requests(self):
        actual={}
        for mode in ('minimal','minimal_structural','minimal_chat','minimal_persona','minimal_context_rules_off'):
            actual.update(await legacy_requests((mode,)))
        fixture=Path(__file__).parent/'fixtures/companion-pre-v6.json'
        self.assertEqual(actual,json.loads(fixture.read_text(encoding='utf-8'))['requests'])

    async def test_snapshot_mismatch_rejected_before_model(self):
        self.enable('minimal_experience');turn=await self.turn('书店')
        with self.store.connection(write=True) as db:
            row=self.raw(turn['id']);model=json.loads(row['model_snapshot'])
            model['_companion_profile']=choose('minimal_chat')
            db.execute('UPDATE turns SET model_snapshot=? WHERE id=?',(json.dumps(model),turn['id']))
        await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['error_code'],'companion_snapshot_mismatch')
        self.assertFalse(self.model.calls)

    async def test_restore_keeps_initial_settings(self):
        before=self.ingress.current_policy(self.pid,'desktop')
        self.enable('minimal_chat');saved=json.loads(self.ingress.current_policy(self.pid,'desktop')['data_json'])['companion_restore']
        self.enable('minimal_experience');current=self.ingress.current_policy(self.pid,'desktop')
        self.assertEqual(json.loads(current['data_json'])['companion_restore'],saved)
        restored=self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],identifier(),companion_mode='disabled')
        self.assertEqual(restored['reply_mode'],before['reply_mode'])
        self.assertNotIn('companion',json.loads(restored['data_json']))

    async def test_expired_state_final_recheck_uses_current_light_template(self):
        from datetime import datetime,timedelta,timezone
        from persona_runtime.profile import append_version
        expiry=datetime.now(timezone.utc)+timedelta(minutes=1)
        with self.store.connection(write=True) as db:
            append_version(db,self.pid,'state',{'text':'人工短期状态','expires_at':expiry.isoformat()})
        self.enable('minimal_experience');turn=await self.turn('书店')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):prepared=self.engine.cognitive.prepare(turn['id'])
        self.assertIn('人工短期状态',str(prepared['messages']))
        with patch('persona_runtime.profile.datetime',wraps=datetime) as clock:
            clock.now.return_value=expiry+timedelta(seconds=1)
            with self.store.connection(write=True) as db:final=self.engine.cognitive.finalize(db,self.raw(turn['id']),prepared)
        self.assertNotIn('人工短期状态',str(final['messages']))
        self.assertTrue(final['messages'][0]['content'].startswith(light.INSTRUCTION))
        self.assertEqual(final['manifest']['input_snapshot_hash'],fingerprint(final['messages']))


class ArtificialReplay(replay_fixture.Replay):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async def prepare(*args,**kwargs):return await run.prepare(self.home,development=True)
        self.enterContext(patch.object(replay,'prepare',prepare))

    async def test_sixteen_runtime_targets_only_contract_diff_and_no_success_replay(self):
        root=await run.prepare(self.home,development=True)
        owner,plan=replay.verify(root)
        self.assertFalse(owner['private'])
        self.assertEqual(owner['material_source'],'artificial')
        self.assertEqual(len(plan['positions']),12)
        self.assertEqual(len(plan['pair_checks']),6)
        self.assertTrue(all(r['only_registered_bundle_differs'] for r in plan['pair_checks']))
        client=replay.base.FixtureClient()
        summary=await replay.execute(root,simulated=True,client=client)
        self.assertEqual((summary['completed'],summary['physical_calls'],summary['simulated_calls']),(12,0,12))
        audit=run.audit_trial(root)
        self.assertTrue(all(r[k] for r in audit['rows'] for k in ('manifest_matches','audit_matches','wire_matches','attempts_terminal')))
        with self.assertRaisesRegex(Exception,'no_auto_resume'):await replay.execute(root,simulated=True)

    async def test_artificial_real_scope_cannot_be_borrowed_or_faked(self):
        root=await run.prepare(self.home,development=True)
        owner,_=replay.verify(root)
        with self.assertRaisesRegex(Exception,'execution_not_authorized'):
            await replay.run_position(root,run.positions()[0]['position_id'])
        with self.assertRaisesRegex(Exception,'development_real_forbidden'):
            await run.prepare(self.home,development=True,real_authorized=True)
        owner['generation_authorization']='real_main_only'
        with self.assertRaisesRegex(Exception,'development_real_forbidden'):run.verify_execution(owner,False)
        owner['private']=True
        with self.assertRaisesRegex(Exception,'owner_scope'):run.validate_owner(root,owner,self.home)

    async def test_tamper_and_real_scope_rejected_before_dispatch(self):
        root=await run.prepare(self.home,development=True)
        owner,plan=replay.verify(root)
        with self.assertRaisesRegex(Exception,'execution_not_authorized'):
            await replay.run_position(root,plan['order'][0])
        path=replay.data_root(owner)/'preflight'/plan['order'][0]/'request.json'
        value=replay.read(path);value['messages'][-1]['content']='modified'
        replay.atomic_json(path,value)
        with self.assertRaisesRegex(Exception,'frozen_request_changed'):
            await replay.run_position(root,plan['order'][0],simulated=True)

    async def test_non_run_actions_stay_offline_and_legal_semantic_errors_do_not_retry(self):
        root=await run.prepare(self.home,development=True)
        client=replay.base.FixtureClient(['这是合法但语义不合适的离线回包。'])
        result=await replay.run_position(root,run.positions()[0]['position_id'],simulated=True,client=client)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['generation_attempts'],1)
        with replay.base.offline():
            replay.verify(root);summary=run.report_trial(root)
        self.assertEqual(summary['physical_calls'],0)
        self.assertEqual(summary['completed'],1)
        self.assertTrue(all(p['attribution']=='incomplete_pair' for p in summary['pairs']))

    async def test_materials_contain_no_calibration_responses_or_labels(self):
        from experiments.companion_prompt_light_materials import PERSONA,CASES
        self.assertEqual(len(CASES),6)
        self.assertTrue(all(set(c)=={'id','history','current_input'} for c in CASES))
        self.assertNotIn('不虚构共同经历',json.dumps(PERSONA,ensure_ascii=False))
        self.assertNotIn('评分',json.dumps(CASES,ensure_ascii=False))


class Desktop(unittest.TestCase):
    from tests import test_companion_workbench_desktop as fixture
    setUp=fixture.CompanionDesktopTests.setUp
    window=fixture.CompanionDesktopTests.window
    doCleanups=fixture.CompanionDesktopTests.doCleanups
    ready=fixture.CompanionDesktopTests.ready

    def test_new_candidate_controls_and_existing_enable_target(self):
        value=self.fixture.policy(True);value['companion']=choose('minimal_experience')
        data=json.loads(value['data_json']);data['companion']=value['companion'];value['data_json']=json.dumps(data)
        w=self.ready(value);w.remember_companion_policy(value)
        self.assertEqual(w.ctx_companion_enable.cget('text'),'升级伴侣聊天')
        self.assertEqual(str(w.ctx_companion_enable.cget('state')),'normal')
        self.assertEqual(str(w.ctx_character_combo.cget('state')),'disabled')
        self.assertIn('轻量化',w.chat_mode_text.get())
