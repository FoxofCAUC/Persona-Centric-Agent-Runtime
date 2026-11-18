"""Offline receipts and branch-isolated histories. No provider client or operating credential."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.corpus_reply_study import read_private, save_once
from experiments.en3_holdout_packet import ReplayAssembly, first_request
from experiments.en3_holdout_trial import (BRANCHES, FirstReceipts, checksum, perform, run,
                                          use_scope, validate_use)
from persona_runtime.corpus_files import CorpusError
from persona_runtime.provider_compat import build_request


BASE='https://api.deepseek.com'
CREDS={'base_url':BASE,'encrypted_key':'0'}


class Local:
    def __init__(self):
        self.calls=0
        self.drop=False

    def preflight(self,model,wire):
        if model['encrypted_key']!='0':raise AssertionError()

    async def complete(self,model,wire,*,save_raw):
        self.calls+=1
        if self.drop:return
        text='{"selected_ids":[]}' if wire.get('response_format') else str(self.calls)
        name=wire['model']
        if wire.get('stream'):
            packet={'model':name,'choices':[{'index':0,'delta':{'content':text},'finish_reason':'stop'}]}
            value='data: '+json.dumps(packet)+'\n\ndata: [DONE]\n'
        else:
            value=json.dumps({'model':name,'choices':[{'finish_reason':'stop','message':{'content':text}}]})
        save_raw(value.encode(),http_status=200,diagnostics={'elapsed_ms':1})


def pair(model, text, select=False):
    public={'base_url':BASE,'name':model,'max_tokens':512,'max_input_chars':60000,'timeout':100,
            '_reply_mode':'context_full','_json_output':select}
    return public,build_request(public,[{'role':'user','content':text+('JSON' if select else '')}])[0]


class MarkerAssembly:
    def __init__(self):
        self.original=SimpleNamespace(assets={})
        self.histories={}
    async def baseline(self,scene,n,item,history,branch):
        self.histories[(scene,n,branch)]=[row[1] for row in history]
        return pair('deepseek-flash' if branch=='A' else 'deepseek-v4-pro',item['text'])
    def candidate_material(self,scene,n,item,history,baseline):
        return item
    def request(self,kind,material,thought=None,selected_ids=()):
        return pair('deepseek-v4-pro' if kind=='reply' else 'deepseek-flash',
                    (material['text']+str(len(kind))) if thought is None else thought,kind=='selection')
    def parse_selection(self,content,m):
        return json.loads(content)['selected_ids']
    def projection(self,body,m):return {'display_body':body}
    def segments(self,body,m):return [{'start':0,'end':len(body),'delay_ms':0}]


def make_journal(root,client):
    return FirstReceipts(root,'0',client=client,guard=lambda slot:None)


class HoldoutTrial(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_slots_first_raw_and_separate_presented_history(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/'s'
            root.mkdir()
            client,assembly=Local(),MarkerAssembly()
            material={'scenes':[]}
            openings={}
            for scene in ('1','2'):
                turns=[{'turn_number':i,'text':str(i),'accepted_at':str(i)} for i in range(1,7)]
                material['scenes'].append({'id':scene,'turns':turns})
                openings[scene]=[]
                for branch in BRANCHES:
                    stage='understanding' if branch=='R' else 'body'
                    request=assembly.request(stage,turns[0]) if branch=='R' else await assembly.baseline(
                        scene,1,turns[0],[],branch)
                    openings[scene].append(first_request(branch,stage,request))
            contract={'study_id':'0','opening_requests':openings}
            result=await perform(assembly,make_journal(root,client),CREDS,contract,material,'0')
            self.assertEqual(client.calls,60)
            self.assertEqual(result['accounting']['requests'],60)
            self.assertEqual(result['accounting']['unknown_usage_requests'],60)
            for scene in result['scenes']:
                for branch in BRANCHES:
                    first=scene['turns'][0]['bodies'][branch]
                    self.assertEqual(assembly.histories[(scene['id'],2,branch)],[first['body']])
            self.assertEqual(await perform(assembly,make_journal(root,client),CREDS,contract,material,'0'),result)
            self.assertEqual(client.calls,60)

    async def test_missing_callback_stops_later_slots_and_preserves_unknown(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/'s'
            root.mkdir()
            client=Local()
            client.drop=True
            j=make_journal(root,client)
            req=pair('deepseek-v4-pro','0')
            first=await j.send('1','body',req,[],CREDS)
            self.assertEqual(first['state'],'unknown')
            j=make_journal(root,client)
            self.assertEqual((await j.send('1','body',req,[],CREDS))['state'],'unknown')
            self.assertEqual((await j.send('2','body',req,[],CREDS))['state'],'blocked')
            self.assertEqual(client.calls,1)

    async def test_authority_window_ends_before_transport_and_revoked_stage_cannot_send(self):
        state={'locked':False,'allowed':True}
        @contextmanager
        def window():
            self.assertFalse(state['locked'])
            state['locked']=True
            try:yield
            finally:state['locked']=False

        class LocalOnly(Local):
            async def complete(self,model,wire,*,save_raw):
                if state['locked']:raise AssertionError()
                await super().complete(model,wire,save_raw=save_raw)

        def check(slot,stage,model):
            self.assertTrue(state['locked'])
            if not state['allowed']:raise CorpusError('0')
        with TemporaryDirectory() as temp:
            root=Path(temp)/'s'
            root.mkdir()
            client=LocalOnly()
            journal=FirstReceipts(root,'0',client=client,guard=lambda slot:None,authority_window=window,guard_route=check)
            pair_=pair('deepseek-v4-pro','0')
            result=await journal.send('1.B.body','body',pair_,[],CREDS)
            self.assertEqual(result['state'],'completed')
            state['allowed']=False
            with self.assertRaises(CorpusError):
                await journal.send('2.B.body','body',pair_,[],CREDS)
            self.assertEqual(client.calls,1)

    async def test_missing_use_never_loads_credentials(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)/'s'
            root.mkdir()
            contract={'version':'en3-ling-holdout-inputs-v1','root':str(root),'persona':'ID-REVIEWED-PERSONA',
                      'branches':list(BRANCHES),'split':'final_holdout','online_retrieval':False}
            save_once(root/'input-contract.private.json',contract)
            with patch('persona_runtime.config.ConfigStore.load',side_effect=AssertionError), self.assertRaises(CorpusError):
                await run(root)
            self.assertFalse((root/'calls.sqlite3').exists())
            self.assertFalse((root/'candidate-runtime').exists())

    def test_only_specific_approval_can_unlock(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('inputs.html','materials.private.json','blind-key.private.json','bundle.json'):
                (root/name).write_bytes(b'0')
            contract={k:checksum(root/'bundle.json') for k in
                      ('inputs_html_sha256','materials_sha256','blind_key_sha256','source_bundle_sha256')}
            contract.update(study_id='0',persona='ID-REVIEWED-PERSONA',source_bundle_path=str(root/'bundle.json'))
            key=save_once(root/'input-contract.private.json',contract)
            with self.assertRaises(CorpusError):validate_use(root,contract,key)
            use=use_scope(contract,key)|{'author_message':'0'}
            path=root/'author-use.private.json'
            path.write_text(json.dumps(use|{'split':'development'}),encoding='utf8')
            with self.assertRaises(CorpusError):validate_use(root,contract,key)
            path.write_text(json.dumps(use|{'persona':'ID-OTHER-PERSONA'}),encoding='utf8')
            with self.assertRaises(CorpusError):validate_use(root,contract,key)
            path.write_text(json.dumps(use),encoding='utf8')
            self.assertEqual(validate_use(root,contract,key),checksum(path))
            (root/'inputs.html').write_bytes(b'1')
            with self.assertRaises(CorpusError):validate_use(root,contract,key)

    @unittest.skipUnless(os.environ.get('PCAR_EN3_OFFLINE_INPUT_ROOT'),'private holdout packet not supplied')
    async def test_reviewed_private_packet_through_all_positions_with_marker_transport(self):
        packet=Path(os.environ['PCAR_EN3_OFFLINE_INPUT_ROOT'])
        contract=read_private(packet/'input-contract.private.json')
        materials=read_private(packet/'materials.private.json')
        with TemporaryDirectory() as temp:
            scratch=Path(temp)/'s'
            scratch.mkdir()
            client=Local()
            with ReplayAssembly(contract['state_path'],scratch,contract['source_bundle_path']) as assembly:
                result=await perform(assembly,make_journal(scratch,client),CREDS,contract,materials,
                                     checksum(packet/'input-contract.private.json'))
            self.assertEqual(result['accounting']['states'],{'completed':60})
            self.assertTrue(all(entry['state']=='completed' for scene in result['scenes'] for turn in scene['turns']
                                for entry in turn['bodies'].values()))
