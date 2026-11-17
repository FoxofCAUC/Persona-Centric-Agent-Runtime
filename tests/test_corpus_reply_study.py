"""Local source/approval and selection tests; marker inputs never reach an LLM."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from experiments.corpus_reply_study import (adopted_assets, choose_positions,
    development_candidates, export_review, freeze_positions, save_once, select_assets, source_materials)
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint
from tests import test_corpus_coverage as coverage_fixture


def seal(value, field):
    return value | {field: fingerprint(value)}


class ResearchAcceptance(unittest.TestCase):
    def setUp(self):
        coverage_fixture.CoverageFlow.setUp(self)
        proposals = []
        with self.learning.connection() as db:
            for row in db.execute('SELECT * FROM learning_proposals ORDER BY id'):
                proposals.append({'proposal_id': row['id'], 'job_id': row['job_id'],
                                  'data_hash': row['data_hash'], 'content': json.loads(row['data_json'])})
        self.package = seal({'version': 'corpus-expression-research-package-v1',
                             'view_payload_hash': self.view['payload_hash'], 'proposals': proposals}, 'package_hash')
        self.package_path = self.directory / 'package.json'
        save_once(self.package_path, self.package)
        self.approval = {'version': 'corpus-research-asset-acceptance-v1',
                         'package_sha256': digest(self.package_path.read_bytes()),
                         'package_hash': self.package['package_hash'],
                         'accepted_proposal_ids': [p['proposal_id'] for p in proposals],
                         'purpose': 'el4_fixed_context_research', 'formal_publication_authorized': False,
                         'new_prompt_writing_authorized': False}
        self.approval_path = self.directory / 'accepted.json'
        save_once(self.approval_path, self.approval)

    def adopt(self):
        return adopted_assets(self.corpus, self.learning, self.package_path, self.approval_path)

    def test_accepted_snapshot_preserves_provenance_without_formal_publication(self):
        before = self.learning.inspect()
        snapshot = self.adopt()
        self.assertEqual(len(snapshot['assets']), 2)
        self.assertFalse(snapshot['formal_publication'])
        self.assertFalse(snapshot['generation_input_approved'])
        self.assertEqual(self.learning.inspect(), before)
        self.assertEqual(snapshot, self.adopt())

    def test_changed_reviewed_package_rejected(self):
        self.package_path.write_bytes(self.package_path.read_bytes() + b' ')
        with self.assertRaisesRegex(CorpusError, 'acceptance_invalid'):
            self.adopt()

    def test_forged_proposal_cannot_replace_original_ledger(self):
        package = deepcopy(self.package)
        package['proposals'][0]['content']['style'] = 'ID-REPLACED-001'
        package = seal({k: v for k, v in package.items() if k != 'package_hash'}, 'package_hash')
        self.package_path.write_text(json.dumps(package), encoding='utf-8')
        approval = self.approval | {'package_hash': package['package_hash'],
                                    'package_sha256': digest(self.package_path.read_bytes())}
        self.approval_path.write_text(json.dumps(approval), encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'receipt_changed'):
            self.adopt()

    def test_learning_permission_revocation_invalidates_adoption(self):
        with self.learning.connection() as db:
            approval_id = db.execute('SELECT approval_id FROM learning_jobs WHERE id=?', (self.prior_ids[0],)).fetchone()[0]
        self.learning.revoke(approval_id)
        with self.assertRaisesRegex(CorpusError, 'approval_unavailable'):
            self.adopt()

    def test_original_dataset_revocation_invalidates_adoption(self):
        with self.corpus.connection() as db:
            row = db.execute('SELECT dataset_id,dataset_revision FROM corpus_parses WHERE id=?', (self.view['parse_id'],)).fetchone()
        self.corpus.revoke_dataset(row[0], row[1], 'REVOKE-001')
        with self.assertRaises(CorpusError):
            self.adopt()

    def test_accepted_id_subset_or_duplicate_is_not_silently_expanded(self):
        self.approval['accepted_proposal_ids'] *= 2
        self.approval_path.write_text(json.dumps(self.approval), encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'accepted_items_changed'):
            self.adopt()

    def test_development_query_never_returns_learning_or_final_text(self):
        _, candidates, messages = development_candidates(self.corpus, self.view['id'])
        self.assertTrue(messages)
        self.assertTrue(all(m['split'] == 'dev_review' for m in messages))
        self.assertTrue(all(c['message']['split'] == 'dev_review' for c in candidates))

    def test_report_escapes_real_content_and_keeps_pending_state(self):
        snapshot = self.adopt()
        snapshot['assets'][0]['source_quote'] = '<script>MARKER-001</script>'
        root = self.directory / 'review'
        positions = {'positions': []}
        export_review(root, snapshot, positions, {'items': [{'label': 'ID-001', 'source': 'SOURCE-001', 'text': '<input>'}]})
        html = (root / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('&lt;input&gt;', html)
        self.assertIn('尚未批准使用', html)
        with self.assertRaisesRegex(CorpusError, 'conflict'):
            save_once(root / 'assets.private.json', {})

    def upstream(self):
        upstream = self.directory / 'upstream'
        snippets = []
        for name, value in [('src/maisaka/builtin_tool/reply.py', 'SCHEMA-ID-001'),
                             ('prompts/zh-CN/main/reply_private.prompt', 'TEMPLATE-ID-001')]:
            target = upstream / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding='utf-8')
            snippets.append({'path': name, 'kind': 'ID-001', 'text': value})
        evidence = {'version': 'el4-upstream-excerpt-readonly-v1', 'repository': 'Mai-with-u/MaiBot',
                    'commit': '478f671e64467121fd3e39205c8d3507b5ce0bd6', 'excerpts': snippets}
        path = self.directory / 'upstream-evidence.json'
        save_once(path, evidence)
        return upstream, path

    def test_source_catalog_copies_original_cores_and_verbatim_text_without_assembling_request(self):
        upstream, path = self.upstream()
        material = source_materials(Path(__file__).resolve().parents[1], upstream, path)
        self.assertEqual(len(material['items']), 6)
        self.assertFalse(material['generation_input_approved'])
        self.assertEqual(material['assembled_model_requests'], 0)
        self.assertFalse(material['new_prompt_authored'])
        self.assertTrue(all('persona_reply_n0' not in i['source'] for i in material['items']))

    def test_changed_upstream_template_cannot_become_reviewed_source(self):
        upstream, path = self.upstream()
        (upstream / 'prompts/zh-CN/main/reply_private.prompt').write_text('TEMPLATE-CHANGED', encoding='utf-8')
        with self.assertRaisesRegex(CorpusError, 'excerpt_changed'):
            source_materials(Path(__file__).resolve().parents[1], upstream, path)

    def test_reference_excerpt_cannot_hide_appended_instruction(self):
        upstream, path = self.upstream()
        with (upstream / 'prompts/zh-CN/main/reply_private.prompt').open('a', encoding='utf-8') as stream:
            stream.write('\nADDED-ID-001')
        with self.assertRaisesRegex(CorpusError, 'template_changed'):
            source_materials(Path(__file__).resolve().parents[1], upstream, path)

    def test_private_artifacts_cannot_be_written_under_git(self):
        with self.assertRaisesRegex(CorpusError, 'outside_source'):
            save_once(Path(__file__).resolve().parents[1] / 'PRIVATE-TEST-001.json', {})


class ResearchSelection(unittest.TestCase):
    def snapshot(self):
        return seal({'assets': [{'id': f'ASSET-{n:02}', 'situation': 'MARKER001',
                                'style': 'STYLE' + str(n), 'source_quote': 'QUOTE-SECRET-001'} for n in range(8)]}, 'snapshot_hash')

    def test_no_match_returns_empty_instead_of_forced_assets(self):
        result = select_assets(self.snapshot(), '')
        self.assertEqual(result['selected_ids'], [])
        self.assertEqual(result['text'], '')
        self.assertFalse(result['model_use_approved'])

    def test_selection_stable_bounded_and_omits_source_quotes(self):
        snapshot = self.snapshot()
        one = select_assets(snapshot, 'MARKER001')
        other = deepcopy(snapshot)
        other['assets'].reverse()
        other = seal({k: v for k, v in other.items() if k != 'snapshot_hash'}, 'snapshot_hash')
        two = select_assets(other, 'MARKER001')
        self.assertEqual(one['selected_ids'], two['selected_ids'])
        self.assertEqual(len(one['selected_ids']), 5)
        self.assertNotIn('QUOTE-SECRET', one['text'])
        self.assertNotIn('ASSET-', one['text'])

    def test_capacity_includes_separators_and_never_truncates_asset(self):
        snapshot = self.snapshot()
        full = select_assets(snapshot, 'MARKER001', maximum=1)
        result = select_assets(snapshot, 'MARKER001', character_limit=len(full['text']) * 2)
        self.assertEqual(len(result['selected_ids']), 1)
        self.assertEqual(select_assets(snapshot, 'MARKER001', character_limit=0)['text'], '')
        for maximum in (-1, 6, True):
            with self.assertRaises(CorpusError):
                select_assets(snapshot, 'MARKER001', maximum=maximum)

    def test_mutated_snapshot_is_rejected(self):
        snapshot = self.snapshot()
        snapshot['assets'][0]['style'] = 'ID-CHANGED'
        with self.assertRaisesRegex(CorpusError, 'snapshot_changed'):
            select_assets(snapshot, 'MARKER001')

    def candidates(self):
        return [{'group_id': f'GROUP-{n}', 'month': f'MONTH-{n % 5}', 'length_bin': n % 4,
                 'message': {'ordinal': n, 'message_id': f'ID-{n}', 'role': 'A' if n % 2 else 'B'}} for n in range(32)]

    def test_positions_cover_distinct_groups_with_deterministic_structure(self):
        candidates = self.candidates()
        selected = choose_positions(candidates)
        self.assertEqual(selected, choose_positions(list(reversed(candidates))))
        self.assertEqual(len(selected), 16)
        self.assertEqual(len({p['group_id'] for p in selected}), 16)
        self.assertEqual(len({p['month'] for p in selected}), 5)

    def test_fewer_source_groups_does_not_fill_with_fake_or_repeated_positions(self):
        candidates = self.candidates()
        for c in candidates:
            c['group_id'] = 'SAME-001'
        with self.assertRaisesRegex(CorpusError, 'sixteen_distinct'):
            choose_positions(candidates)

    def test_fixed_inputs_preserve_original_and_never_relabel_human_context_as_persona(self):
        candidates = self.candidates()
        for c in candidates:
            n = c['message']['ordinal']
            text = 'ID-' + str(n).zfill(4) + '-MARKER-001'
            c['message'].update(text=text, text_hash=digest(text.encode()), fragment_id='FRAGMENT-' + str(n // 2), kind='text')
        snapshot = self.snapshot()
        snapshot = seal({k: v for k, v in snapshot.items() if k != 'snapshot_hash'} | {'view_payload_hash': '0' * 64}, 'snapshot_hash')
        with patch('experiments.corpus_reply_study.development_candidates', return_value=(
                {'payload_hash': '0' * 64}, candidates, [c['message'] for c in candidates])):
            frozen = freeze_positions(None, 'VIEW-001', snapshot)
        for position in frozen['positions']:
            self.assertEqual(position['persona_history'], [])
            self.assertFalse(position['model_use_approved'])
            self.assertEqual(digest(position['input_text'].encode()), position['input_text_hash'])
        self.assertEqual(sorted(__import__('collections').Counter(p['role'] for p in frozen['positions']).values()), [4] * 4)


if __name__ == '__main__':
    unittest.main()
