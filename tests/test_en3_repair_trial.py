"""Verify branch isolation, old-receipt reuse and scoped repair-study authorization offline."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.corpus_reply_study import read_private, save_once
from experiments.en3_holdout_packet import first_request
from experiments.en3_holdout_trial import FirstReceipts
from experiments.en3_repair_packet import BRANCHES, STUDY, VERSION, ComparisonAssembly, openings, original_regression
from experiments.en3_repair_trial import perform, run, use_scope, validate_use
from persona_runtime.corpus_files import CorpusError, digest
from tests.test_en3_holdout_trial import CREDS, Local, pair


class Markers:
    def __init__(self):
        self.original=SimpleNamespace(assets={})
        self.history={}
        self.recent={}

    async def baseline(self,scene,n,item,history,branch):
        self.history[(scene,n,branch)]=[r[1] for r in history]
        return pair('deepseek-v4-pro',item['text'])

    def for_turn(self,branch,scene,n,item,history,baseline,recent):
        self.recent[(scene,n,branch)]=[list(v) for v in recent]
        return item

    def engine(self,branch):return self
    def candidate_pool(self,material):return {'eligible_ids':[],'omitted':{}}
    def selectable(self,material):return material.get('select',True)

    def wire(self,branch,stage,material,thought=None,selected_ids=()):
        return pair('deepseek-v4-pro' if stage=='reply' else 'deepseek-flash',
                    material['text'] + branch, stage=='selection')

    def applied(self,branch,raw,material,thought):
        return {'requested_ids':[],'applied_ids':[],'omitted':{}}

    def projection(self,raw,material):return {'display_body':raw}
    def segments(self,raw,material):return [{'start':0,'end':len(raw),'delay_ms':10}]


def mark_scene(sid,n):
    return {'id':sid,'title':sid,'turns':[{'turn_number':i,'text':str(i),'accepted_at':str(i)} for i in range(1,n+1)]}


class RepairTrial(unittest.IsolatedAsyncioTestCase):
    async def test_new_positions_and_preserved_controls(self):
        materials={'scenes':[mark_scene('S',3)]}
        reg=mark_scene('X00',2)
        prior={'turns':[{'index':i,'user':str(i),'bodies':{b:{'state':'completed','body':b+str(i),'segments':[b+str(i)],
                      'stages':[],'planned_display_wait_ms':22} for b in ('B','R')}} for i in (1,2)]}
        with TemporaryDirectory() as d:
            p=Path(d)/'study'; p.mkdir()
            a=Markers()
            contract={'opening_requests':{'S':await openings(a,materials['scenes'][0])},
                      'regression':{'opening_request':(await openings(a,reg))[-1]}}
            client=Local()
            journal=FirstReceipts(p,'0',client=client,guard=lambda slot:None)
            result=await perform(a,journal,CREDS,contract,materials,reg,prior,'0')
            self.assertEqual(client.calls,27)
            self.assertEqual(result['accounting']['states'],{'completed':27})
            for branch in BRANCHES:
                first=result['scenes'][0]['turns'][0]['bodies'][branch]
                self.assertEqual(a.history[('S',2,branch)],[first['body']])
                expected=0 if branch=='R' else 2
                self.assertEqual(result['accounting']['by_branch'][branch]['reused_positions'],expected)
            self.assertEqual(result['regression']['turns'][1]['bodies']['O']['body'],'R2')
            self.assertNotIn(('X00',1,'O'),a.history)
            self.assertEqual(a.recent[('S',3,'R')],[[],[]])
            replay=FirstReceipts(p,'0',client=client,guard=lambda slot:None)
            self.assertEqual(result,await perform(a,replay,CREDS,contract,materials,reg,prior,'0'))
            self.assertEqual(client.calls,27)

    async def test_absent_use_stops_before_credentials_or_runtime(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            contract={'version':VERSION,'study_id':STUDY,'persona':'ID-REVIEWED-PERSONA','root':str(root),'branches':BRANCHES,
                      'split':'final_holdout','online_retrieval':False,'jev_enabled':False}
            save_once(root/'input-contract.private.json',contract)
            with patch('persona_runtime.config.ConfigStore.load',side_effect=AssertionError), self.assertRaises(CorpusError):
                await run(root)
            self.assertFalse((root/'calls.sqlite3').exists())
            self.assertFalse((root/'candidate-runtime').exists())

    def test_specific_approval_binds_both_material_versions(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            files={'materials.private.json':'materials_sha256','blind-key.private.json':'blind_key_sha256',
                   'inputs.html':'inputs_html_sha256','a.json':'source_bundle_sha256','b.json':'repair_bundle_sha256'}
            c={'study_id':STUDY,'persona':'ID-REVIEWED-PERSONA',
               'source_bundle_path':str(root/'a.json'),'repair_bundle_path':str(root/'b.json'),
               'prior_feedback_sha256':'0','regression':{'source_results_sha256':'1'}}
            for filename,field in files.items():
                raw=filename.encode()
                (root/filename).write_bytes(raw)
                c[field]=digest(raw)
            sha=save_once(root/'input-contract.private.json',c)
            use=use_scope(c,sha)|{'author_message':'0'}
            tmp=root/'author-use.private.json'
            tmp.write_text(json.dumps(use|{'version':'en3-ling-author-use-v1'}),encoding='utf8')
            with self.assertRaises(CorpusError):validate_use(root,c,sha)
            tmp.write_text(json.dumps(use|{'persona':'ID-OTHER-PERSONA'}),encoding='utf8')
            with self.assertRaises(CorpusError):validate_use(root,c,sha)
            tmp.write_text(json.dumps(use),encoding='utf8')
            validate_use(root,c,sha)
            (root/'b.json').write_bytes(b'0')
            with self.assertRaises(CorpusError):validate_use(root,c,sha)

    @unittest.skipUnless(os.environ.get('PCAR_EN3_REPAIR_OFFLINE_ROOT'),'private input packet required')
    async def test_full_frozen_material_and_regression_with_local_transport(self):
        packet=Path(os.environ['PCAR_EN3_REPAIR_OFFLINE_ROOT'])
        contract=read_private(packet/'input-contract.private.json')
        state=read_private(contract['state_path'])
        reg,prior,_=original_regression(state)
        materials=read_private(packet/'materials.private.json')
        with TemporaryDirectory() as temp:
            scratch=Path(temp)/'study';scratch.mkdir()
            client=Local()
            with ComparisonAssembly(contract['state_path'],scratch,contract['source_bundle_path'],contract['repair_bundle_path']) as assembly:
                journal=FirstReceipts(scratch,'0',client=client,guard=lambda _:None)
                result=await perform(assembly,journal,CREDS,contract,materials,reg,prior,'0')
            self.assertEqual(result['accounting']['states'],{'completed':contract['request_positions']})
            self.assertTrue(all(v['state']=='completed' for s in [*result['scenes'],result['regression']]
                                for t in s['turns'] for v in t['bodies'].values()))
            self.assertEqual([r['bodies']['O']['raw_body'] for r in result['regression']['turns']],
                             [r['bodies']['R']['raw_body'] for r in prior['turns']])
