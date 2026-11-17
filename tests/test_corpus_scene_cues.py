"""Same-direction cue matching; all fixtures are numeric or identifier text."""
import unittest

from experiments.corpus_scene_cues import select_responses
from persona_runtime.corpus_files import CorpusError


class IncomingCueTests(unittest.TestCase):
    def values(self):
        assets = [{'id': 'ASSET-ID', 'situation': 'SITUATION-ID', 'style': 'STYLE-ID'}]
        calibration = {'ASSET-ID': {'source_context_available': True,
            'source_situation_context_score': .1, 'source_situation_cutoff': .8,
            'source_situation_diagnostic_above_background': False,
            'current_input_cutoff': .6, 'understanding_cutoff': .6, 'relation_author_verified': False}}
        scores = {'ASSET-ID': {'raw_to_peer': .8, 'understanding_to_peer': .9}}
        return assets, calibration, scores, {'ASSET-ID': [1., 0.]}, {'prefix': 'PREFIX-ID', 'item_format': '{situation}/{style}', 'suffix_if_present': '\n'}

    def test_response_act_need_not_be_a_paraphrase_of_preceding_peer_text(self):
        result = select_responses(*self.values())
        self.assertEqual(result['selected_ids'], ['ASSET-ID'])
        self.assertFalse(result['audit'][0]['source_situation_diagnostic_above_background'])

    def test_both_incoming_views_match_the_same_peer_side(self):
        for key in ('raw_to_peer', 'understanding_to_peer'):
            values = list(self.values())
            values[2]['ASSET-ID'][key] = .4
            self.assertEqual(select_responses(*values)['selected_ids'], [])

    def test_missing_real_preceding_peer_has_no_automatic_fallback(self):
        values = list(self.values())
        values[1]['ASSET-ID'] = {'source_context_available': False, 'source_reason': 'no_visible_preceding_peer'}
        self.assertEqual(select_responses(*values)['selected_ids'], [])

    def test_raw_source_and_scores_do_not_become_persona_language(self):
        result = select_responses(*self.values())
        self.assertEqual(result['rendered_text'], 'PREFIX-IDSITUATION-ID/STYLE-ID\n')
        self.assertNotIn('0.8', result['rendered_text'])
        self.assertFalse(result['model_use_approved'])
        self.assertFalse(result['semantic_suitability_verified_by_author'])

    def test_count_and_rendered_capacity_remain_upper_bounds(self):
        self.assertEqual(select_responses(*self.values(), maximum=0)['rendered_text'], '')
        self.assertEqual(select_responses(*self.values(), character_limit=1)['selected_ids'], [])

    def test_model_scores_and_source_scope_are_validated(self):
        values = list(self.values())
        values[2]['ASSET-ID']['raw_to_peer'] = float('nan')
        with self.assertRaises(CorpusError):select_responses(*values)
        values = list(self.values())
        values[2].clear()
        with self.assertRaises(CorpusError):select_responses(*values)


if __name__ == '__main__':unittest.main()
