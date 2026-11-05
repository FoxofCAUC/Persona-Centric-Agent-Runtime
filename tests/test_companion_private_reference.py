"""Artificial, offline checks for the private reference comparison adapter."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_comparison as comparison
from experiments import companion_private_reference as reference
from persona_runtime.archive import fingerprint
from persona_runtime.companion import GENERATION_PROFILE, MINIMAL_INSTRUCTION, choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import generic_persona
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import provider_identity
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import expression_snapshot, prompt_contract
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def artificial_request(base_model=None):
    model = deepcopy(base_model or {'base_url': 'http://127.0.0.1:9/v1', 'name': 'grok-4.7',
        'max_tokens': 100, 'timeout': 60, 'max_input_chars': 24000, 'hourly_limit': 0})
    model.update(_companion_profile=choose('minimal'), _generation_profile=GENERATION_PROFILE,
        _reply_mode='natural_structured', _expression_snapshot=expression_snapshot('natural_structured',version=3))
    material = '{"name":"人工人物"}\n关系说明：普通朋友\n人物补充资料：\nLORE正文\nEXAMPLE正文\n已有记忆：["人工记忆"]'
    messages = [{'role':'system','content': MINIMAL_INSTRUCTION + material +
        prompt_contract(model['_reply_mode'],model['_expression_snapshot'])},
        {'role':'user','content':'人工前文'}, {'role':'assistant','content':'之前的人工接话'},
        {'role':'user','content':'人工新输入'}]
    wire,_=build_request(model,messages)
    return {'messages':messages,'wire':wire,'model':comparison.public_model(model),
        'messages_hash':fingerprint(messages),'wire_hash':fingerprint(wire)}


class Boundaries(unittest.TestCase):
    def test_material_keeps_selected_examples_lore_memory_and_exact_history(self):
        request=artificial_request()
        value=reference.split_minimal_request(request)
        self.assertIn('EXAMPLE正文',value['persona'])
        self.assertIn('LORE正文',value['persona'])
        self.assertIn('人工记忆',value['persona'])
        self.assertEqual(value['history'],request['messages'][1:-1])
        self.assertEqual(value['user'],request['messages'][-1]['content'])
        self.assertNotIn(MINIMAL_INSTRUCTION,value['persona'])

    def test_changed_boundaries_and_fingerprints_fail_closed(self):
        for mutation,code in [
            (lambda r:r['messages'][0].update(content='wrong'+r['messages'][0]['content']), 'reference_minimal_boundaries_mismatch'),
            (lambda r:r['messages'][0].update(content=r['messages'][0]['content']+'tail'), 'reference_minimal_boundaries_mismatch'),
        ]:
            request=artificial_request();mutation(request)
            request['messages_hash']=fingerprint(request['messages'])
            request['wire_hash']=fingerprint(build_request(request['model'],request['messages'])[0])
            with self.assertRaisesRegex(Exception,code):reference.split_minimal_request(request)
        request=artificial_request();request['messages'][-1]['content']='modified'
        with self.assertRaisesRegex(Exception,'reference_source_messages_changed'):
            reference.split_minimal_request(request)

    def test_common_decoder_displays_text_and_preserves_upstream_shape(self):
        request=artificial_request()
        raw=json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':'第一句','content_kind':'chat','cancel_if_new_input':False},
            {'content':'第二句','content_kind':'chat','cancel_if_new_input':False}]},ensure_ascii=False)
        upstream={'captured_messages':[[{'type':'Plain','text':raw}]]}
        before=deepcopy(upstream)
        view=reference.review_display(upstream,request['model'])
        self.assertEqual(view['display'],'第一句\n\n第二句')
        self.assertEqual(view['bubbles'],['第一句','第二句'])
        self.assertEqual(upstream,before)
        self.assertTrue(view['native_bubble_segmentation_not_compared'])

    def test_restore_model_only_restores_credential_for_matching_public_parameters(self):
        original={'name':'grok-4.7','encrypted_key':'artificial-encrypted-placeholder'}
        self.assertEqual(reference.restore_model({'name':'grok-4.7'},original,comparison.public_model),original)
        with self.assertRaisesRegex(Exception,'reference_public_model_mismatch'):
            reference.restore_model({'name':'different'},original,comparison.public_model)
        with self.assertRaisesRegex(Exception,'reference_public_model_contains_credential'):
            reference.restore_model(original,original,comparison.public_model)


class OfflineFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=DEFAULT_HOME/'tmp',prefix='reference-compare-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'comparison';self.root.mkdir()
        self.public_reference=Path(self.temp.name)/'public-reference';self.public_reference.mkdir()
        home=Path(self.temp.name)/'home';self.paths=RuntimePaths(home)
        store=Store(self.paths);store.initialize()
        service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'http://127.0.0.1:9/v1','name':'grok-4.7','hourly_limit':0}})
        model=self.config.load()['model']
        atomic_json(home/'runtime/private-copy-policy.json',{'persona_id':pid,
            'approved_models':{'main':provider_identity(model)}})
        self.request=artificial_request(model)
        paired=self.root/'positions/p04-minimal';paired.mkdir(parents=True)
        atomic_json(paired/'request.json',self.request)
        atomic_json(paired/'result.json',{'status':'completed','kind':'simulated','display':'人工对照正文'})
        self.row={'id':'p04-reference','pair_id':'p04','mode':'reference','home':str(home),'persona_id':pid,
            'paired_pcar_position':'p04-minimal'}
        self.plan={'development_material':True,'source':{'home':str(home)},
            'positions':[self.row,{'id':'p04-minimal','pair_id':'p04','mode':'minimal'}],
            'pairs':[{'id':'p04','inputs':['人工输入'],'positions':['p04-minimal','p04-reference'],
                'anonymous':{'A':'p04-minimal','B':'p04-reference'}}]}
        atomic_json(self.root/'state.json',{'status':'prepared'})
        self.source={'source_ready':True,'text_patches_applied':True,'version':'artificial-fixed-source'}
        self.load=self.enterContext(patch.object(comparison,'load_plan',return_value=self.plan))
        self.verify=self.enterContext(patch.object(comparison,'verify_frozen'))
        self.enterContext(patch.object(reference,'source_status',return_value=self.source))
        self.enterContext(patch('socket.socket.connect',side_effect=AssertionError('offline_network_blocked')))

    async def upstream(self,root,*,persona,user,history,complete,output_contract,runtime_name):
        self.assertIn('LORE正文',persona);self.assertIn('EXAMPLE正文',persona)
        messages=[{'role':'system','content':'# Persona Instructions\n'+persona+output_contract}]+history+[{'role':'user','content':user}]
        raw=await complete(messages)
        return {'messages':messages,'raw_text':raw,'after_response_hook':raw,'processed_text':raw,
            'captured_messages':[[{'type':'Plain','text':raw}]],'provider_dispatches':1}

    async def test_fake_flow_reserves_registers_runtime_and_decodes_report(self):
        with patch.object(reference,'run_reference',side_effect=self.upstream):
            result=await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['physical_calls'],0)
        self.assertEqual(result['simulated_calls'],1)
        self.assertEqual(result['display'],'独立人工参考夹具')
        cleanup=json.loads((self.root/'reference-cleanup.json').read_text('utf-8'))
        self.assertEqual(len(cleanup['paths']),1)
        self.assertEqual(Path(cleanup['paths'][0]).parent,self.public_reference)
        output=self.root/'positions/p04-reference'
        self.assertTrue((output/'started.json').is_file())
        self.assertEqual(len(list(output.glob('*.reserve.json'))),1)
        self.assertTrue((output/'generation-result.json').is_file())
        saved=json.loads((output/'request.json').read_text('utf-8'))
        self.assertNotIn('encrypted_key',saved['model'])
        # Report only accepts real results; fake text never masquerades as real.
        report=comparison.report(self.root).read_text('utf-8')
        self.assertIn('未完成真实生成',report)
        self.assertNotIn('独立人工参考夹具',report)
        with patch.object(reference,'run_reference',side_effect=AssertionError('must_not_replay')):
            with self.assertRaises(FileExistsError):
                await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)

    async def test_changed_history_is_rejected_before_any_generation(self):
        async def bad_history(root,*,persona,user,history,complete,output_contract,runtime_name):
            return await complete([{'role':'system','content':persona+output_contract},{'role':'user','content':'changed'}])
        with patch.object(reference,'run_reference',side_effect=bad_history):
            with self.assertRaisesRegex(Exception,'reference_shared_materials_changed'):
                await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)
        result=json.loads((self.root/'positions/p04-reference/result.json').read_text('utf-8'))
        self.assertEqual(result['generation_attempts'],0)
        self.assertEqual(json.loads((self.root/'state.json').read_text('utf-8'))['status'],'stopped')

    async def test_post_hook_protocol_failure_keeps_completed_generation_and_does_not_retry(self):
        async def bad_hook(*args,**kwargs):
            result=await self.upstream(*args,**kwargs)
            result['captured_messages']=[[{'type':'Plain','text':'hook destroyed JSON'}]]
            return result
        with patch.object(reference,'run_reference',side_effect=bad_hook):
            with self.assertRaises(Exception):
                await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)
        output=self.root/'positions/p04-reference'
        result=json.loads((output/'result.json').read_text('utf-8'))
        audit=json.loads((output/'audit.json').read_text('utf-8'))
        self.assertEqual(result['generation_attempts'],1)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(audit['generation_retry']['attempts'][0]['status'],'completed')
        self.assertTrue((output/'generation-result.json').is_file())

    async def test_fake_material_cannot_be_used_as_real(self):
        with self.assertRaisesRegex(Exception,'comparison_simulation_scope_mismatch'):
            await reference.run_reference_position(self.root,'p04-reference',reference_root=self.public_reference)

    async def test_runtime_registration_failure_has_terminal_and_stops_batch(self):
        with patch.object(reference,'register_runtime',side_effect=OSError('artificial_write_failure')):
            with self.assertRaises(OSError):
                await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)
        output=self.root/'positions/p04-reference'
        self.assertTrue((output/'started.json').is_file())
        terminal=json.loads((output/'terminal.json').read_text('utf-8'))
        self.assertEqual(terminal['status'],'failed')
        self.assertEqual(terminal['physical_calls'],0)
        self.assertEqual(json.loads((self.root/'state.json').read_text('utf-8'))['status'],'stopped')

    async def test_an_unfinished_other_position_blocks_reference_without_dispatch(self):
        atomic_json(self.root/'positions/p01-current/started.json',{'kind':'simulated'})
        with patch.object(reference,'run_reference',side_effect=AssertionError('must_not_dispatch')):
            with self.assertRaisesRegex(Exception,'comparison_unfinished_position_no_auto_resume'):
                await reference.run_reference_position(self.root,'p04-reference',fake=True,reference_root=self.public_reference)
        self.assertFalse((self.root/'positions/p04-reference/started.json').exists())


class RealUpstreamOffline(unittest.TestCase):
    def test_nonempty_history_and_common_schema3_through_real_upstream(self):
        value=os.environ.get('PCAR_REFERENCE_ROOT')
        if not value:
            self.skipTest('set PCAR_REFERENCE_ROOT for the isolated upstream offline integration')
        root=Path(value).resolve()
        python=root/'.venv/Scripts/python.exe'
        if not python.is_file():
            self.skipTest('isolated reference Python unavailable')
        with tempfile.TemporaryDirectory(dir=root,prefix='artificial-prefix-integration-') as folder:
            owned=Path(folder).resolve()
            runtime=owned/'runtime'
            # Register before allowing upstream writes; this scope has no private inputs.
            atomic_json(owned/'cleanup.json',{'kind':'artificial_only','paths':[str(runtime)]})
            output=owned/'result.json'
            script='''
import asyncio,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,REPO)
from experiments.companion_reference import run_reference
from experiments.companion_private_reference import split_minimal_request,review_display,FakeClient
from tests.test_companion_private_reference import artificial_request
from persona_runtime.config import atomic_json
request=artificial_request(); common=split_minimal_request(request)
async def complete(messages):
    if messages[1:]!=request['messages'][1:]: raise RuntimeError('history_changed_before_dispatch')
    result=await FakeClient().complete(request['model'],messages)
    return result[0]
async def invoke():
    with patch('socket.socket.connect',side_effect=RuntimeError('offline_network_blocked')):
        result=await run_reference(Path(REFERENCE),persona=common['persona'],user=common['user'],
            history=common['history'],complete=complete,output_contract=common['output_contract'],runtime_name=RUNTIME)
    display=review_display(result,request['model'])
    atomic_json(Path(OUTPUT),{'history_unchanged':result['messages'][1:]==request['messages'][1:],
        'provider_dispatches':result['provider_dispatches'],'decoded':display['display']=='独立人工参考夹具',
        'kind':'simulated','physical_calls':0,'stage':result['stages'][-1],
        'native_bubble_segmentation_not_compared':display['native_bubble_segmentation_not_compared']})
asyncio.run(invoke())
'''
            bindings={'REPO':str(Path(__file__).resolve().parents[1]),'REFERENCE':str(root),
                'RUNTIME':str(runtime.relative_to(root)),'OUTPUT':str(output)}
            code='\n'.join(key+'='+repr(val) for key,val in bindings.items())+'\n'+script
            env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1',
                'PYTHONWARNINGS':'ignore::SyntaxWarning','TEMP':str(DEFAULT_HOME/'tmp'),'TMP':str(DEFAULT_HOME/'tmp')}
            run=subprocess.run([str(python),'-B','-c',code],env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)
            self.assertEqual(run.returncode,0,run.stderr)
            evidence=json.loads(output.read_text('utf-8'))
            self.assertTrue(evidence['history_unchanged'])
            self.assertTrue(evidence['decoded'])
            self.assertEqual(evidence['provider_dispatches'],1)
            self.assertEqual(evidence['stage'],'respond_capture')
            self.assertEqual(evidence['physical_calls'],0)
            self.assertEqual(evidence['kind'],'simulated')


if __name__=='__main__':
    unittest.main()
