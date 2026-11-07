"""Purpose renderer and actual normal-entry integration, using artificial data."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import companion_persona as persona
from persona_runtime.archive import fingerprint
from persona_runtime.character import empty_character
from persona_runtime.character_context import ALIASES
from persona_runtime.character_retrieval import assemble
from persona_runtime.companion import choose, checked
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.models import generic_persona, RuntimeErrorBase
from persona_runtime.prompt_assembly import bounded, byte_size, DIAGNOSTIC_BYTES
from persona_runtime.provider_compat import build_request
from tests.companion_persona_fixture import legacy_requests
from tests import test_companion_chat_contract as fixture


def material(query='你多大了，你喜欢什么'):
    data=empty_character()
    data['identity']['gender']='女'
    data['social']['context_rules']='允许表达不同意见。'
    data['world']['description']='人工海边小镇。'
    version={'id':'artificial-purpose-character','revision':1,'data':data}
    return assemble(version,[query],{'lore':[],'example':[]},2)


class Rendering(unittest.TestCase):
    def test_sections_empty_facts_and_sources_without_body(self):
        spec=generic_persona().to_dict()
        selected=material()
        before=deepcopy((spec,selected))
        text,info,included=persona.render(spec,selected['parts'])
        self.assertEqual((spec,selected),before)
        for heading in ('你是谁','你的过往和生活','你待人做事的分寸'):
            self.assertIn(heading,text)
        self.assertNotIn('说话示范',text)
        for technical in ('format_version','"kind"','"id"','unset','social.context_rules'):
            self.assertNotIn(technical,text)
        self.assertIn('允许表达不同意见。',text.split('你待人做事的分寸')[1])
        self.assertIn(persona.UNCERTAINTY,text)
        self.assertEqual(info['hard_uncertainty_fields'],['age.years'])
        self.assertFalse(any(p['kind']=='fact' for p in included))
        self.assertNotIn('人工海边小镇',json.dumps(info,ensure_ascii=False))
        self.assertEqual(info['history_symptom_review']['status'],'not_reviewed')

    def test_all_supported_paths_and_known_values(self):
        self.assertEqual(set(ALIASES),set(persona.FIELDS))
        selected=material('你性别是什么')
        text,info,included=persona.render(generic_persona().to_dict(),selected['parts'])
        self.assertEqual(text.count('性别\n女'),1)
        self.assertEqual(len([r for r in info['fields'] if r['field_id']=='identity.gender']),2)
        self.assertIn('same_field_merged',[r.get('reason') for r in info['fields']])
        self.assertEqual(len([p for p in included if p['id']=='identity.gender']),2)
        fact={'kind':'fact','id':'appearance.height_cm','required':True,
              'text':json.dumps({'kind':'fact','id':'appearance.height_cm',
                                 'data':{'status':'value','value':168,'unit':'cm'}})}
        self.assertIn('168厘米',persona.render(generic_persona().to_dict(),[fact])[0])

    def test_preference_does_not_add_hard_uncertainty_or_extra_material(self):
        selected=material('你喜欢什么')
        text,info,_=persona.render(generic_persona().to_dict(),selected['parts'])
        self.assertNotIn(persona.UNCERTAINTY,text)
        self.assertEqual(info['hard_uncertainty_fields'],[])
        self.assertEqual(len(info['selected_parts']),len(selected['parts']))

    def test_examples_lore_namespaces_and_budget(self):
        parts=[]
        for kind,item,data in (
            ('lore_group','group',[{'id':'core.name','title':'人工往事','text':'海边修桥。'}]),
            ('example','core.name',{'scenario':'分享进展','text':'原来是这一步最费时间。'}),
        ):
            parts.append({'kind':kind,'id':item,'text':json.dumps({'kind':kind,'id':item,'data':data}),'required':False})
        text,info,_=persona.render(generic_persona().to_dict(),parts)
        self.assertIn('说话示范',text)
        self.assertIn('海边修桥。',text)
        self.assertNotIn('core.name',text)
        payload=json.loads(parts[1]['text']);payload['data']['text']='过长'*400
        parts[1]['text']=json.dumps(payload)
        with self.assertRaisesRegex(RuntimeErrorBase,'budget_exceeded'):
            persona.render(generic_persona().to_dict(),parts)

    def test_diagnostics_bounded_and_unknown_fields_rejected(self):
        _,info,_=persona.render(generic_persona().to_dict(),material()['parts'])
        info['fields']*=600
        bounded_info=bounded({'blocks':[],'persona_purpose':info,'input_snapshot_hash':'artificial'})
        self.assertLessEqual(byte_size(bounded_info),DIAGNOSTIC_BYTES)
        self.assertTrue(bounded_info['persona_purpose']['details_omitted'])
        part={'kind':'anchor','id':'unknown.field','text':json.dumps({'kind':'anchor','id':'unknown.field','data':'人工'})}
        with self.assertRaisesRegex(RuntimeErrorBase,'field_unknown'):
            persona.render(generic_persona().to_dict(),[part])
        with self.assertRaises(RuntimeErrorBase):
            checked(choose('minimal_persona')|{'persona_renderer':'unknown'})


class Legacy(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_nine_legacy_requests_and_parameters(self):
        baseline=json.loads((Path(__file__).parent/'fixtures/companion-pre-v4.json').read_text(encoding='utf-8'))
        with patch('socket.socket.connect',side_effect=AssertionError('offline_only')):
            self.assertEqual(await legacy_requests(),baseline['requests'])


class NormalEntry(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Runtime.setUp
    save=fixture.Runtime.save
    hybrid=fixture.Runtime.hybrid
    policy=fixture.Runtime.policy
    raw=fixture.Runtime.raw
    turn=fixture.Runtime.turn
    enable=fixture.Runtime.enable

    async def test_final_sent_message_manifest_and_no_hidden_generation(self):
        self.enable('minimal_persona')
        turn=await self.turn('你多大了，你喜欢什么')
        raw=json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'今天想去海边走走。','content_kind':'chat','cancel_if_new_input':False}]})
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid), \
             patch.object(self.model,'complete',new=AsyncMock(return_value=(raw,None))) as client:
            await self.engine._run(turn['id'])
        self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        self.assertEqual(client.await_count,1)
        model,messages=client.call_args.args
        self.assertEqual(model['_companion_profile'],choose('minimal_persona'))
        self.assertTrue(messages[0]['content'].endswith(chat_contract()))
        self.assertNotIn('作为交流材料',messages[0]['content'])
        with self.store.connection() as db:
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(turn['id'],)).fetchone()[0])
        self.assertEqual(manifest['input_snapshot_hash'],fingerprint(messages))
        info=manifest['prompt_assembly']['persona_purpose']
        self.assertEqual(info['version'],persona.VERSION)
        self.assertEqual(manifest['prompt_assembly']['material_selection']['fact_paths'],[])
        self.assertEqual(info['hard_uncertainty_fields'],['age.years'])

    async def test_pair_only_replaces_registered_blocks(self):
        self.enable('minimal_chat')
        turn=await self.turn('你喜欢什么，书店怎么样')
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid):
            prepared=self.engine.cognitive.prepare(turn['id'])
        control=prepared['messages']
        row=self.raw(turn['id'])
        model=json.loads(row['model_snapshot']);policy=json.loads(row['context_policy_snapshot'])
        model['_companion_profile']=policy['companion']=choose('minimal_persona')
        row.update(model_snapshot=json.dumps(model),context_policy_snapshot=json.dumps(policy))
        with self.store.connection() as db:
            evidence=self.engine.cognitive.dialogue.basic_evidence(db,row)
            candidate,details=self.engine.cognitive.dialogue.render(db,row,evidence,prepared['character_prepared'])
        self.assertEqual(control[1:],candidate[1:])
        old_blocks={b['name']:b for b in prepared['manifest']['prompt_assembly']['blocks']}
        for block in details['prompt_assembly']['blocks']:
            if block['name'] not in ('persona_expression','character'):
                self.assertEqual(block['content_hash'],old_blocks[block['name']]['content_hash'])
        self.assertEqual(build_request(model,control)[0]|{'messages':[]},build_request(model,candidate)[0]|{'messages':[]})

    async def test_policy_switch_does_not_change_accepted_v3(self):
        self.enable('minimal_chat')
        turn=await self.turn('人工分享')
        original=self.raw(turn['id'])
        self.enable('minimal_persona')
        self.assertEqual(self.raw(turn['id']),original)

    async def test_expanded_required_presentation_stops_before_model(self):
        self.enable('minimal_persona')
        turn=await self.turn('你喜欢什么')
        original=persona.render
        def oversized(spec,parts):
            text,diagnostic,included=original(spec,parts)
            return text+'人工必要内容'*5000,diagnostic,included
        with patch.object(self.rt,'_hybrid',side_effect=self.hybrid), \
             patch.object(persona,'render',side_effect=oversized), \
             patch.object(self.model,'complete',new=AsyncMock()) as client:
            await self.engine._run(turn['id'])
        client.assert_not_awaited()
        self.assertEqual(self.engine.get(turn['id'])['error_code'],'companion_persona_input_limit_exceeded')
