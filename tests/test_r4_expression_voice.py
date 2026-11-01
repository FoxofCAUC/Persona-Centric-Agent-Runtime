import json
from pathlib import Path
import unittest
from unittest.mock import patch
from persona_runtime.dialogue_voice import freeze,read,render,bubble_contract,VERSION,EXPRESSION_REALIZATION_VERSION
from persona_runtime.provider_compat import build_request
from persona_runtime.config import atomic_json
from persona_runtime.archive import fingerprint
from tests import test_dialogue_voice as voice_fixture


class VersionContract(unittest.TestCase):
    def test_legacy_frozen_rendering_and_protocol_unchanged(self):
        path=Path('D:/Persona-Centric Agent Runtime/exports/acceptance/M7.6.3-R4-A-expression-realization/legacy-voice-baseline.json')
        old=json.loads(path.read_text(encoding='utf-8'))
        for row in old['profiles']:
            self.assertEqual(list(render(row['profile'])),row['normal'])
            self.assertEqual(list(render(row['profile'],ablated=True)),row['ablation'])
        self.assertEqual(bubble_contract(),old['format_text'])
        self.assertEqual(VERSION,'natural-dialogue-v2')

    def test_only_version_and_conversation_rendering_change(self):
        a=freeze('same',['人工日常材料']);b=freeze('same',['人工日常材料'],version=EXPRESSION_REALIZATION_VERSION)
        self.assertEqual({k:v for k,v in a.items() if k!='version'},{k:v for k,v in b.items() if k!='version'})
        self.assertNotEqual(render(a)[0],render(b)[0])
        self.assertEqual(render(a,ablated=True)[0],render(b,ablated=True)[0])
        model={'name':'grok-4.7','base_url':'https://example.invalid/v1','max_tokens':8192,'timeout':180}
        payloads=[build_request(model|{'_dialogue_profile':v},[{'role':'user','content':'人工'}])[0] for v in (a,b)]
        self.assertEqual(payloads[0],payloads[1])

    def test_unknown_or_malformed_snapshot_fails_before_request(self):
        for version in ('natural-dialogue-v99','',None,{},[]):
            with self.subTest(version=version),self.assertRaisesRegex(Exception,'dialogue_profile_invalid'):
                build_request({'name':'grok-4.7','base_url':'https://example.invalid/v1','max_tokens':8192,
                    '_dialogue_profile':freeze('s',['人工'])|{'version':version}},[])


class Integrated(voice_fixture.Integrated):
    def configure_voice(self,version):
        value=self.config.load();value['model']['_dialogue_profile']=freeze('r4-fixed',['我想找人倾诉'],version=version)
        atomic_json(self.config.file,value)

    async def test_v3_uses_frozen_snapshot_and_correct_assembly_version(self):
        self.configure_voice(EXPRESSION_REALIZATION_VERSION);tid=await self.queued('人工测试')
        self.configure_voice(VERSION)
        prepared=self.engine.cognitive.prepare(tid)
        assembly=prepared['manifest']['prompt_assembly']
        self.assertEqual(assembly['requested']['dialogue_voice'],EXPRESSION_REALIZATION_VERSION)
        self.assertEqual(assembly['effective']['expression'],EXPRESSION_REALIZATION_VERSION)
        self.assertEqual(assembly['input_snapshot_hash'],fingerprint(prepared['messages']))
        self.assertEqual(prepared['messages'][-1]['content'],'人工测试')

    async def test_assist_keeps_existing_capsule_and_withdrawal_rebuild(self):
        head=self.publish();self.admit();self.mode('assist')
        self.configure_voice(EXPRESSION_REALIZATION_VERSION);tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid)
        self.assertIn('这一轮更在意',prepared['messages'][0]['content'])
        self.assertEqual(prepared['manifest']['decision_capsule']['renderer'],'decision-capsule-voice-v2')
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-r4')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertNotIn('这一轮更在意',final['messages'][0]['content'])
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt'])
        self.assertEqual(final['manifest']['prompt_assembly']['effective']['expression'],EXPRESSION_REALIZATION_VERSION)
        self.assertEqual(final['manifest']['input_snapshot_hash'],fingerprint(final['messages']))

for name in dir(voice_fixture.Integrated):
    if name.startswith('test_') and name not in Integrated.__dict__:setattr(Integrated,name,None)
