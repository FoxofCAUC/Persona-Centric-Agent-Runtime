"""Artificial R1 paths; all model completions are deterministic doubles."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hashlib

from tests import test_m754r2 as prior
from tests.expression_selection_fixture import CASES, fixture, character
from tests.m71_fixtures import seed_turn
from persona_runtime.archive import fingerprint
from persona_runtime.character_selector_protocol import candidates, parse_output, prompt_for, PROMPT_VERSION
from persona_runtime.character_retrieval import load_config
from persona_runtime.models import identifier
from persona_runtime.chat import ModelError
from persona_runtime.prompt_assembly import material_selection, bounded, byte_size, fit_manifest
from scripts.expression_selection_r1 import new_policy, Ledger, StopRun, confined, OUT, HOME


class Contracts(unittest.TestCase):
    def test_previous_prompts_exact_and_default_unchanged(self):
        # SHA256s verified against the pre-edit R0 source archive, not refreshed.
        for version,digest in [('character-selector-v1','d63dbfe7709ba88d8c005d4526b9203f1ac5358e4326ec2119ce3abe63247acf'),
                               ('character-selector-v2','77cf21cc58a48dcca8b6e704ba7898a1bbe01628b9cbae05dbf0c229c06b02b5')]:
            self.assertEqual(hashlib.sha256(prompt_for(version).encode()).hexdigest(),digest)
        self.assertEqual(PROMPT_VERSION,'character-selector-v2')
        self.assertNotEqual(prompt_for('character-selector-v3'),prompt_for('character-selector-v2'))

    def test_new_material_only_examples_differ(self):
        data=fixture();a=data['characters']['detail'];b=data['characters']['stance']
        self.assertEqual({k:v for k,v in a.items() if k!='examples'},{k:v for k,v in b.items() if k!='examples'})
        for x,y in zip(a['examples'],b['examples']):self.assertEqual({k:v for k,v in x.items() if k!='text'},{k:v for k,v in y.items() if k!='text'})
        self.assertEqual(len(data['cases']),24);self.assertEqual(len(data['real_case_ids']),8)

    def test_diagnostic_size_and_unknown_distinct_from_empty(self):
        p={'character_retrieval':{'selector_contract':{'prompt':'character-selector-v3'}}}
        details={'character':{'example_ids':['e'],'retrieval':{'effective_mode':'lexical','degrade_reason':'selector_timeout'}}}
        unknown=material_selection(p,details);self.assertIsNone(unknown['returned_example_ids']);self.assertIsNone(unknown['applied_prompt'])
        empty=material_selection(p,details,{'selected':{'example_ids':[]},'status':'completed','prompt_version':'character-selector-v3'})
        self.assertEqual(empty['returned_example_count'],0);self.assertIsNone(empty['applied_prompt'])
        huge={'blocks':[],'material_selection':empty|{'returned_example_ids':['z'*100]*1000},'input_snapshot_hash':'final'}
        result=bounded(huge);self.assertTrue(result['material_selection']['ids_omitted']);self.assertLessEqual(byte_size(result),16384)
        self.assertEqual(fit_manifest({'old':'x'*131060,'prompt_assembly':result}),{'old':'x'*131060})

    def test_global_budget_duplicate_unknown_and_fifth_failure(self):
        with tempfile.TemporaryDirectory(dir=HOME/'tmp') as tmp:
            ledger=Ledger(Path(tmp)/'ledger.json')
            first=ledger.reserve('one',{})
            with self.assertRaises(StopRun):Ledger(ledger.path).check()
            ledger.finish(first,'completed')
            with self.assertRaises(StopRun):ledger.reserve('one',{})
            for i in range(5):
                row=ledger.reserve('failed'+str(i),{});ledger.finish(row,'failed',seconds=1)
            with self.assertRaises(StopRun):ledger.check()
        with tempfile.TemporaryDirectory(dir=HOME/'tmp') as tmp:
            ledger=Ledger(Path(tmp)/'ledger.json')
            for i in range(64):ledger.finish(ledger.reserve(str(i),{}),'completed')
            with self.assertRaises(StopRun):ledger.reserve('65',{})
        with tempfile.TemporaryDirectory(dir=HOME/'tmp') as tmp:
            ledger=Ledger(Path(tmp)/'ledger.json');ledger.finish(ledger.reserve('slow',{}),'failed',seconds=601)
            with self.assertRaises(StopRun):ledger.check()
        with self.assertRaises(StopRun):confined(HOME,OUT)

    def test_ledger_write_failure_is_fatal_and_leaves_no_permission_to_resume(self):
        with tempfile.TemporaryDirectory(dir=HOME/'tmp') as tmp:
            ledger=Ledger(Path(tmp)/'ledger.json')
            from scripts.expression_selection_r1 import atomic_json
            def broken(path,value):
                if path==ledger.path:raise PermissionError('artificial file contention')
                return atomic_json(path,value)
            with patch('scripts.expression_selection_r1.atomic_json',side_effect=broken):
                with self.assertRaisesRegex(StopRun,'ledger_write_failed:PermissionError'):ledger.reserve('never-dispatched',{})
            self.assertTrue((Path(tmp)/'stop.json').exists())
            with self.assertRaisesRegex(StopRun,'experiment_stopped'):Ledger(ledger.path).check()

    def test_accounting_failure_prevents_provider_dispatch(self):
        import asyncio
        from scripts.expression_selection_r1 import CaptureClient
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory(dir=HOME/'tmp') as tmp:
            ledger=Ledger(Path(tmp)/'ledger.json');messages=[{'role':'system','content':'json'}]
            client=CaptureClient(None,{'key':'artificial','selector_messages':messages},Path(tmp),{},ledger,True)
            client.check=ledger.check;client.client.complete=AsyncMock()
            model={'name':'deepseek-flash','base_url':'https://api.deepseek.com','max_tokens':512,'_json_output':True}
            with patch('scripts.expression_selection_r1.atomic_json',side_effect=OSError('injected')):
                with self.assertRaises(StopRun):asyncio.run(client.complete(model,messages))
            client.client.complete.assert_not_called()


class Scenarios(prior.SelectorTests):
    async def test_consistent_payload_wrong_accepted_version_zero_calls(self):
        self.enable()
        with patch.object(self.engine,'_schedule'):new_policy(self.engine,self.pid,'character-selector-v3')
        with patch.object(self.engine,'_schedule'):
            receipt=self.ingress.accept({'text':'书店','request_id':identifier()})
            b=self.ingress.get(receipt['batch_id']);tid=self.ingress.action(b['id'],'seal',b['revision'],identifier(),self.pid)['turn_id']
        from persona_runtime.character_selector_protocol import messages
        pool=self.pool(self.version,{},['书店']);model=self.config.load()['character_selector']
        payload=messages(pool,['书店'],'',4,48000,prompt_version='character-selector-v2')
        with patch.object(self.model,'complete') as call:
            with self.assertRaisesRegex(ValueError,'selector_prompt_snapshot_mismatch'):
                await self.engine.selector._call(self.pid,self.raw(tid),model,payload,pool,4,prompt_version='character-selector-v2')
        call.assert_not_called();self.assertFalse(self.engine.selector.list(self.pid))

    async def exercise(self,case):
        data=character('detail');mutation=case['mutation']
        if mutation=='hidden':
            data['lorebook'][0]['visibility']='creator_only';data['lorebook'][2]['dependencies']=[]
        if mutation=='disabled':data['examples'][0]['enabled']=False
        if mutation=='dependency':data['lorebook'][0]['enabled']=False
        if mutation=='budget':
            for row in data['examples']:row['text']='人工长样例。'*60
        self.version=self.save(data)
        self.config.update({'character_selector':{'enabled':True,'base_url':'http://127.0.0.1:1/v1','name':'selector'}})
        with patch.object(self.engine,'_schedule'):new_policy(self.engine,self.pid,'character-selector-v3')
        if case['reference']:
            with self.store.connection(write=True) as db:seed_turn(db,self.pid,self.sid,'r1-ref',[case['reference']['text']],reply=case['reference']['reply'])
        with patch.object(self.engine,'_schedule'):
            rows=[]
            for index,text in enumerate(case['members']):
                payload={'persona_id':self.pid,'session_id':self.sid,'text':text,'request_id':identifier()}
                if index==0 and case['reference']:payload['reference']={'kind':'turn','id':'r1-ref'}
                rows.append(self.ingress.accept(payload))
            batch=self.ingress.get(rows[0]['batch_id']);tid=self.ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid)['turn_id']
        from persona_runtime.character_semantic_index import sources
        semantic={kind:[row|{'score':.8} for row in sources(self.version) if row['kind']==kind] for kind in ('lore','example')}
        pool=candidates(self.version,case['members'],semantic,load_config())
        if mutation=='hidden':self.assertNotIn('room',pool['payload']['roots']['lore'])
        if mutation=='disabled':self.assertNotIn('small_win',pool['payload']['roots']['example'])
        if mutation=='dependency':self.assertNotIn('festival',pool['payload']['roots']['lore'])
        chosen=[key for key in case['allowed_examples'] if key in pool['payload']['roots']['example']]
        answer={'schema_version':1,'lore_ids':[key for key in case['allowed_lore'] if key in pool['payload']['roots']['lore']], 'example_ids':chosen}
        if mutation=='duplicate':answer['example_ids']=[pool['payload']['roots']['example'][0]]*2
        if mutation=='cross_kind':answer['example_ids']=['room']
        if mutation=='outside':answer['example_ids']=['not-a-candidate']
        calls=[]
        async def provider(model,payload):
            calls.append((model,payload))
            if model['name']=='selector':
                self.assertEqual(payload[0]['content'],prompt_for('character-selector-v3'))
                self.assertEqual([r['text'] for r in json.loads(payload[1]['content'])['members']],case['members'])
                if mutation=='provider_failure':raise ModelError('provider_timeout')
                if mutation=='source_changed':
                    with self.store.connection(write=True) as db:db.execute("UPDATE turns SET character_snapshot='{}' WHERE id=?",(tid,))
                if mutation=='reference_revoked':
                    with self.store.connection(write=True) as db:db.execute('INSERT INTO source_exclusions VALUES (?,?,?,?,?)',('r1-ref-e1',self.pid,'00000000-0000-4000-8000-000000000001','artificial withdrawal','2026-11-01'))
                return json.dumps(answer),{'total_tokens':1}
            from persona_runtime.reply_protocol import response_format
            version=response_format(model['_reply_mode'],model['_expression_snapshot'])['json_schema']['schema']['properties']['schema_version']['enum'][0]
            return json.dumps({'schema_version':version,'behavior_intent':'respond','messages':[{'content':'人工回复','content_kind':'chat','cancel_if_new_input':False}]}),{'total_tokens':1}
        original=self.engine.cognitive.prepare
        def prepare(*args,**kwargs):
            prepared=original(*args,**kwargs)
            if mutation=='final_recheck':prepared['character_prepared']['_guard']='invalidated-artificial-guard'
            return prepared
        with patch.object(self.rt,'local_pool',return_value=pool),patch.object(self.model,'complete',side_effect=provider),patch.object(self.engine.cognitive,'prepare',side_effect=prepare):
            await self.engine._run(tid)
        with self.store.connection() as db:
            manifests=[json.loads(r[0]) for r in db.execute("SELECT manifest_json FROM context_manifests WHERE task_type='dialogue' AND turn_id=?",(tid,))]
        if mutation in ('source_changed','reference_revoked'):
            self.assertEqual(len(calls),1);self.assertNotEqual(self.engine.get(tid)['status'],'completed');return
        if mutation in ('duplicate','cross_kind','outside','provider_failure'):
            self.assertEqual(len(calls),4);self.assertEqual(self.engine.get(tid)['error_code'],'provider_retries_exhausted')
            self.assertEqual(len(manifests),1)
            self.assertEqual(manifests[0]['status'],'blocked_before_call')
            self.assertIsNone(manifests[0]['input_snapshot_hash']);return
        self.assertEqual(self.engine.get(tid)['status'],'completed',self.engine.get(tid).get('error_code'))
        main=manifests[-1];diagnostic=main['prompt_assembly'];selected=diagnostic['material_selection']
        self.assertEqual(diagnostic['input_snapshot_hash'],fingerprint(calls[-1][1]))
        self.assertEqual(selected['included_example_ids'],main['character_retrieval']['selected']['example'])
        self.assertEqual(selected['requested_prompt'],'character-selector-v3')
        self.assertEqual(len(calls),2)
        if mutation in ('duplicate','cross_kind','outside','provider_failure','final_recheck'):
            self.assertIsNone(selected['applied_prompt']);self.assertEqual(selected['effective_mode'],'lexical')
            self.assertTrue(selected['fallback_reason'])
        else:self.assertEqual(selected['applied_prompt'],'character-selector-v3')
        if mutation=='budget':
            self.assertGreater(len(selected['returned_example_ids']),len(selected['included_example_ids']))
        if mutation=='final_recheck':self.assertEqual(selected['returned_example_ids'],chosen)
        if case['id']=='R05':self.assertIn('appearance.height_cm',selected['fact_paths'])
        if case['id']=='R08':self.assertIn(case['reference']['text'],json.loads(calls[0][1][1]['content'])['references'])
        if mutation=='diagnostic_size':
            reduced=bounded(diagnostic|{'blocks':diagnostic['blocks']*1000});self.assertTrue(reduced['details_omitted'])
        if mutation=='desktop_history':
            history=self.engine.cognitive.inspect(self.pid,'desktop',turn_id=tid)
            self.assertTrue(any(m.get('prompt_assembly')==diagnostic for m in history['manifests']))


for name in dir(prior.SelectorTests):
    if name.startswith('test_'):setattr(Scenarios,name,None)
for value in CASES:
    def make(case):
        async def test(self):await self.exercise(case)
        return test
    setattr(Scenarios,'test_'+value['id'],make(value))
