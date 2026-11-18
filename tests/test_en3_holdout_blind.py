"""Blind assignment and reader output use only local marker data."""
import json
from pathlib import Path
import tempfile
import unittest

from experiments.corpus_reply_study import save_once
from experiments.en3_holdout_blind import assemble_review, export, freeze, KEY
from persona_runtime.corpus_files import CorpusError, digest


class HoldoutBlind(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.material = {'scenes':[{'id':sid,'title':sid,
            'turns':[{'text':f'{sid}/{i}'} for i in range(1,7)]} for sid in ('I1','I2')]}
        self.material['scenes'][0]['turns'][0]['text']='</script><ID-X>'
        source = save_once(self.root/'materials.private.json',self.material)
        key = freeze(self.root,self.material)
        self.contract = {'study_id':'ID','materials_sha256':source,'blind_key_sha256':key}
        self.contract_hash = save_once(self.root/'input-contract.private.json',self.contract)
        self.results = {'version':'en3-ling-results-v1','study_id':'ID','input_contract_sha256':self.contract_hash,
                        'accounting':{'actual_charge':None},'scenes':[]}
        for scene in self.material['scenes']:
            rows = []
            for i, item in enumerate(scene['turns'],1):
                entries = {b:{'state':'completed','body':'X0','segments':['X','0'],
                    'understanding':'ID-INTERNAL','stages':[]} for b in 'ABR'}
                rows.append({'index':i,'user':item['text'],'bodies':entries})
            self.results['scenes'].append({'id':scene['id'],'turns':rows})

    def test_key_is_bound_before_results_and_cannot_be_regenerated(self):
        key = self.root/KEY
        original = json.loads(key.read_bytes())
        self.assertEqual(freeze(self.root,self.material),digest(key.read_bytes()))
        self.assertTrue(all(a!=b for a,b in zip(*original['scenes'].values())))
        self.material['scenes'][0]['turns'][0]['text']='ID-CHANGED'
        with self.assertRaises(CorpusError):freeze(self.root,self.material)
        key.unlink()
        with self.assertRaises(CorpusError):freeze(self.root,self.material)

    def test_anonymous_page_excludes_internal_material_and_keeps_missing_results(self):
        self.results['scenes'][0]['turns'][1]['bodies']['B']={'state':'failed','body':None,'segments':[]}
        save_once(self.root/'results.private.json',self.results)
        path=export(self.root)
        rendered=path.read_text(encoding='utf-8')
        self.assertNotIn('ID-INTERNAL',rendered)
        self.assertNotIn('</script><ID-X>',rendered)
        self.assertIn('ID-INTERNAL',path.with_name('evidence.html').read_text(encoding='utf-8'))
        review,_=assemble_review(self.root)
        self.assertTrue(review['scenes'][0]['turns'][0]['identical'])
        self.assertIn('failed',{v['state'] for v in review['scenes'][0]['turns'][1]['options'].values()})

    def test_rejects_mismatched_display_and_provenance(self):
        path=self.root/'results.private.json'
        self.results['scenes'][0]['turns'][0]['bodies']['R']['segments']=['!']
        save_once(path,self.results)
        with self.assertRaisesRegex(CorpusError,'presentation_changed'):assemble_review(self.root)
        self.results['input_contract_sha256']='ID-FOREIGN'
        path.write_text(json.dumps(self.results),encoding='utf-8')
        with self.assertRaisesRegex(CorpusError,'results_not_bound'):assemble_review(self.root)
