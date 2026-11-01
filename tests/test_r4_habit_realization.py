from copy import deepcopy
import unittest
from scripts import r4_expression_realization as common
from persona_runtime.dialogue_voice import freeze,render,read,placement,VERSION,HABIT_REALIZATION_VERSION
from persona_runtime.provider_compat import build_request
from persona_runtime.config import atomic_json
from tests.r4_habit_fixture import fixture,rubric
from tests import test_dialogue_voice as voice_fixture


class Contracts(unittest.TestCase):
    def test_all_prior_expression_renderings_unchanged(self):
        for row in common.read(common.OUT/'revision-004-legacy-voice-baseline.json'):
            self.assertEqual(list(render(row['profile'])),row['normal'])
            self.assertEqual(list(render(row['profile'],ablated=True)),row['ablation'])
        self.assertEqual(VERSION,'natural-dialogue-v2')

    def test_only_habit_instruction_changes_same_placement_sampling_hard_rules(self):
        a=freeze('same',['人工'],version='natural-dialogue-v4');b=a|{'version':HABIT_REALIZATION_VERSION}
        self.assertEqual(render(a,ablated=True)[0],render(b,ablated=True)[0])
        old='表达样例让你体会人物怎么关注事情；把这种关注用在眼前，直接作出反应，不向对方介绍自己的回应策略。'
        new='把表达样例当作人物的观察方向。描写习惯的样例，转成当前话题里的具体反应；对白样例，顺着其中的立场和节奏即兴接话。发言从人物此刻想说的那句话开始，回应方法留在幕后。'
        self.assertEqual(render(a)[0].replace(old,new),render(b)[0]);self.assertEqual(placement(a),placement(b))
        model={'name':'grok-4.7','base_url':'https://example.invalid','max_tokens':8192}
        self.assertEqual(build_request(model|{'_dialogue_profile':a},[]),build_request(model|{'_dialogue_profile':b},[]))
        self.assertEqual(read({'model_snapshot':{'_dialogue_profile':b}}),b)

    def test_materials_same_facts_and_frozen_effect_thresholds(self):
        data=fixture();a=deepcopy(data['characters']['detail']);b=deepcopy(data['characters']['stance'])
        self.assertEqual(len(a.pop('examples')),6);b.pop('examples');self.assertEqual(a,b)
        self.assertEqual(len(data['cases']),12);common.PersonaSpec.from_dict(data['spec'])
        self.assertEqual(rubric()['acceptance'],common.read(common.OUT/'real-batch-003/rubric.json')['acceptance'])
        self.assertNotIn('reading_rules',str(data));self.assertEqual(rubric()['policy_version'],'liveliness-priority-v1')


class Integrated(voice_fixture.Integrated):
    def v6(self):
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('habit',['人工'],version=HABIT_REALIZATION_VERSION);atomic_json(self.config.file,cfg)

    async def test_frozen_admission_and_actual_persona_block_version(self):
        self.v6();tid=await self.queued('人工测试')
        cfg=self.config.load();cfg['model'].pop('_dialogue_profile');atomic_json(self.config.file,cfg)
        p=self.engine.cognitive.prepare(tid);assembly=p['manifest']['prompt_assembly']
        self.assertEqual(assembly['effective']['expression'],HABIT_REALIZATION_VERSION)
        self.assertNotIn('expression_realization',[b['name'] for b in assembly['blocks']])
        self.assertEqual(assembly['input_snapshot_hash'],common.fingerprint(p['messages']))

    async def test_withdrawal_keeps_v6_and_original_capsule_contract(self):
        head=self.publish();self.admit();self.mode('assist');self.v6();tid=await self.queued()
        p=self.engine.cognitive.prepare(tid);self.assertEqual(p['manifest']['decision_capsule']['renderer'],'decision-capsule-voice-v2')
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-v6')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());final=self.engine.cognitive.finalize(db,turn,p)
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt'])
        self.assertEqual(final['manifest']['prompt_assembly']['effective']['expression'],HABIT_REALIZATION_VERSION)
        self.assertEqual(final['manifest']['input_snapshot_hash'],common.fingerprint(final['messages']))

for name in dir(voice_fixture.Integrated):
    if name.startswith('test_') and name not in Integrated.__dict__:setattr(Integrated,name,None)
