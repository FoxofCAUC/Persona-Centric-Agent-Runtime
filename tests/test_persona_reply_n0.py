"""只测试已否决候选的阻断及证据完整性，不构造模型语言输入。"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.persona_reply_n0 import bundle as n0
from experiments.persona_reply_n0 import review
from persona_runtime.paths import RuntimePaths


class RejectedCandidate(unittest.TestCase):
    def test_generation_entrypoints_stop_before_reading_or_building_material(self):
        entries=(lambda:n0.build(),lambda:n0.history_text(None),lambda:n0.context_text(None),
                 lambda:n0.understanding_messages(None,None),lambda:n0.replyer_messages(None,None),
                 lambda:n0.make_preview(None,None,None),lambda:n0.supplemental_previews(None))
        with patch.object(n0,'read',side_effect=AssertionError('archive must not be loaded for generation')):
            for index,entry in enumerate(entries):
                with self.subTest(entry=index),self.assertRaisesRegex(ValueError,'n0_candidate_rejected_by_author'):entry()

    def test_render_rejects_before_creating_output(self):
        base=RuntimePaths.from_environment().home/'tmp'
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='n0-revocation-',dir=base) as temp:
            target=Path(temp)/'never-generated'
            with self.assertRaisesRegex(ValueError,'n0_candidate_rejected_by_author'):review.render(None,target)
            self.assertFalse(target.exists())

    def test_old_all_approved_flags_cannot_pass_author_gate(self):
        for metadata in ({},{'overall':'approve_for_n1','sections':[{'choice':'approve'}]*7},
                         {'overall':'approve_for_n1','new_authorization':True,'state':'active'}):
            result=n0.verify_review({},metadata)
            self.assertEqual(result['status'],'rejected_by_author')
            self.assertFalse(result['author_material_gate_passed']);self.assertFalse(result['dispatch_performed'])
            self.assertEqual(result['real_model_requests'],0)

    def test_archived_prompt_and_material_bytes_match_rejection_record(self):
        decision=n0.read(n0.ROOT/'author-decision.json')
        self.assertEqual(decision['status'],'rejected_by_author')
        for name,digest in decision['preserved_language_files'].items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((n0.ROOT/name).read_bytes()).hexdigest(),digest)


if __name__=='__main__':unittest.main()
