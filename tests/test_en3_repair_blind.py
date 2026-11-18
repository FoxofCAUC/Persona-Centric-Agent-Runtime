import json
from pathlib import Path
import tempfile
import unittest

from experiments.corpus_reply_study import save_once
from experiments.en3_repair_blind import bound_review, export, freeze, KEY, STUDY
from persona_runtime.corpus_files import CorpusError


class RepairReview(unittest.TestCase):
    def setUp(self):
        work=tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root=Path(work.name)
        turns=[{'turn_number':1,'text':'</script><ID>'}]
        self.m={'scenes':[{'id':f'G{i}','title':f'ID{i}','turns':turns} for i in range(6)]}
        mh=save_once(self.root/'materials.private.json',self.m)
        self.reg={'id':'X00','source_scene_id':'G02','source_results_sha256':'0'*64,'turns':turns}
        self.key=freeze(self.root,self.m,self.reg['source_results_sha256'])
        self.ch=save_once(self.root/'input-contract.private.json',{'study_id':STUDY,'materials_sha256':mh,
                        'blind_key_sha256':self.key,'regression':self.reg})
        def record(group):
            bodies={}
            for b in 'BOR':
                entry={'state':'completed','body':'V','segments':['V'],'understanding':'HIDDEN_MARKER'}
                if group=='X00' and b in 'BO':
                    entry['reused_from']={'study':'en3-ling-01','scene':'G02','index':1,'branch':('B' if b=='B' else 'R')}
                bodies[b]=entry
            return {'id':group,'turns':[{'index':1,'user':turns[0]['text'],'bodies':bodies}]}
        self.r={'version':'en3-ling-repair-results-v2','study_id':STUDY,'input_contract_sha256':self.ch,
                'regression':record('X00')|{'source_scene_id':'G02'},
                'scenes':[record(s['id']) for s in self.m['scenes']], 'accounting':{}}

    def test_balanced_keys_and_regression_bound_before_calls(self):
        key=json.loads((self.root/KEY).read_bytes())
        self.assertEqual(len({tuple(key['scenes'][s['id']]) for s in self.m['scenes']}),6)
        self.assertEqual(freeze(self.root,self.m,'0'*64),self.key)
        with self.assertRaises(CorpusError):freeze(self.root,self.m,'1'*64)
        (self.root/KEY).unlink()
        with self.assertRaises(CorpusError):freeze(self.root,self.m,'0'*64)

    def test_reader_keeps_gaps_separate_and_evidence_private(self):
        self.r['scenes'][0]['turns'][0]['bodies']['R']={'state':'unknown','body':None,'segments':[]}
        save_once(self.root/'results.private.json',self.r)
        view,_=bound_review(self.root)
        self.assertFalse(view['scenes'][0]['turns'][0]['same'])
        self.assertTrue(view['scenes'][-1]['regression'])
        page=export(self.root)
        html=page.read_text(encoding='utf8')
        self.assertNotIn('HIDDEN_MARKER',html)
        self.assertNotIn('</script><ID>',html)
        self.assertIn('HIDDEN_MARKER',page.with_name('evidence.html').read_text(encoding='utf8'))

    def test_wrong_control_or_private_text_rejected(self):
        self.r['regression']['turns'][0]['bodies']['O']['reused_from']['branch']='A'
        save_once(self.root/'results.private.json',self.r)
        with self.assertRaisesRegex(CorpusError,'regression_control_changed'):bound_review(self.root)
        self.r['scenes'][0]['turns'][0]['user']='FOREIGN'
        (self.root/'results.private.json').write_text(json.dumps(self.r),encoding='utf8')
        with self.assertRaisesRegex(CorpusError,'wrong_turn'):bound_review(self.root)
