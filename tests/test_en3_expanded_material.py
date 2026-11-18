"""Check immutable holdout exclusions and targeted duplicate screening with local markers."""
import os
from pathlib import Path
import unittest

from experiments.corpus_reply_study import read_private
from experiments.en3_expanded_material import check_short_near_duplicates, exclude_reviewed_holdout, verify_materials
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_store import fingerprint


def scene(group, texts):
    return {'group_id':group,'turns':[{'bubbles':[{'text':value}]} for value in texts]}


class ExpandedMaterial(unittest.TestCase):
    def test_previous_holdout_excludes_groups_and_normalized_bubbles(self):
        scenes = [scene('Gx',['MK-1','MK-2']),scene('Gy',['MK-3'])]
        prior = {'split':'final_holdout','scenes':scenes}
        prior['materials_hash'] = fingerprint(prior)
        groups, used = {'G0'}, {'mk0'}
        exclude_reviewed_holdout(groups, used, prior)
        self.assertEqual(groups, {'G0','Gx','Gy'})
        self.assertEqual(used, {'mk0','mk1','mk2','mk3'})
        with self.assertRaisesRegex(CorpusError, 'group_overlap'):
            exclude_reviewed_holdout({'Gy'}, set(), prior)
        prior['scenes'][0]['group_id'] = 'Gz'
        with self.assertRaisesRegex(CorpusError, 'changed'):
            exclude_reviewed_holdout(set(), set(), prior)

    def test_near_overlap_checked_inside_new_window_and_across_previous_inputs(self):
        sample = scene('G',['01234567890','0123456789X'])
        with self.assertRaisesRegex(CorpusError, 'near_duplicate_input'):
            check_short_near_duplicates(sample,set())
        with self.assertRaisesRegex(CorpusError, 'near_duplicate_input'):
            check_short_near_duplicates(scene('G',['ABC-DEFG-HIJKL']),{'abcdefghijkz'})
        check_short_near_duplicates(scene('G',['0','034','9876543210']),{'01','035','abcdefghij'})

    @unittest.skipUnless(os.environ.get('PCAR_EN3_REPAIR_INPUT_ROOT') and os.environ.get('PCAR_DIALOGUE_REPLAY_STATE'),
                         'private holdout material required')
    def test_rebuild_expansion_and_detect_source_material_change(self):
        root=Path(os.environ['PCAR_EN3_REPAIR_INPUT_ROOT'])
        state=read_private(Path(os.environ['PCAR_DIALOGUE_REPLAY_STATE']))
        old=read_private(Path(state['study_root'])/'plan.private.json')
        fresh=read_private(Path(state['fresh_scene_study_root'])/'materials.private.json')
        prior=read_private(Path(state['en3_ling_input_contract']).parent/'materials.private.json')
        material=read_private(root/'materials.private.json')
        corpus=DatasetCorpusStore(old['corpus_root'])
        result=verify_materials(corpus,material,old,fresh,prior)
        self.assertEqual((result['scenes'],result['turns']), (6,36))
        self.assertEqual(material['excluded_prior_groups'],41)
        material['scenes'][0]['turns'][0]['text_hash'] = 'changed'
        with self.assertRaisesRegex(CorpusError, 'expanded_material_changed'):
            verify_materials(corpus,material,old,fresh,prior)


if __name__ == '__main__':
    unittest.main()
