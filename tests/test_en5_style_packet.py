"""Exercise the unapproved dispatch boundary and independently check the private fixed-history study."""
import asyncio
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.dialogue_receipt import parse_receipt
from experiments.corpus_reply_study import read_private,save_once
from experiments.en5_style_packet import STUDY,assemble,file_hash,inputs_page
from experiments.en5_style_trial import authorized,blind_key,order,run,scope
from experiments.en5_style_review import bound
from experiments.en5_style_feedback import decode


class TrialBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_different_author_review_never_creates_a_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            contract={'study_id':STUDY,'root':str(root)}
            sha=save_once(root/'input-contract.private.json',contract)
            with patch('experiments.en5_style_trial.assemble',side_effect=AssertionError('unexpected assembly')):
                with self.assertRaisesRegex(RuntimeErrorBase,'approval_required'):
                    await run(root)
            self.assertFalse((root/'calls.sqlite3').exists())

    def test_absolute_grades_ties_and_missing_votes(self):
        meta={'study_id':'M','input_contract_sha256':'I','results_sha256':'R','blind_key_sha256':'K'}
        view=meta|{'cases':[{'position':'1','options':{k:{'state':'completed','body':k} for k in '甲乙'}}]}
        key={'labels':{'1':{'甲':'a','乙':'b'}}}
        contract={'positions':[{'position':'1','arms':{k:{'request_sha256':v} for k,v in [('B','a'),('H','a'),('P','b'),('PH','b')]}}]}
        ballot=meta|{'version':'en5-style-feedback-v1','overall':'','positions':{}}
        self.assertIsNone(decode(ballot,view,key,contract)['candidate_decision'])
        answer={'ratings':{'甲':'service','乙':'help'},'ranks':{},'all_unacceptable':True,'note':'ID'}
        ballot['positions']['1']=answer
        self.assertEqual(decode(ballot,view,key,contract)['positions'][0]['groups'][0]['arms'],['B','H'])
        answer['ranks']={'甲':'1'}
        with self.assertRaisesRegex(RuntimeErrorBase,'rating_required'):decode(ballot,view,key,contract)
        answer.update(ratings={'甲':'accept'},all_unacceptable=False)
        with self.assertRaisesRegex(RuntimeErrorBase,'rating_required'):decode(ballot,view,key,contract)
        answer.update(ratings={'甲':'accept','乙':'uncertain'},ranks={'甲':'1','乙':'1'})
        self.assertEqual([r['rank'] for r in decode(ballot,view,key,contract)['positions'][0]['groups']],['1','1'])
        with self.assertRaisesRegex(RuntimeErrorBase,'ballot_binding'):decode(ballot|{'study_id':'X'},view,key,contract)

    def test_equivalent_arms_schedule_one_position_only(self):
        def arm(k):return {'request_sha256':k}
        c={'positions':[{'position':'1-03','arms':dict(B=arm('1'),P=arm('2'),H=arm('1'),PH=arm('2'))},
                        {'position':'1-06','arms':dict(B=arm('3'),P=arm('4'),H=arm('5'),PH=arm('6'))}],
           'new_requests':['2','4','5','6']}
        schedule=order(c)
        self.assertEqual(len(schedule),4)
        self.assertEqual(set(c['new_requests']),{k for _,k in schedule})


@unittest.skipUnless(os.environ.get('PCAR_STYLE_FACTORS_ROOT'),'private author review not supplied')
class FrozenPacket(unittest.TestCase):
    def test_all_originals_all_arms_and_receipts_when_available(self):
        root=Path(os.environ['PCAR_STYLE_FACTORS_ROOT'])
        contract=read_private(root/'input-contract.private.json')
        frozen,actual=assemble(Path(contract['source']),root)
        actual['preview_sha256']=digest(inputs_page(actual).encode())
        self.assertEqual(actual,contract)
        self.assertEqual(frozen,read_private(root/'original-baselines.private.json'))
        self.assertEqual(len(frozen),30)
        self.assertEqual(len(contract['new_requests']),26)
        self.assertEqual(len(contract['requests']),42)
        self.assertEqual([p['position'] for p in contract['positions'] if p['arms']['H']['removed']],
                         ['1-06','1-11','2-10','2-12','2-13'])
        self.assertFalse(next(p['arms']['H']['removed'] for p in contract['positions'] if p['position']=='2-14'))
        if not (root/'results.private.json').exists():return
        results=read_private(root/'results.private.json')
        view,_,counts=bound(root)
        seen=set()
        with closing(sqlite3.connect((root/'calls.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            for slot,wire,raw,attempt,checksum,status,code in db.execute(
                'SELECT u.slot,r.wire,r.raw,r.attempt_id,r.raw_hash,r.state,r.http FROM uses u JOIN requests r USING(hash)'):
                parsed=json.loads(wire); h=fingerprint(parsed)
                self.assertIn(h,contract['new_requests'])
                self.assertNotIn(h,seen)
                self.assertEqual(parsed,contract['requests'][h])
                seen.add(h)
                result=results['outcomes'][h]
                self.assertEqual(attempt,result['attempt_id'])
                if status=='completed':
                    self.assertEqual(code,200)
                    self.assertEqual(digest(raw),checksum)
                    received=parse_receipt(raw,parsed['stream'])
                    self.assertTrue(received['valid'])
                    self.assertEqual(received['model'],parsed['model'])
                    self.assertEqual(received['body'],result['raw_body'])
                    self.assertEqual(received['usage'],result['usage'])
        self.assertEqual(len(seen),counts['new_physical_requests'])
        self.assertEqual(len(view['cases']),16)
