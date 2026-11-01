from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import r4_expression_realization as common
from scripts import r4_expression_placement as revision
from persona_runtime.dialogue_voice import freeze,render,placement,VERSION
from persona_runtime.provider_compat import build_request
from persona_runtime.config import atomic_json
from tests.r4_placement_fixture import fixture,rubric
from tests import test_dialogue_voice as voice_fixture


class Contracts(unittest.TestCase):
    def test_identical_wording_and_parameters_only_placement_differs(self):
        for ablated in (False,True):
            for seed in ('a','b','c'):
                a=freeze(seed,['人工'],version='natural-dialogue-v4');b=a|{'version':'natural-dialogue-v5'}
                self.assertEqual(render(a,ablated=ablated)[0],render(b,ablated=ablated)[0])
                self.assertEqual(placement(a),'persona');self.assertEqual(placement(b),'after_character')
                model={'name':'grok-4.7','base_url':'https://example.invalid','max_tokens':8192}
                self.assertEqual(build_request(model|{'_dialogue_profile':a},[]),build_request(model|{'_dialogue_profile':b},[]))
        self.assertEqual(VERSION,'natural-dialogue-v2');self.assertEqual(placement(None),'persona')

    def test_canonical_comparison_preserves_non_voice_content(self):
        a=freeze('same',['人工'],version='natural-dialogue-v4');b=a|{'version':'natural-dialogue-v5'};voice=render(a)[0]
        facts='人物资料';material='材料'
        req=lambda text:{'messages':[{'role':'system','content':text},{'role':'user','content':'输入'}],'temperature':1.1}
        ma={'prompt_assembly':{'blocks':[{'name':'persona_expression','start_character':0,'characters':len(facts+voice)}]}}
        mb={'prompt_assembly':{'blocks':[{'name':'expression_realization','start_character':len(facts+material),'characters':len(voice)}]}}
        x=req(facts+voice+material);y=req(facts+material+voice)
        self.assertEqual(common.voice_removed(x,ma,a),common.voice_removed(y,mb,b))
        changed=deepcopy(y);changed['messages'][-1]['content']='另一输入'
        self.assertNotEqual(common.voice_removed(x,ma,a),common.voice_removed(changed,mb,b))
        broken=deepcopy(y);broken['messages'][0]['content']+='!';mb['prompt_assembly']['blocks'][0]['characters']+=1
        with self.assertRaises(common.StopRun):common.voice_removed(broken,mb,b)

    def test_new_material_and_unchanged_thresholds(self):
        data=fixture();a=deepcopy(data['characters']['detail']);b=deepcopy(data['characters']['stance']);a.pop('examples');b.pop('examples')
        self.assertEqual(a,b);self.assertEqual(len(data['cases']),12)
        self.assertEqual(rubric()['acceptance'],common.read(common.OUT/'real-batch-002/rubric.json')['acceptance'])
        self.assertEqual(rubric()['policy_version'],'liveliness-priority-v1')

    def test_both_prior_seals_protected(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(revision,'OUT',Path(temp)):
            root=Path(temp)
            for index in ('001','002'):
                p=root/f'real-batch-{index}/evidence.json';common.write_once(p,{'original':True})
                common.write_once(root/f'batch-{index}-closure.json',{'evidence_hashes':{'evidence.json':common.sha(p)},'receipt_prefix_hashes':{}})
            revision.prior_batches_unchanged();p.write_text('{}')
            with self.assertRaisesRegex(common.StopRun,'002'):revision.prior_batches_unchanged()


class Integrated(voice_fixture.Integrated):
    def configure_voice(self,version):
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('r4-placement',['人工'],version=version);atomic_json(self.config.file,cfg)

    def verify_placement(self,p):
        assembly=p['manifest']['prompt_assembly'];names=[b['name'] for b in assembly['blocks']]
        self.assertEqual(names.index('expression_realization'),names.index('character')+1)
        self.assertEqual(assembly['effective']['expression'],'natural-dialogue-v5')
        self.assertEqual(p['manifest']['input_snapshot_hash'],common.fingerprint(p['messages']))
        block=next(b for b in assembly['blocks'] if b['name']=='expression_realization')
        text=p['messages'][0]['content'][block['start_character']:block['start_character']+block['characters']]
        self.assertEqual(text,render(freeze('r4-placement',['人工'],version='natural-dialogue-v5'))[0])

    async def test_admitted_v5_frozen_and_exact_location(self):
        self.configure_voice('natural-dialogue-v5');tid=await self.queued('人工消息');self.configure_voice(VERSION)
        self.verify_placement(self.engine.cognitive.prepare(tid))

    async def test_final_withdrawal_rebuild_keeps_placement(self):
        head=self.publish();self.admit();self.mode('assist');self.configure_voice('natural-dialogue-v5');tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid);self.verify_placement(prepared)
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-placement')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt']);self.verify_placement(final)

for name in dir(voice_fixture.Integrated):
    if name.startswith('test_') and name not in Integrated.__dict__:setattr(Integrated,name,None)
