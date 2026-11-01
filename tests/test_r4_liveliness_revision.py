import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import r4_expression_realization as common
from scripts import r4_liveliness_revision as revision
from persona_runtime.dialogue_voice import freeze,render,read,VERSION,LIVELINESS_VERSION
from persona_runtime.provider_compat import build_request
from persona_runtime.config import atomic_json
from tests.r4_liveliness_fixture import fixture,rubric
from tests import test_dialogue_voice as voice_fixture


class Contracts(unittest.TestCase):
    def test_legacy_v2_v3_match_sealed_request_blocks(self):
        root=common.OUT/'real-batch-001'
        for item in common.read(root/'prepared.json')['items']:
            planned=common.read(root/'planned'/(item['key']+'.json'))
            block=next(b for b in planned['manifest']['prompt_assembly']['blocks'] if b['name']=='persona_expression')
            segment=planned['request']['messages'][0]['content'][block['start_character']:block['start_character']+block['characters']]
            self.assertTrue(segment.endswith(render(item['voice'])[0]))
        self.assertEqual(VERSION,'natural-dialogue-v2')
    def test_v4_is_frozen_version_with_same_sampling_and_wire_contract(self):
        a=freeze('fixed',['人工日常输入']);b=freeze('fixed',['人工日常输入'],version=LIVELINESS_VERSION)
        self.assertEqual({k:v for k,v in a.items() if k!='version'},{k:v for k,v in b.items() if k!='version'})
        m={'name':'grok-4.7','base_url':'https://example.invalid','max_tokens':8192}
        self.assertEqual(build_request(m|{'_dialogue_profile':a},[]),build_request(m|{'_dialogue_profile':b},[]))
        self.assertEqual(read({'model_snapshot':{'_dialogue_profile':b}}),b)
        with self.assertRaisesRegex(Exception,'dialogue_profile_invalid'):freeze('s',['x'],version='future')
    def test_new_material_facts_equal_and_acceptance_thresholds_not_lowered(self):
        data=fixture();a=deepcopy(data['characters']['detail']);b=deepcopy(data['characters']['stance']);a.pop('examples');b.pop('examples')
        self.assertEqual(a,b);self.assertEqual(len(data['cases']),12);self.assertTrue(any(c.get('history') for c in data['cases']))
        original=common.read(common.OUT/'real-batch-001/rubric.json')
        self.assertEqual(rubric()['acceptance'],original['acceptance'])
        self.assertEqual(rubric()['policy_version'],'liveliness-priority-v1')
    def test_first_batch_seal_rejects_changed_receipt_prefix(self):
        with tempfile.TemporaryDirectory() as t,patch.object(revision,'OUT',Path(t)):
            root=Path(t);p=root/'real-receipts/001.reserve.json';common.write_once(p,{'request':'original'})
            common.write_once(root/'batch-001-closure.json',{'evidence_hashes':{},'receipt_prefix_hashes':{p.name:common.sha(p)}})
            revision.first_batch_unchanged();p.write_text('{}')
            with self.assertRaises(common.StopRun):revision.first_batch_unchanged()
    def test_wrong_offline_gate_source_stops_before_dispatch(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);gate=root/'gate.json'
            common.write_once(gate,{'passed':True,'execution_sources':{'code':'old'}})
            common.write_once(root/'freeze.json',{'execution_sources':{'code':'new'}})
            with patch.object(common,'protected'),patch.object(common,'verify_freeze',return_value={'real':True}):
                with self.assertRaisesRegex(common.StopRun,'offline_gate_source_mismatch'):asyncio.run(common.execute(root,True,gate_path=gate))
            self.assertFalse((root/'run-started.json').exists())


class Integrated(voice_fixture.Integrated):
    def v4(self):
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('r4-v4',['人工'],version=LIVELINESS_VERSION);atomic_json(self.config.file,cfg)
    async def test_v4_admission_stays_frozen_and_diagnostic_is_actual(self):
        self.v4();tid=await self.queued('普通人工消息')
        cfg=self.config.load();cfg['model'].pop('_dialogue_profile');atomic_json(self.config.file,cfg)
        p=self.engine.cognitive.prepare(tid)
        self.assertEqual(p['manifest']['prompt_assembly']['effective']['expression'],LIVELINESS_VERSION)
        self.assertEqual(p['manifest']['input_snapshot_hash'],common.fingerprint(p['messages']))
    async def test_v4_keeps_existing_assist_capsule_and_withdrawal_behavior(self):
        head=self.publish();self.admit();self.mode('assist');self.v4();tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid)
        self.assertEqual(prepared['manifest']['decision_capsule']['renderer'],'decision-capsule-voice-v2')
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-v4')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone());final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt'])
        self.assertEqual(final['manifest']['prompt_assembly']['effective']['expression'],LIVELINESS_VERSION)

for name in dir(voice_fixture.Integrated):
    if name.startswith('test_') and name not in Integrated.__dict__:setattr(Integrated,name,None)
