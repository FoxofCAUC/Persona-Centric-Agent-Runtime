"""Offline tests for provenance, authored judgments and fixed review handling."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import dialogue_communication_continuation_analysis as a
from persona_runtime.archive import fingerprint


def raw(text):
    return json.dumps({'messages': [{'content': text}]}, ensure_ascii=False)


def result(ident, text='接着聊。', projection='', status='completed', canonical_id=None):
    group, condition = ident.rsplit('-', 1)
    messages = [{'role': 'system', 'content': '原提示' + projection}, {'role': 'user', 'content': '先别建议，随便聊聊。'}]
    value = {'id': ident, 'group': group, 'condition': condition, 'real': True, 'simulation': False,
             'status': status, 'request_hash': fingerprint(messages), 'messages': messages, 'reply': raw(text),
             'calls': ['0001'], 'manifest': {'input_snapshot_hash': fingerprint(messages)},
             'view': {'projection': projection, 'character': {'example_ids': ['e1']},
                      'shared_evidence_hash': 'same-evidence', 'mother_spec_hash': 'same-mother'}}
    if canonical_id:
        value.update(canonical_id=canonical_id, actual_manifest_from=canonical_id)
    return value


def assessment(value, score=4):
    text = a.answer(value)
    judgment = {'score': score, 'reason': '整段接住了当前只想闲聊的意愿。', 'excerpt': text}
    return {'id': value['id'], 'output_hash': a.output_hash(value['reply']),
            'dimensions': {key: deepcopy(judgment) for key in a.DIMENSIONS},
            'task': {'status': 'satisfied', 'reason': '没有未完成任务。', 'excerpt': text},
            'boundary': {'status': 'respected', 'reason': '未强行建议。', 'excerpt': text},
            'sample_use': {'category': 'not_observable', 'reason': '无可辨认的措辞移植。', 'excerpt': ''}}


def protocol():
    return {'review': [{'number': i, 'case_id': f'G{7 + (i - 1) // 2:02d}',
                        'role': 'process' if i % 2 else 'candid', 'replicate': 1,
                        'full_side': 'A' if i % 4 in (1, 2) else 'B'} for i in range(1, 13)]}


def bundle(values=None):
    group = 'G07-process-r1'
    values = values or {f'{group}-raw': result(f'{group}-raw'), f'{group}-full': result(f'{group}-full', '那就歇一会儿。', '\n新规格')}
    aux = {'parsed': {'frame': {'focus': '只想聊天'}}, 'calls': ['a001']}
    parent, current = Path('/old/real-v1'), Path('/new/real-v1')
    return {'root': current, 'parent': parent, 'results': values,
            'prepared': {'positions': [{'id': group, 'conditions': ['raw', 'essential', 'full']}]},
            'protocol': protocol(), 'real': True,
            'materials': {'cases': [{'id': f'G{i:02d}', 'members': ['先别建议，随便聊聊。'],
                                     'history': [('旧问', '旧答')]} for i in range(7, 13)],
                          'roles': {'process': {'style': '在意过程'}, 'candid': {'style': '坦率取舍'}}},
            'auxiliary': {group: aux},
            'origins': {('auxiliary', group): current} | {('results', key): current for key in values},
            'receipts': {'aux': {'origin_root': str(current), 'receipt_id': 'a001', 'qualified_id': 'new::a001',
                                 'transport': {'diagnostics': {'response_model': 'm', 'system_fingerprint': 'b'}}}}}


class AuthoredReadings(unittest.TestCase):
    def test_scores_need_raw_hash_exact_excerpts_and_all_dimensions(self):
        b = bundle(); value = next(iter(b['results'].values())); row = assessment(value)
        self.assertIn(value['id'], a.validate_main([row], b))
        for change, code in (('hash', 'output_changed'), ('quote', 'excerpt'), ('range', 'score_range'), ('bool', 'score_range'), ('dimension', 'dimensions')):
            broken = deepcopy(row)
            if change == 'hash': broken['output_hash'] = 'stale'
            if change == 'quote': broken['dimensions']['im_naturalness']['excerpt'] = '输出中没有'
            if change == 'range': broken['dimensions']['im_naturalness']['score'] = 6
            if change == 'bool': broken['dimensions']['im_naturalness']['score'] = True
            if change == 'dimension': del broken['dimensions']['im_naturalness']
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, code):
                a.validate_main([broken], b)

    def test_alias_cannot_create_another_independent_assessment(self):
        canonical = result('G07-process-r1-raw')
        alias = deepcopy(canonical) | {'id': 'G07-process-r1-full', 'condition': 'full', 'status': 'aliased',
                                     'canonical_id': canonical['id'], 'actual_manifest_from': canonical['id']}
        b = bundle({canonical['id']: canonical, alias['id']: alias})
        with self.assertRaisesRegex(ValueError, 'alias_duplicate_assessment'):
            a.validate_main([assessment(alias)], b)

    def test_auxiliary_evidence_binds_concrete_parsed_result_and_source(self):
        b = bundle(); group = next(iter(b['auxiliary'])); parsed = b['auxiliary'][group]['parsed']
        row = {'group': group, 'result_hash': fingerprint(parsed), 'critical_items': [
            {'key': 'current_wish', 'status': 'correct', 'reason': '最新意愿是交流。',
             'input_excerpt': '先别建议', 'frame_excerpt': '只想聊天', 'covers': list(a.AUXILIARY_COVERAGE)}]}
        self.assertIn(group, a.validate_auxiliary([row], b))
        for field in ('input_excerpt', 'frame_excerpt'):
            broken = deepcopy(row); broken['critical_items'][0][field] = '伪造片段'
            with self.assertRaisesRegex(ValueError, 'excerpt'):
                a.validate_auxiliary([broken], b)
        b['auxiliary'][group]['parsed']['frame']['focus'] = 'different'
        with self.assertRaisesRegex(ValueError, 'assessment_changed'):
            a.validate_auxiliary([row], b)

    def test_auxiliary_reading_must_cover_frame_and_all_frozen_semantic_categories(self):
        b = bundle(); group = next(iter(b['auxiliary']))
        row = {'group': group, 'result_hash': fingerprint(b['auxiliary'][group]['parsed']), 'critical_items': [
            {'key': 'only_focus', 'status': 'correct', 'reason': '只检查焦点还不够。',
             'input_excerpt': '先别建议', 'frame_excerpt': '只想聊天', 'covers': ['frame']}]}
        with self.assertRaisesRegex(ValueError, 'coverage_incomplete'):
            a.validate_auxiliary([row], b)

    def test_absent_behavior_evidence_needs_explicit_unable_or_unobservable(self):
        b = bundle(); value = next(iter(b['results'].values())); row = assessment(value)
        row['boundary']['excerpt'] = ''
        with self.assertRaisesRegex(ValueError, 'excerpt'): a.validate_main([row], b)
        row['boundary']['status'] = 'unable'
        self.assertEqual(a.validate_main([row], b)[value['id']]['boundary']['status'], 'unable')


class ComparisonReading(unittest.TestCase):
    def rows(self, b):
        return [dict(id=v['id'], canonical_id=v.get('canonical_id', v['id']), model_parameters={'temperature': 1},
                     model='m', backend='b') for v in b['results'].values()]

    def test_score_difference_does_not_invent_whole_context_win(self):
        b = bundle(); scores = {key: assessment(v, 5 if v['condition'] == 'full' else 1) for key, v in b['results'].items()}
        row = a.comparisons(b, self.rows(b), scores)[0]
        self.assertEqual(row['verdict'], 'unable')
        self.assertEqual(row['im_delta'], 4)
        self.assertTrue(row['explained_consumption_only'])

    def test_authored_pair_has_quotes_and_task_boundary_precedence(self):
        b = bundle(); scores = {key: assessment(v) for key, v in b['results'].items()}
        reading = {'group': 'G07-process-r1', 'left': 'full', 'right': 'raw', 'outcome': 'left',
                   'reason': '两段都合规，左侧接话更轻松，没有报幕。', 'left_excerpt': '那就歇一会儿。', 'right_excerpt': '接着聊。'}
        row = a.comparisons(b, self.rows(b), scores, [reading])[0]
        self.assertEqual(row['verdict'], 'win')
        self.assertEqual(row['reason'], reading['reason'])
        scores['G07-process-r1-full']['boundary']['status'] = 'violated'
        self.assertEqual(a.comparisons(b, self.rows(b), scores, [reading])[0]['verdict'], 'loss')
        reading['left_excerpt'] = '没说过'
        with self.assertRaisesRegex(ValueError, 'pair_excerpt'): a.comparisons(b, self.rows(b), scores, [reading])

    def test_backend_missing_limits_attribution_not_fabricated_identity(self):
        b = bundle(); rows = self.rows(b); rows[0]['backend'] = None
        row = a.comparisons(b, rows, {})[0]
        self.assertFalse(row['main_backend_known'])
        self.assertEqual(row['attribution'], 'consumption_difference_backend_limited')
        self.assertEqual(row['shared_auxiliary_calls'], ['new::a001'])

    def test_parameter_change_is_unexplained_not_component_gain(self):
        b = bundle(); rows = self.rows(b); rows[0]['model_parameters']['temperature'] = 0.2
        row = a.comparisons(b, rows, {})[0]
        self.assertFalse(row['explained_consumption_only'])
        self.assertEqual(row['attribution'], 'unexplained_difference')

    def test_internal_deadline_is_not_a_wire_difference_but_real_temperature_is(self):
        b = bundle(); rows = self.rows(b)
        model = {'name': 'grok-4.7', 'max_tokens': 8192, '_request_deadline': 1,
                 '_dialogue_profile': {'version': 'natural-dialogue-v2', 'seed': 'fixed', 'temperature': 1.1}}
        later = deepcopy(model); later['_request_deadline'] = 200
        rows[0]['model_parameters'] = a.provider_parameters(model, [])
        rows[1]['model_parameters'] = a.provider_parameters(later, [])
        self.assertTrue(a.comparisons(b, rows, {})[0]['explained_consumption_only'])
        self.assertNotIn('_request_deadline', rows[0]['model_parameters'])
        later['_dialogue_profile']['temperature'] = 0.3
        rows[1]['model_parameters'] = a.provider_parameters(later, [])
        self.assertEqual(rows[1]['model_parameters']['temperature'], 0.3)
        self.assertEqual(a.comparisons(b, rows, {})[0]['attribution'], 'unexplained_difference')

    def test_bad_alias_never_counts_as_another_completion(self):
        b = bundle(); v = b['results']['G07-process-r1-full']
        v.update(status='aliased', canonical_id='G07-process-r1-raw', actual_manifest_from='G07-process-r1-raw')
        with self.assertRaisesRegex(ValueError, 'alias_not_identical'): a.canonical(b, v['id'])


class AnonymousReview(unittest.TestCase):
    def complete(self):
        b = bundle(); b['results'] = {}; b['origins'] = {}
        for item in b['protocol']['review']:
            group = f"{item['case_id']}-{item['role']}-r{item['replicate']}"
            for condition in ('full', 'raw'):
                v = result(group + '-' + condition, '候选自然接话。' if condition == 'full' else '对照自然接话。', '\n规格' if condition == 'full' else '')
                b['results'][v['id']] = v
        return b

    def test_fixed_twelve_not_selected_by_quality_and_no_mapping_disclosure(self):
        b = self.complete(); plan = a.review_plan(b)
        self.assertEqual(len(plan), 12)
        self.assertEqual(Counter(x['full_side'] for x in plan), {'A': 6, 'B': 6})
        text = a.render_review(b, plan)
        self.assertEqual(text.count('## '), 12)
        self.assertIn('````', text)
        self.assertNotIn('full_side', text)
        self.assertNotIn('候选条件', text)

    def test_alias_is_no_vote_even_if_user_chose_a_and_missing_is_not_replaced(self):
        b = self.complete(); ident = 'G07-process-r1-raw'
        b['results']['G07-process-r1-full'] = deepcopy(b['results'][ident]) | {'id': 'G07-process-r1-full', 'condition': 'full'}
        del b['results']['G12-candid-r1-full']
        plan = a.review_plan(b)
        self.assertFalse(plan[0]['vote_required'])
        self.assertFalse(plan[11]['complete'])
        result = a.resolve_user_review([{'number': 1, 'choice': 'A'}], plan, b['results'])
        self.assertEqual(result[0]['user_candidate_verdict'], 'no_vote_identical_input')
        self.assertIn('尚未齐备', a.render_review(b, plan))
        with self.assertRaisesRegex(ValueError, 'missing_result'):
            a.resolve_user_review([{'number': 12, 'choice': 'B'}], plan, b['results'])

    def test_user_answers_do_not_get_invented_reasons_and_duplicates_reject(self):
        b = self.complete(); plan = a.review_plan(b)
        rows = a.resolve_user_review([{'number': 1, 'choice': 'B'}], plan, b['results'])
        self.assertIsNone(rows[0]['user_reason'])
        self.assertEqual(rows[0]['user_candidate_verdict'], 'loss')
        with self.assertRaisesRegex(ValueError, 'review_number'):
            a.resolve_user_review([{'number': 1, 'choice': 'B'}] * 2, plan, b['results'])

    def test_omitted_identical_input_vote_completes_coverage_without_an_invented_user_choice(self):
        b = self.complete()
        b['results']['G07-process-r1-full'] = deepcopy(b['results']['G07-process-r1-raw']) | {'id': 'G07-process-r1-full', 'condition': 'full'}
        plan = a.review_plan(b)
        rows = a.resolve_user_review([{'number': i, 'choice': '平'} for i in range(2, 13)], plan, b['results'])
        status = a.review_coverage(plan, rows)
        self.assertEqual(status, {'received': 11, 'exempt_identical_input': 1, 'covered': 12, 'complete': True})
        self.assertIsNone(rows[0]['user_choice'])
        self.assertFalse(rows[0]['is_user_opinion'])
        self.assertEqual(rows[0]['user_candidate_verdict'], 'no_vote_identical_input')
        fewer = a.resolve_user_review([{'number': i, 'choice': '平'} for i in range(3, 13)], plan, b['results'])
        self.assertFalse(a.review_coverage(plan, fewer)['complete'])


class ProvenanceAndUsage(unittest.TestCase):
    def test_origin_qualified_ids_keep_failed_old_window_and_new_recovery_separate(self):
        b = bundle(); b['receipts'] = {}
        for origin, count, successful in ((b['parent'], 4, False), (b['root'], 1, True)):
            for n in range(count):
                ident = f'{n + 1:04d}'; key = str(origin) + '::' + ident
                b['receipts'][key] = {'origin_root': str(origin), 'receipt_id': ident, 'qualified_id': key,
                    'reserve': {'ordinal': n + 1, 'slot': 'G11-process-r1/joint', 'real': True},
                    'terminal': {'status': 'completed' if successful else 'failed'},
                    'transport': {'usage': {'total_tokens': 10} if successful else None}}
        stats = a.usage_summary(b)
        self.assertEqual(stats['scopes']['combined']['physical_calls'], 5)
        self.assertEqual(stats['scopes']['original']['failed_calls'], 4)
        self.assertEqual(stats['scopes']['continuation']['native_retry_attempts'], 0)
        self.assertEqual(stats['scopes']['combined']['usage']['unknown_calls'], 4)
        self.assertEqual(stats['user_authorized_new_window_recoveries'], ['G11-process-r1/joint'])

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_import_hash_and_success_replay_guard_and_original_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); parent = base / 'parent' / 'real-v1'; current = base / 'continuation' / 'real-v1'
            parent.mkdir(parents=True); current.mkdir(parents=True)
            for path in (parent, current):
                self.write(path / 'prepared.json', {'real': True, 'batch': path.parent.name, 'positions': []})
                self.write(path / 'protocol.json', protocol())
                self.write(path / 'materials.json', bundle()['materials'])
            value = result('G08-process-r1-raw'); source = parent / 'results' / (value['id'] + '.json')
            self.write(source, value); old_bytes = source.read_bytes()
            imports = {'parent_root': str(parent), 'results': {value['id']: {'path': str(source), 'sha256': a.digest_file(source)}}, 'auxiliary': {}}
            self.write(current / 'imports.json', imports)
            with patch.object(a, 'PARENT', parent.parent), patch.object(a, 'OUT', current.parent):
                loaded = a.load_bundle(current)
                self.assertEqual(loaded['results'][value['id']], value)
                self.assertEqual(source.read_bytes(), old_bytes)
                self.write(current / 'results' / source.name, value)
                with self.assertRaisesRegex(ValueError, 'successful_position_replayed'): a.load_bundle(current)
                (current / 'results' / source.name).unlink()
                source.write_text('{}', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'import_changed'): a.load_bundle(current)

    def test_output_cannot_target_original_or_a_sealed_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'new'; out.mkdir()
            with patch.object(a, 'OUT', out):
                self.assertEqual(a.confined(out / 'real-v1'), (out / 'real-v1').resolve())
                with self.assertRaisesRegex(ValueError, 'outside_continuation'): a.confined(root / 'old')
                self.write(out / 'closure.json', {'closed': True})
                with self.assertRaisesRegex(ValueError, 'sealed'): a.confined(out / 'real-v1')

    def test_user_corrections_append_without_overwriting_original(self):
        b = AnonymousReview().complete()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'real-v1'; root.mkdir()
            with patch.object(a, 'OUT', root.parent), patch.object(a, 'load_bundle', return_value=b):
                first = a.append_user_review(root, [{'number': 1, 'choice': 'A'}], raw_answer='01 A')
                before = first.read_bytes()
                second = a.append_user_review(root, [{'number': 1, 'choice': 'B'}], after_mapping_revealed=True)
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), before)
                self.assertEqual(a.read(second)['supersedes'], first.name)
                self.assertTrue(a.read(second)['after_mapping_revealed'])


from collections import Counter


if __name__ == '__main__':
    unittest.main()
