"""Numeric gates and identifier-only fixtures; no invented model dialogue."""
from copy import deepcopy
import unittest

from experiments.corpus_scene_selection import (calibrate, encode_texts, qualify,
    quantile_nearest, select_background, validate_cache)
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint


WRAPPER = {'prefix': 'PREFIX-ID\n', 'item_format': '{situation}/{style}', 'suffix_if_present': '\n\n'}


def rows(count=3):
    return [{'id': str(n), 'situation': 'SITUATION-ID-' + str(n), 'style': 'STYLE-ID-' + str(n),
             'source_quote': 'SOURCE-QUOTE-NOT-PROJECTED'} for n in range(count)]


def scores(assets, raw=.8, understood=.8, supported=True):
    ids = [a['id'] for a in assets]
    calibration = {i: {'background_cutoff': .6, 'source_quote_score': .8, 'source_context_score': .8,
                        'source_supported': supported} for i in ids}
    return calibration, {i: raw for i in ids}, {i: understood for i in ids}, {i: [1., 0.] for i in ids}


class QualificationTests(unittest.TestCase):
    def test_both_current_input_and_actual_understanding_must_pass(self):
        assets = rows()
        for raw, understood in ((.8, .4), (.4, .8), (.6, .8), (.8, .6)):
            result = qualify(assets, *scores(assets, raw, understood), WRAPPER)
            self.assertEqual(result['selected_ids'], [])
        self.assertTrue(qualify(assets, *scores(assets), WRAPPER)['selected_ids'])

    def test_source_evidence_is_a_separate_gate(self):
        assets = rows()
        result = qualify(assets, *scores(assets, supported=False), WRAPPER)
        self.assertEqual(result['selected_ids'], [])
        self.assertTrue(all('source_support_below_background_gate' in a['reasons'] for a in result['audit']))

    def test_no_lexical_overlap_is_required_when_semantic_evidence_passes(self):
        assets = rows(1)
        result = qualify(assets, *scores(assets), WRAPPER)
        self.assertEqual(result['selected_ids'], ['0'])
        self.assertFalse(result['semantic_suitability_verified_by_author'])
        self.assertFalse(result['model_use_approved'])

    def test_an_entire_turn_can_have_no_candidates_without_fallback(self):
        assets = rows()
        result = qualify(assets, *scores(assets, .2, .3), WRAPPER)
        self.assertEqual(result['selected_ids'], [])
        self.assertEqual(result['rendered_text'], '')

    def test_whole_assets_and_separators_are_counted_without_rewriting(self):
        assets = rows(1)
        result = qualify(assets, *scores(assets), WRAPPER)
        expected = 'PREFIX-ID\nSITUATION-ID-0/STYLE-ID-0\n\n'
        self.assertEqual(result['rendered_text'], expected)
        self.assertNotIn('SOURCE-QUOTE-NOT-PROJECTED', result['rendered_text'])
        self.assertEqual(qualify(assets, *scores(assets), WRAPPER, character_limit=len(expected) - 1)['selected_ids'], [])

    def test_capacity_and_scope_fail_closed(self):
        assets = rows()
        for maximum in (-1, 6, True):
            with self.assertRaises(CorpusError):
                qualify(assets, *scores(assets), WRAPPER, maximum=maximum)
        values = list(scores(assets))
        values[2].pop('0')
        with self.assertRaisesRegex(CorpusError, 'scope_invalid'):
            qualify(assets, *values, WRAPPER)
        self.assertEqual(qualify(assets, *scores(assets), WRAPPER, maximum=0)['rendered_text'], '')

    def test_order_is_stable_and_five_is_only_an_upper_bound(self):
        assets = rows(8)
        a = qualify(assets, *scores(assets), WRAPPER)
        b = qualify(list(reversed(assets)), *scores(assets), WRAPPER)
        self.assertEqual(a['selected_ids'], b['selected_ids'])
        self.assertEqual(len(a['selected_ids']), 5)
        values = list(scores(assets))
        for key in values[1]:
            if key != '0':
                values[1][key] = .2
        self.assertEqual(qualify(assets, *values, WRAPPER)['selected_ids'], ['0'])

    def test_nonfinite_semantic_scores_are_rejected(self):
        assets = rows(1)
        values = list(scores(assets))
        values[1]['0'] = float('nan')
        with self.assertRaisesRegex(CorpusError, 'nonfinite'):
            qualify(assets, *values, WRAPPER)


class CalibrationTests(unittest.TestCase):
    def test_nearest_rank_quantile_has_no_query_or_opinion_input(self):
        self.assertEqual(quantile_nearest(list(range(128)), .95), 121)
        for values in ([], [float('nan')], [True]):
            with self.assertRaises(CorpusError):
                quantile_nearest(values, .95)

    def test_learning_background_excludes_dev_holdout_and_source_groups(self):
        messages = [{'message_id': str(n), 'text_hash': str(n), 'group_id': str(n), 'split': 'learn',
                     'kind': 'text', 'target_candidate': True, 'flags': [], 'redactions': [], 'text': 'BACKGROUND-ID-' + str(n)} for n in range(150)]
        messages[0]['split'] = 'dev_review'
        messages[1]['split'] = 'final_holdout'
        messages[2]['flags'] = ['FLAG-ID']
        selected = select_background(messages, {'3'}, 128, 'VIEW-ID')
        self.assertEqual(selected, select_background(list(reversed(messages)), {'3'}, 128, 'VIEW-ID'))
        self.assertEqual(len(selected), 128)
        self.assertFalse({'0', '1', '2', '3'} & {m['message_id'] for m in selected})
        with self.assertRaises(CorpusError):
            select_background(messages[:20], set(), 128, 'VIEW-ID')

    def test_calibration_uses_situation_and_actual_support_only(self):
        assets = [{'id': '0', 'situation': 'S-ID', 'style': 'STYLE-NOT-USED'}]
        sources = {'0': {'quote': 'Q-ID', 'context': 'C-ID'}}
        background = [{'text': 'BG-ID'}]
        vectors = {digest(k.encode()): v for k, v in [('S-ID', [1., 0.]), ('Q-ID', [1., 0.]),
                   ('C-ID', [0., 1.]), ('BG-ID', [0., 1.])]}
        result = calibrate(assets, sources, background, vectors, .95)
        self.assertEqual(result['0']['background_cutoff'], 0.)
        self.assertTrue(result['0']['source_supported'])

    def test_vector_cache_binds_model_inputs_and_data(self):
        cache = {'identity': {'fingerprint': 'MODEL-ID', 'dimension': 2}, 'vectors': {'TEXT-ID': [1., 0.]}}
        cache['data_hash'] = fingerprint(cache)
        validate_cache(cache, {'TEXT-ID'}, 'MODEL-ID', 2)
        for hashes, model in [({'OTHER-ID'}, 'MODEL-ID'), ({'TEXT-ID'}, 'OTHER-MODEL')]:
            with self.assertRaises(CorpusError):
                validate_cache(cache, hashes, model, 2)
        changed = deepcopy(cache)
        changed['vectors']['TEXT-ID'] = [0., 1.]
        with self.assertRaisesRegex(CorpusError, 'cache_changed'):
            validate_cache(changed, {'TEXT-ID'}, 'MODEL-ID', 2)

    def test_chunk_pooling_preserves_offsets_and_avoids_overlap_double_weight(self):
        class Encoder:
            def identity(self):return {'dimension': 2}
            def chunks(self, text):
                return [{'start': 0, 'end': 3, 'text': text[:3], 'token_start': 0, 'token_end': 3},
                        {'start': 2, 'end': len(text), 'text': text[2:], 'token_start': 2, 'token_end': len(text)}]
            def embed_documents(self, chunks):return [[1., 0.], [0., 1.]]
        cache = encode_texts(Encoder(), ['ABCDE'])
        value = cache['vectors'][digest(b'ABCDE')]
        self.assertAlmostEqual(value[0] / value[1], 1.5)
        self.assertEqual(cache['text_count'], 1)


if __name__ == '__main__':
    unittest.main()
