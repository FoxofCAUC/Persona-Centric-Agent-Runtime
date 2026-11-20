"""Local review data and scores use opaque identifiers and stay outside the source tree."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from experiments import private_reply_blind as review
from experiments.corpus_reply_study import save_once
from persona_runtime.character_embedding import file_hash
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint


class BlindReview(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.study = {'episodes':[{'id':'C1','kind':'continuous','positions':[
            {'turn_id':str(index),'input':[{'sha256':digest(str(index).encode())}]}
            for index in (123,124)]}]}
        save_once(self.root/review.STUDY,self.study)
        self.order = review.anonymous_order(self.root,self.study)['C1']
        study_hash = file_hash(self.root/review.STUDY)
        controls = {'endpoint':'7','model':'8','temperature':{'present':False}}
        contract = {'study_sha256':study_hash,'recipients':dict.fromkeys(review.ARMS,['7']),
                    'assembly_sha256':{arm:digest(arm.encode()) for arm in review.ARMS},
                    'writer_controls':controls,'starting_context_sha256':{'C1':digest(b'90')}}
        self.contract = contract
        save_once(self.root/review.CONTRACT,contract)
        contract_hash = file_hash(self.root/review.CONTRACT)
        self.approval = {'input_contract_sha256':contract_hash,'decision':'approved'}
        save_once(self.root/review.APPROVAL,self.approval)
        rows = []
        previous = {arm:[] for arm in review.ARMS}
        for source_turn in ('123','124'):
            arms = {}
            for index, arm in enumerate(review.ARMS):
                text = str(600+index)
                bubble = {'phase':'main','text':text}
                arms[arm] = {
                    'writer_controls':deepcopy(controls),'assembly_sha256':contract['assembly_sha256'][arm],
                    'starting_context_sha256':contract['starting_context_sha256']['C1'],
                    'previous_visible_sha256':fingerprint(previous[arm]),'state':'completed',
                    'visible':[bubble|{'receipt_id':source_turn+str(index),'text_sha256':digest(text.encode())}],
                }
                previous[arm].append({'inputs':[source_turn],'visible':[bubble]})
            rows.append({'source_turn':source_turn,'inputs':[source_turn],'arms':arms})
        self.results = {'study_sha256':study_hash,'blind_key_sha256':file_hash(self.root/review.KEY),
                        'input_contract_sha256':contract_hash,'writer_controls':controls,'episodes':{'C1':rows}}
        self.results_path = self.root/'results.private.json'

    def test_preparation_then_render_and_map_bound_scores(self):
        prep = review.render(self.root)
        self.assertEqual(prep.name,'preparation.html')
        self.assertFalse((self.root/'review/index.html').exists())
        save_once(self.results_path,self.results)
        out = review.render(self.root)
        self.assertEqual(out.name,'index.html')
        data = review.make_packet(self.root,self.study,self.results)
        text = out.read_text(encoding='utf-8')
        for arm in review.ARMS:
            self.assertNotIn(arm,text)
        all_labels = dict(zip(review.LABELS,({'naturalness':n,'issues':[]} for n in range(1,5))))
        score = {key:data[key] for key in ('study_sha256','results_sha256')} | {'episodes':{'C1':all_labels}}
        score_path = self.root/'scores.json'
        save_once(score_path,score)
        mapped = json.loads(review.verify_scores(self.root,score_path).read_bytes())
        self.assertEqual(mapped['episodes']['C1'][self.order[2]]['naturalness'],3)
        self.assertEqual(mapped['decision'],'awaiting_author_signoff')
        self.assertEqual(review.anonymous_order(self.root,self.study)['C1'],self.order)

    def test_mismatch_in_transcript_model_and_presentation(self):
        save_once(self.results_path,self.results)
        for field, value, error in (
            ('previous_visible_sha256','1','transcript_chain_changed'),
            ('starting_context_sha256','1','input_assembly_changed'),
            ('writer_controls',{'model':'1'},'writer_controls_changed'),
            ('visible',[{'text':'2','text_sha256':'3','receipt_id':'4','phase':'main'}],'presentation_receipt_invalid'),
        ):
            with self.subTest(field=field):
                modified = deepcopy(self.results)
                modified['episodes']['C1'][1]['arms']['candidate'][field] = value
                with self.assertRaisesRegex(ValueError,error):
                    review.make_packet(self.root,self.study,modified)

    def test_approval_and_score_hash_are_bound(self):
        save_once(self.results_path,self.results)
        wrong = deepcopy(self.results)
        wrong['input_contract_sha256'] = '2'
        with self.assertRaisesRegex(ValueError,'approval_scope_changed'):
            review.make_packet(self.root,self.study,wrong)
        score_path = self.root/'scores.json'
        save_once(score_path,{'study_sha256':'2'})
        with self.assertRaisesRegex(ValueError,'score_version_changed'):
            review.verify_scores(self.root,score_path)
