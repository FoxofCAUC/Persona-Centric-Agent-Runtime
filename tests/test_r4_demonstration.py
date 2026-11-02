from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_voice import freeze,render,read,placement,VERSION,DEMONSTRATION_VERSION,RESPONSE_DEMONSTRATIONS,bubble_contract
from persona_runtime.provider_compat import build_request
from scripts import r4_demonstration_preparation as runner
from scripts import r4_expression_realization as common
from tests.r4_demonstration_fixture import fixture,rubric
from tests import test_dialogue_voice as voice_fixture

class Contracts(unittest.TestCase):
    def test_all_42_frozen_legacy_renderings_and_wire_text_remain_exact(self):
        baseline=json.loads((Path(__file__).parent/'fixtures/r4-v2-v7-render-baseline.json').read_text('utf-8'))
        self.assertEqual(len(baseline['profiles']),42)
        for row in baseline['profiles']:
            self.assertEqual(fingerprint(render(row['profile'])),row['normal_sha256'])
            self.assertEqual(fingerprint(render(row['profile'],ablated=True)),row['ablation_sha256'])
        self.assertEqual(fingerprint(bubble_contract()),baseline['format_sha256'])
        self.assertEqual(VERSION,baseline['default'])

    def test_only_demonstrations_added_sampling_boundary_and_placement_same(self):
        a=freeze('same',['人工日常'],version='natural-dialogue-v4');b=freeze('same',['人工日常'],version=DEMONSTRATION_VERSION)
        self.assertEqual(a|{'version':DEMONSTRATION_VERSION},b)
        self.assertEqual(render(b)[0].replace(RESPONSE_DEMONSTRATIONS,'',1),render(a)[0])
        self.assertEqual(render(a,ablated=True)[0],render(b,ablated=True)[0])
        self.assertEqual(placement(a),placement(b))
        m={'name':'grok-4.7','base_url':'https://example.invalid','max_tokens':8192,'timeout':180}
        self.assertEqual(build_request(m|{'_dialogue_profile':a},[]),build_request(m|{'_dialogue_profile':b},[]))
        self.assertEqual(read({'model_snapshot':{'_dialogue_profile':b}}),b)

    def test_unknown_version_fails_before_provider(self):
        p=freeze('valid',['人工'],version=DEMONSTRATION_VERSION)
        with self.assertRaisesRegex(Exception,'dialogue_profile_invalid'):
            build_request({'name':'fake','base_url':'https://example.invalid','max_tokens':1,'_dialogue_profile':p|{'version':'natural-dialogue-v999'}},[])

    def test_same_role_facts_only_six_texts_differ(self):
        d=fixture();a=deepcopy(d['characters']['detail']);b=deepcopy(d['characters']['stance'])
        ae=a.pop('examples');be=b.pop('examples');self.assertEqual(a,b);self.assertEqual(len(ae),6)
        for x,y in zip(ae,be):
            self.assertNotEqual(x.pop('text'),y.pop('text'));self.assertEqual(x,y)
        common.PersonaSpec.from_dict(d['spec'])

    def test_materials_new_and_not_the_instruction_demonstrations(self):
        d=fixture();inputs={m for case in d['cases'] for m in case['members']}
        texts={e['text'] for char in d['characters'].values() for e in char['examples']}
        for n in range(1,8):
            old=common.read(common.OUT/f'real-batch-{n:03}/fixture.json')
            self.assertFalse(inputs&{m for case in old['cases'] for m in case['members']})
            self.assertFalse(texts&{e['text'] for char in old['characters'].values() for e in char['examples']})
        self.assertFalse(any(s in str(d) for s in ('聚餐全员自愿','是报了但还没交钱','页面上的数字突然有了重量')))
        self.assertNotIn('material_forms',str(d));self.assertNotIn('reading_rules',str(d))

    def test_original_gates_kept_and_simulation_not_effect_claim(self):
        r=rubric();old=common.read(common.OUT/'real-batch-007/rubric.json')
        self.assertEqual(r['acceptance'],old['acceptance']);self.assertEqual(r['primary_denominator_per_arm'],18)
        self.assertEqual(r['real_effect_status'],'unverified');self.assertIn('not proof against formal default v2',r['stage_scope'])
        self.assertEqual([c['id'] for c in fixture()['cases'] if c['reference']],['I08'])
        self.assertEqual(len(fixture()['cases']),12)

    def test_no_real_cli_action_or_hidden_enable(self):
        with patch.object(sys,'argv',['r4_demonstration_preparation.py','run']),patch.object(runner,'prepare') as prepare,patch.object(common,'execute') as execute:
            with self.assertRaises(SystemExit) as error:runner.main()
            self.assertEqual(error.exception.code,2);prepare.assert_not_called();execute.assert_not_called()
        self.assertEqual(runner.ARMS,{'control':'natural-dialogue-v4','experiment':'natural-dialogue-v8'})

class Integrated(voice_fixture.Integrated):
    def v8(self):
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('demonstration',['人工'],version=DEMONSTRATION_VERSION);atomic_json(self.config.file,cfg)

    async def test_version_and_actual_input_remain_frozen_after_config_changes(self):
        self.v8();tid=await self.queued('这里是当前真实输入，不是那两段示范。')
        cfg=self.config.load();cfg['model'].pop('_dialogue_profile');atomic_json(self.config.file,cfg)
        p=self.engine.cognitive.prepare(tid);a=p['manifest']['prompt_assembly']
        self.assertEqual(a['effective']['expression'],DEMONSTRATION_VERSION)
        self.assertEqual(p['messages'][-1]['content'],'这里是当前真实输入，不是那两段示范。')
        self.assertEqual(p['messages'][0]['content'].count(RESPONSE_DEMONSTRATIONS),1)
        self.assertEqual(a['input_snapshot_hash'],fingerprint(p['messages']))
        self.assertNotIn('聚餐全员自愿',json.dumps(a,ensure_ascii=False))
        self.assertEqual(self.fake.calls,[])

    async def test_source_withdrawal_rebuild_preserves_version_and_capsule_rules(self):
        head=self.publish();self.admit();self.mode('assist');self.v8();tid=await self.queued()
        p=self.engine.cognitive.prepare(tid);self.assertEqual(p['manifest']['decision_capsule']['renderer'],'decision-capsule-voice-v2')
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-v8')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());f=self.engine.cognitive.finalize(db,turn,p)
        self.assertFalse(f['manifest']['decision_capsule']['applied_to_prompt'])
        self.assertEqual(f['manifest']['prompt_assembly']['effective']['expression'],DEMONSTRATION_VERSION)
        self.assertEqual(f['manifest']['input_snapshot_hash'],fingerprint(f['messages']))
        self.assertEqual(f['messages'][0]['content'].count(RESPONSE_DEMONSTRATIONS),1)

for name in dir(voice_fixture.Integrated):
    if name.startswith('test_') and name not in Integrated.__dict__:setattr(Integrated,name,None)
