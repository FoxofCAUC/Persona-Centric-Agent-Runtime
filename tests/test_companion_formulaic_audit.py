"""Artificial-only stage attribution using the real ranker and budget assembler."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from experiments.companion_formulaic_audit import audit_reachability, private_prompt_sources
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_context import manifest, select_context
from persona_runtime.character_embedding import digest
from persona_runtime.character_policy import choose
from persona_runtime.character_retrieval import CharacterCandidateRetriever
from persona_runtime.character_runtime import audit
from persona_runtime.companion import MINIMAL_INSTRUCTION
from persona_runtime.companion_contract import structural_contract
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.profile import snapshot
from persona_runtime.prompt_assembly import PromptBlock, assemble
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def example(item, text='这是人工表达资料', enabled=True):
    return {'id': item, 'scenario': '人工情境', 'keywords': [item + '关键词'],
            'text': text, 'enabled': enabled}


class Evidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME / 'tmp', prefix='formulaic-audit-unit-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(RuntimePaths(Path(self.temp.name))); self.store.initialize()
        service = RuntimeService(self.store)
        self.pid = service.create_persona(generic_persona())['id']; service.use_persona(self.pid)
        self.characters = CharacterService(self.store)
        self.data = empty_character()
        self.data['examples'] = [example('kept', '人' * 100), example('budget', '长' * 490),
            example('low'), example('absent'), example('disabled', enabled=False)]
        self.data['lorebook'] = [{'id': 'hidden', 'title': '隐藏人工背景', 'category': 'place',
            'keywords': ['隐藏'], 'text': '不应进入模型', 'priority': 1, 'enabled': True,
            'visibility': 'creator_only', 'dependencies': []}]
        self.version = self.characters.update(self.pid, self.data,
            self.characters.current(self.pid)['id'], identifier())
        self.policy = choose('hybrid')
        with self.store.connection() as db: profile = snapshot(db, self.pid)
        self.turn = {'id': 'new-artificial-request', 'persona_id': self.pid, 'execution_revision': 1,
            'character_snapshot': json.dumps(self.version), 'profile_snapshot': json.dumps(profile),
            'context_policy_snapshot': json.dumps({'character_retrieval': self.policy})}
        self.engine = SimpleNamespace(store=self.store)
        self.query_count = 0
        self.semantic = {'lore': [], 'example': [self.candidate('kept', .95),
            self.candidate('budget', .9), self.candidate('low', .1)]}

    def candidate(self, item, score):
        row = next(r for r in self.version['data']['examples'] if r['id'] == item)
        return {'kind': 'example', 'item_id': item, 'score': score, 'member_index': 0,
                'source_hash': digest(row), 'chunk_index': 0}

    def selection(self, members=None):
        def query(*args, **kwargs):
            self.query_count += 1
            return {'version_id': self.version['id'], 'model_fingerprint': self.policy['config']['model_fingerprint'],
                    'index_fingerprint': 'artificial-index', 'by_kind': deepcopy(self.semantic)}
        return CharacterCandidateRetriever(self.store, SimpleNamespace(query=query), self.policy['config']).retrieve(
            self.pid, self.version['id'], members or ['外面突然热闹起来了'], example_limit=4)

    def prepared(self, selection, *, final_drop=False):
        parts = [p for p in selection['parts'] if not (final_drop and p['kind'] == 'example')]
        removed = [{'kind': 'example', 'id': 'kept', 'reason': 'input_budget'}] if final_drop else []
        character = manifest(selection, parts, removed)
        retrieval = audit(selection, parts, removed)
        character['retrieval'] = retrieval
        profile = json.loads(self.turn['profile_snapshot'])
        spec = {'format_version': 1, **profile['core']['data'],
                'expression_style': profile['expression']['data']['style']}
        messages, assembly = assemble([
            PromptBlock('persona_expression', MINIMAL_INSTRUCTION + json.dumps(spec, ensure_ascii=False),
                        'companion', 'companion-minimal-v1'),
            PromptBlock('relationship', '', 'profile', 'relationship-v1'),
            PromptBlock('character', '\n'.join(p['text'] for p in parts), 'character_context', 'companion-character-v1'),
            PromptBlock('reply_protocol', structural_contract(), 'reply_batch+companion_contract', 'bubbles-schema3-structural-v1')],
            [], {'role': 'user', 'content': '外面突然热闹起来了'})
        return {'messages': messages, 'character_prepared': selection,
            'details': {'character': character, 'prompt_assembly': assembly},
            'manifest': {'input_snapshot_hash': fingerprint(messages), 'character_retrieval': retrieval,
                         'included_state_refs': [], 'evidence_refs': [], 'excluded_state_refs': [], 'truncation_info': removed}}

    @staticmethod
    def by_id(result):
        return {r['id']: r for r in result['materials']}

    def test_actual_stages_distinguish_threshold_budget_absence_hidden_and_final_cut(self):
        selection = self.selection(); prepared = self.prepared(selection, final_drop=True)
        result = audit_reachability(self.engine, self.turn, prepared)
        rows = self.by_id(result)
        self.assertEqual(self.query_count, 1)  # The audit did not run another query.
        self.assertTrue(rows['kept']['budget_retained']); self.assertFalse(rows['kept']['final_in_model'])
        self.assertEqual(rows['kept']['final_removal_reasons'], ['input_budget'])
        self.assertTrue(rows['budget']['ranked_after_threshold'])
        self.assertEqual(rows['budget']['budget_removal_reasons'], ['character_budget'])
        self.assertEqual(rows['budget']['budget_decision'], 'removed')
        self.assertTrue(rows['low']['raw_semantic_recalled'])
        self.assertEqual(rows['low']['threshold_decision'], 'below_threshold')
        self.assertEqual(rows['low']['budget_decision'], 'not_reached')
        self.assertFalse(rows['absent']['raw_semantic_recalled'])
        self.assertEqual(rows['absent']['threshold_decision'], 'no_candidate')
        self.assertEqual(rows['disabled']['threshold_decision'], 'disabled')
        self.assertEqual(rows['hidden']['threshold_decision'], 'hidden')
        self.assertEqual(result['counts']['configured_examples'], 5)
        self.assertEqual(result['counts']['enabled_examples'], 4)
        self.assertEqual(result['counts']['raw_semantic_examples'], 3)
        self.assertEqual(result['counts']['threshold_retained_examples'], 2)
        self.assertEqual(result['counts']['budget_retained_examples'], 1)
        self.assertEqual(result['counts']['final_included_examples'], 0)
        self.assertFalse(result['historical_inference'])
        self.assertNotIn('这是人工表达资料', json.dumps(result, ensure_ascii=False))

    def test_lexical_support_is_not_mislabeled_as_semantic_threshold_failure(self):
        selection = self.selection(['low关键词'])
        rows = self.by_id(audit_reachability(self.engine, self.turn, self.prepared(selection)))
        self.assertEqual(rows['low']['threshold_decision'], 'lexical_bypass')
        self.assertTrue(rows['low']['ranked_after_threshold'])
        self.assertTrue(rows['low']['final_in_model'])

    def test_fallback_does_not_invent_missing_raw_or_threshold_evidence(self):
        selection = select_context(self.version, 'kept关键词', example_limit=4)
        selection.update(requested_mode='hybrid', effective_mode='lexical', degrade_reason='embedding_unavailable')
        result = audit_reachability(self.engine, self.turn, self.prepared(selection))
        rows = self.by_id(result)
        self.assertIsNone(result['counts']['raw_semantic_examples'])
        self.assertIsNone(rows['kept']['ranked_after_threshold'])
        self.assertEqual(rows['kept']['threshold_decision'], 'not_observed')
        self.assertTrue(rows['kept']['budget_retained']); self.assertTrue(rows['kept']['final_in_model'])
        self.assertEqual(result['fallback_reason'], 'embedding_unavailable')
        self.assertEqual(self.query_count, 0)

    def test_final_manifest_and_request_bindings_are_not_guessed(self):
        prepared = self.prepared(self.selection())
        prepared['manifest']['character_retrieval']['selected']['example'] = []
        with self.assertRaisesRegex(Exception, 'final_manifest_mismatch'):
            audit_reachability(self.engine, self.turn, prepared)
        prepared = self.prepared(self.selection())
        prepared['messages'][-1]['content'] += '篡改'
        with self.assertRaisesRegex(Exception, 'request_mismatch'):
            audit_reachability(self.engine, self.turn, prepared)

    def test_prior_and_final_recheck_selections_remain_separate(self):
        original = self.selection()
        fallback = select_context(self.version, '无关键词', example_limit=4)
        fallback.update(requested_mode='hybrid', effective_mode='lexical', degrade_reason='character_preparation_invalidated')
        result = audit_reachability(self.engine, self.turn, self.prepared(fallback), selection=original)
        row = self.by_id(result)['kept']
        self.assertTrue(row['budget_retained']); self.assertFalse(row['final_in_model'])
        self.assertEqual(result['final_fallback_reason'], 'character_preparation_invalidated')

    def test_full_input_blocks_have_exact_provenance_and_no_automatic_obligation_judgment(self):
        prepared = self.prepared(self.selection())
        value = private_prompt_sources(self.turn, prepared)
        self.assertTrue(value['private_only'])
        self.assertEqual(''.join(r['body'] for r in value['blocks'] if r['role'] == 'system'),
                         prepared['messages'][0]['content'])
        fields = {r['path']: r for r in value['author_fields']}
        self.assertEqual(fields['expression.style']['field_type'], 'author_expression_guidance')
        self.assertEqual(fields['expression.style']['value_hash'], fingerprint(fields['expression.style']['value']))
        self.assertFalse(fields['relationship.description']['parent_block_present'])
        for row in value['blocks'] + value['author_fields']:
            self.assertEqual(row['obligation_review'], {'status': 'not_reviewed', 'findings': []})

    def test_block_mismatch_or_missing_coverage_fails_without_reconstruction(self):
        prepared = self.prepared(self.selection())
        prepared['details']['prompt_assembly']['blocks'][0]['content_hash'] = 'bad'
        with self.assertRaisesRegex(Exception, 'block_binding_mismatch'):
            private_prompt_sources(self.turn, prepared)
        prepared = self.prepared(self.selection())
        prepared['details']['prompt_assembly']['blocks'].pop()
        with self.assertRaisesRegex(Exception, 'block_coverage_mismatch'):
            private_prompt_sources(self.turn, prepared)


if __name__ == '__main__':
    unittest.main()
