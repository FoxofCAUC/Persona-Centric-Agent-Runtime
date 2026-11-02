"""CR1 evidence/report tooling. No generation or automated naturalness heuristics.

Scores and semantic judgments must be supplied after reading every real output.
This module validates exact excerpt bindings, summarizes them, and prepares the
predeclared anonymous user subset. It never invents missing reviewer decisions.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_frame import require


def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))


def text_of(result):
    value = json.loads(result['raw'])
    return '\n'.join(item['content'] for item in value['messages'])


def percentile(values, p):
    if not values: return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo = int(index); hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def scene_interval(pairs, *, seed=71301):
    grouped = defaultdict(list)
    for row in pairs: grouped[row['scene']].append(row['delta'])
    values = [statistics.mean(grouped[key]) for key in sorted(grouped)]
    if not values: return None
    rng = random.Random(seed)
    boot = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(10000)]
    return {'scene_count': len(values), 'mean': statistics.mean(values),
            'descriptive_95_percent_interval': [percentile(boot, .025), percentile(boot, .975)],
            'not_fixed_backend_causal_interval': True}


def validate_assessments(rows, results, materials, protocol):
    require(len(rows) == len({r['position_id'] for r in rows}) == len(results), 'cr1_assessment_coverage')
    cases = {c['id']: c for c in materials['cases']}
    for row in rows:
        require(row['position_id'] in results, 'cr1_unknown_assessment_position')
        result = results[row['position_id']]
        output = text_of(result)
        require(row['output_hash'] == fingerprint(result['raw']), 'cr1_assessment_output_changed')
        require(set(row['dimensions']) == set(protocol['dimensions']), 'cr1_assessment_dimensions')
        for dimension in row['dimensions'].values():
            require(type(dimension['score']) is int and 1 <= dimension['score'] <= 5, 'cr1_score_range')
            require(bool(dimension['reason']) and bool(dimension['excerpt']) and dimension['excerpt'] in output,
                    'cr1_score_excerpt_missing')
        case_id = result['position']['item'].split('-')[0]
        case = cases[case_id]
        require(len(row['obligations']) == len(case['spec']['obligations']), 'cr1_obligation_coverage')
        require(len(row['boundaries']) == len(case['spec']['avoid']), 'cr1_boundary_coverage')
        for judgment in row['obligations'] + row['boundaries']:
            require(judgment['status'] in ('satisfied', 'partial', 'not_satisfied', 'unable', 'respected', 'violated'),
                    'cr1_semantic_status')
            require(bool(judgment['reason']) and bool(judgment['excerpt']) and judgment['excerpt'] in output,
                    'cr1_semantic_excerpt_missing')
        require(row['sample_use']['category'] in ('natural_borrow', 'fitting_reuse', 'forced', 'fact_transfer', 'not_observable'),
                'cr1_sample_use_category')
        require(bool(row['sample_use']['reason']), 'cr1_sample_use_reason')
    return {r['position_id']: r for r in rows}


def compare(results, assessments, prepared, contexts, protocol):
    pairs = []
    for candidate, control in protocol['comparisons']:
        for item in prepared['items']:
            for replicate in (1, 2):
                left = f'{item["key"]}/{candidate}/r{replicate}'
                right = f'{item["key"]}/{control}/r{replicate}'
                if left not in results or right not in results: continue
                a, b = assessments[left], assessments[right]
                delta = a['dimensions']['im_naturalness']['score'] - b['dimensions']['im_naturalness']['score']
                error_a = any(x['status'] in ('not_satisfied', 'partial', 'unable', 'violated') for x in a['obligations'] + a['boundaries'])
                error_b = any(x['status'] in ('not_satisfied', 'partial', 'unable', 'violated') for x in b['obligations'] + b['boundaries'])
                verdict = 'loss' if error_a and not error_b else 'win' if error_b and not error_a else 'win' if delta > 0 else 'loss' if delta < 0 else 'tie'
                diagnostics = [results[pos].get('diagnostics', {}) for pos in (left, right)]
                backend_known = all(d.get('response_model') and d.get('system_fingerprint') for d in diagnostics)
                same_backend = backend_known and all(diagnostics[0].get(k) == diagnostics[1].get(k) for k in ('response_model', 'system_fingerprint'))
                context = contexts[item['key']]
                same_input = context['conditions'][candidate]['input_hash'] == context['conditions'][control]['input_hash']
                pairs.append({'id': f'{item["key"]}/{candidate}-{control}/r{replicate}', 'scene': item['case_id'],
                    'role': item['role_id'], 'comparison': candidate + '-' + control, 'candidate': left, 'control': right,
                    'delta': delta, 'dimension_deltas': {k: a['dimensions'][k]['score'] - b['dimensions'][k]['score'] for k in protocol['dimensions']},
                    'verdict': verdict, 'verdict_method': 'machine reading scores and semantic boundary judgments; not independent blind judging',
                    'candidate_excerpt': a['dimensions']['im_naturalness']['excerpt'],
                    'control_excerpt': b['dimensions']['im_naturalness']['excerpt'],
                    'reason': a['dimensions']['im_naturalness']['reason'] + '；对照：' + b['dimensions']['im_naturalness']['reason'],
                    'candidate_task_or_boundary_error': error_a, 'control_task_or_boundary_error': error_b,
                    'same_input': same_input, 'main_backend_known': backend_known, 'main_backend_same': same_backend,
                    'component_attribution_eligible': bool(same_backend and not same_input),
                    'shared_selector': True, 'unexplained_input_change': False})
    return pairs


def review_subset(pairs, cases):
    """Four deterministic strata per comparison; no score-driven re-generation."""
    selected = []
    for comparison in ('B1-B0', 'B2-B1', 'B2-B0'):
        group = [p for p in pairs if p['comparison'] == comparison]
        used = set()
        for stratum in ('strongest_positive', 'strongest_negative', 'closest_tie_or_repeat_disagreement', 'task_boundary'):
            candidates = [p for p in group if p['scene'] not in used]
            require(bool(candidates), 'cr1_review_not_enough_scenes')
            if stratum == 'task_boundary':
                task = [p for p in candidates if cases[p['scene']]['spec']['avoid'] or cases[p['scene']]['category'] == 'realization']
                candidates = task or candidates
                key = lambda p: (-int(p['candidate_task_or_boundary_error'] != p['control_task_or_boundary_error']), -abs(p['delta']), p['id'])
            elif stratum == 'strongest_positive': key = lambda p: (-p['delta'], p['id'])
            elif stratum == 'strongest_negative': key = lambda p: (p['delta'], p['id'])
            else: key = lambda p: (abs(p['delta']), p['id'])
            row = dict(min(candidates, key=key), stratum=stratum)
            used.add(row['scene']); selected.append(row)
    return selected


def report(root):
    root = Path(root)
    prepared, materials, protocol = read(root / 'prepared.json'), read(root / 'materials.json'), read(root / 'protocol.json')
    results = {r['position']['id']: r for p in sorted((root / 'results').glob('*.json')) if (r := read(p))['real'] == prepared['real']}
    require(len(results) == len(list((root / 'results').glob('*.json'))), 'cr1_mixed_report_results')
    output = root / 'analysis'; output.mkdir(exist_ok=True)
    cases = {c['id']: c for c in materials['cases']}
    for scene in materials['real_subset']:
        rows = [dict(position=key, output_hash=fingerprint(value['raw']), output=text_of(value), diagnostics=value['diagnostics'])
                for key, value in results.items() if key.startswith(scene + '-')]
        atomic_json(output / 'reading' / (scene + '.json'), {'case': cases[scene], 'outputs': rows,
                    'selected_contexts': {item['key']: read(root / 'contexts' / (item['key'] + '.json'))['selected']
                                         for item in prepared['items'] if item['case_id'] == scene and (root / 'contexts' / (item['key'] + '.json')).exists()}})
    if not prepared['real']:
        summary = {'status': 'simulation_report_only', 'real_calls': 0, 'true_effect': 'unverified'}
        atomic_json(output / 'summary.json', summary); return summary
    assessment_path = output / 'machine-assessments.json'
    if not assessment_path.exists():
        return {'status': 'awaiting_full_machine_reading', 'real_outputs': len(results), 'expected': 144, 'stage_complete': False}
    assessments = validate_assessments(read(assessment_path), results, materials, protocol)
    contexts = {i['key']: read(root / 'contexts' / (i['key'] + '.json')) for i in prepared['items'] if (root / 'contexts' / (i['key'] + '.json')).exists()}
    pairs = compare(results, assessments, prepared, contexts, protocol)
    atomic_json(output / 'comparisons.json', pairs)
    summaries = {}
    for comparison in ('B1-B0', 'B2-B1', 'B2-B0'):
        group = [p for p in pairs if p['comparison'] == comparison]
        interval = scene_interval(group)
        summaries[comparison] = {'pairs': len(group), 'wins': sum(p['verdict'] == 'win' for p in group),
                'ties': sum(p['verdict'] == 'tie' for p in group), 'losses': sum(p['verdict'] == 'loss' for p in group),
                'naturalness_delta': interval, 'research_signal_met': bool(interval and interval['mean'] >= .5),
                'new_task_boundary_error_pairs': sum(p['candidate_task_or_boundary_error'] and not p['control_task_or_boundary_error'] for p in group),
                'known_same_backend_pairs': sum(p['component_attribution_eligible'] for p in group),
                'route_observation_scope': 'actual gateway route; missing backend identifiers do not prove fixed weights'}
    selected = review_subset(pairs, cases) if len(results) == 144 else []
    mapping, lines = [], ['# CR1 匿名关键对照', '', '每组只需选择 A / B / 平 / 无法判断，可补一句原因。条件映射在审阅后揭示。', '']
    for index, pair in enumerate(selected, 1):
        reverse = int(hashlib.sha256((protocol['review']['anonymous_orientation_seed'] + pair['id']).encode()).hexdigest(), 16) % 2
        ids = [pair['candidate'], pair['control']]
        if reverse: ids.reverse()
        mapping.append({'number': index, 'pair': pair['id'], 'A': ids[0], 'B': ids[1], 'stratum': pair['stratum']})
        case = cases[pair['scene']]
        lines += [f'## {index:02d}', '', '前文：' + encoded(case['history']), '本轮：' + ' / '.join(case['members']),
                  '', '**A**', '', text_of(results[ids[0]]), '', '**B**', '', text_of(results[ids[1]]), '', '选择：____　原因：____', '']
    if selected:
        (output / 'anonymous-user-review.md').write_text('\n'.join(lines), encoding='utf-8')
        atomic_json(output / 'review-mapping-local.json', mapping)
    review = read(output / 'user-review.json') if (output / 'user-review.json').exists() else []
    user_complete = len(review) == 12 and {r['number'] for r in review} == set(range(1, 13)) and all(r['choice'] in ('A', 'B', 'tie', 'unable') for r in review)
    summary = {'status': 'machine_analysis_complete_user_review_complete' if user_complete else 'machine_analysis_complete_awaiting_user_review',
               'real_outputs': len(results), 'machine_assessments': len(assessments), 'comparisons': summaries,
               'user_review_expected': 12, 'user_review_received': len(review), 'user_review_complete': user_complete,
               'independent_human_blind_review': False, 'stage_complete': False}
    atomic_json(output / 'summary.json', summary)
    return summary
