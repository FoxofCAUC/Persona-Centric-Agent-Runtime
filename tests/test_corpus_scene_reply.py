"""Source-side identity and numeric eligibility tests; marker text only."""
from copy import deepcopy
import unittest

from experiments.corpus_scene_reply import select_responses, source_response_contexts
from persona_runtime.corpus_files import CorpusError


def message(n, role, fragment='FRAGMENT-ID'):
    return {'message_id': str(n), 'ordinal': n, 'role': role, 'fragment_id': fragment,
            'text': 'TEXT-ID-' + str(n), 'split': 'learn', 'flags': [], 'redactions': []}


def asset(n):
    return {'id': 'ASSET-' + str(n), 'situation': 'SITUATION-ID-' + str(n), 'style': 'STYLE-ID-' + str(n),
            'source_quote': 'TEXT-ID-' + str(n), 'source_binding': {'view_message_id': str(n)}}


class SourceCueTests(unittest.TestCase):
    def test_preceding_other_speaker_is_used_instead_of_own_target(self):
        rows = [message(1, 'A'), message(2, 'B'), message(3, 'B')]
        result = source_response_contexts([asset(3)], rows)['ASSET-3']
        self.assertEqual(result['peer_text'], 'TEXT-ID-1')
        self.assertEqual(result['quote'], 'TEXT-ID-3')
        self.assertNotIn('TEXT-ID-3', result['preceding_context'])
        self.assertTrue(result['self_continuation'])
        self.assertFalse(result['relation_author_verified'])

    def test_later_peer_is_not_used_to_invent_missing_past_context(self):
        result = source_response_contexts([asset(1)], [message(1, 'A'), message(2, 'B')])['ASSET-1']
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'no_visible_preceding_peer')

    def test_context_does_not_cross_fragment_boundaries(self):
        result = source_response_contexts([asset(2)], [message(1, 'A', 'OLD-ID'), message(2, 'B', 'NEW-ID')])['ASSET-2']
        self.assertFalse(result['available'])

    def test_invalid_nearest_peer_is_not_silently_skipped_for_an_older_one(self):
        rows = [message(1, 'A'), message(2, 'A'), message(3, 'B')]
        rows[1]['flags'] = ['MEDIA-ID']
        result = source_response_contexts([asset(3)], rows)['ASSET-3']
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'preceding_peer_unusable')

    def test_missing_or_duplicate_source_identity_is_rejected(self):
        with self.assertRaises(CorpusError):
            source_response_contexts([asset(2)], [message(1, 'A')])
        with self.assertRaises(CorpusError):
            source_response_contexts([asset(1)], [message(1, 'A'), message(1, 'B')])

    def test_source_quote_or_split_cannot_be_replaced(self):
        rows = [message(1, 'A'), message(2, 'B')]
        rows[1]['split'] = 'dev_review'
        with self.assertRaises(CorpusError):
            source_response_contexts([asset(2)], rows)
        rows[1]['split'] = 'learn'
        rows[1]['text'] = 'CHANGED-ID'
        with self.assertRaises(CorpusError):
            source_response_contexts([asset(2)], rows)

    def test_evidence_is_bounded_preserves_order_and_does_not_claim_a_reply_edge(self):
        rows = [message(1, 'A'), *[message(i, 'B') for i in range(2, 9)]]
        result = source_response_contexts([asset(8)], rows)['ASSET-8']
        self.assertEqual(result['context_message_ids'], ['1', '5', '6', '7'])
        self.assertIn('not_proven_reply', result['relation_basis'])


class ResponseGateTests(unittest.TestCase):
    def values(self):
        assets = [asset(1), asset(2)]
        calibration = {a['id']: {'source_context_available': True, 'source_context_supported': True,
            'source_situation_context_score': .9, 'current_input_cutoff': .6, 'understanding_cutoff': .7,
            'relation_author_verified': False} for a in assets}
        scores = {a['id']: {'raw_to_peer': .8, 'understanding_to_context': .9} for a in assets}
        vectors = {a['id']: [1., 0.] for a in assets}
        wrapper = {'prefix': 'PREFIX-ID\n', 'item_format': '{situation}/{style}', 'suffix_if_present': '\n'}
        return assets, calibration, scores, vectors, wrapper

    def test_a_missing_source_peer_cannot_be_overridden_by_high_topic_similarity(self):
        values = list(self.values())
        values[1]['ASSET-1'] = {'source_context_available': False, 'source_reason': 'no_visible_preceding_peer'}
        result = select_responses(*values)
        self.assertNotIn('ASSET-1', result['selected_ids'])

    def test_source_situation_requires_evidence_in_the_preceding_context(self):
        values = list(self.values())
        values[1]['ASSET-1']['source_context_supported'] = False
        result = select_responses(*values)
        self.assertNotIn('ASSET-1', result['selected_ids'])

    def test_the_two_query_views_use_their_own_background_cutoffs(self):
        for field, value in [('raw_to_peer', .6), ('understanding_to_context', .7)]:
            values = list(self.values())
            values[2]['ASSET-1'][field] = value
            self.assertNotIn('ASSET-1', select_responses(*values)['selected_ids'])

    def test_original_style_only_is_projected_and_no_author_approval_is_inferred(self):
        result = select_responses(*self.values())
        self.assertIn('SITUATION-ID', result['rendered_text'])
        self.assertIn('STYLE-ID', result['rendered_text'])
        self.assertNotIn('TEXT-ID', result['rendered_text'])
        self.assertFalse(result['model_use_approved'])
        self.assertFalse(result['semantic_suitability_verified_by_author'])

    def test_all_empty_is_supported_and_capacity_does_not_truncate(self):
        values = list(self.values())
        self.assertEqual(select_responses(*values, character_limit=0)['rendered_text'], '')
        for v in values[2].values():v['raw_to_peer'] = .1
        self.assertEqual(select_responses(*values)['selected_ids'], [])

    def test_deterministic_order_and_finite_scores(self):
        values = list(self.values())
        first = select_responses(*values)
        values[0].reverse()
        self.assertEqual(first['selected_ids'], select_responses(*values)['selected_ids'])
        values[2]['ASSET-1']['raw_to_peer'] = float('nan')
        with self.assertRaises(CorpusError):
            select_responses(*values)

    def test_missing_score_scope_is_not_filled_with_guesses(self):
        values = list(self.values())
        values[2].pop('ASSET-1')
        with self.assertRaises(CorpusError):
            select_responses(*values)


if __name__ == '__main__':
    unittest.main()
