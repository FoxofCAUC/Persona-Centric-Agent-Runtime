"""Offline M6.4 reports; no network/model calls. Reviews are written by the evaluator.

Private comparison/checkpoint and anonymized review artifacts stay outside Git.
Only aggregate summary.json is suitable for copying into public documentation.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import atomic_json
from scripts.acceptance_m64 import MODES, automatic_checks

AXES = ('relevance', 'tone', 'repetition', 'questions', 'completeness')


def summarize(data, reviews=None):
    rows = [row for row in data['cases'] if 'turn' in row]
    reviews = reviews or {}
    result = {'planned': 72, 'recorded': len(rows), 'machine_sample_only': True, 'money_cost': 'unknown',
              'major_calls': sum(row['turn'].get('attempt_count', 0) for row in rows), 'modes': {},
              'reviewed': 0, 'critical_defects': [], 'automatic_failures': [], 'quality_gate': 'pending'}
    for row in rows:
        checks = automatic_checks(row, row['turn'])
        for key, value in checks.items():
            if not value: result['automatic_failures'].append({'anonymous_id': row['anonymous_id'], 'check': key})
        review = reviews.get(row['anonymous_id'])
        if review:
            if set(review.get('scores', {})) != set(AXES) or any(type(v) is not int or not 0 <= v <= 2 for v in review['scores'].values()) or not review.get('reason') or type(review.get('critical')) is not bool:
                raise ValueError('incomplete machine review: '+row['anonymous_id'])
            result['reviewed'] += 1
            if review['critical']: result['critical_defects'].append(row['anonymous_id'])
    for mode in MODES:
        group = [r for r in rows if r['mode'] == mode]
        seconds = sorted(r['turn'].get('timings', {}).get('provider_seconds') for r in group if r['turn'].get('timings', {}).get('provider_seconds') is not None)
        result['modes'][mode] = {'recorded': len(group), 'tokens': sum((r['turn'].get('usage') or {}).get('total_tokens', 0) for r in group),
            'provider_seconds_median': statistics.median(seconds) if seconds else None,
            'provider_seconds_max': max(seconds) if seconds else None,
            'desktop_platform_delivery': 'not performed', 'not_worse_than_legacy': None}
        if mode != 'legacy_full' and result['reviewed'] == 72:
            good = 0
            for row in group:
                baseline = next(r for r in rows if r['scenario'] == row['scenario'] and r['mode'] == 'legacy_full')
                review, base = reviews[row['anonymous_id']], reviews[baseline['anonymous_id']]
                good += not review['critical'] and sum(review['scores'].values()) >= sum(base['scores'].values())
            result['modes'][mode]['not_worse_than_legacy'] = good
    if len(rows) == 72 and result['reviewed'] == 72:
        result['quality_gate'] = 'passed' if (result['major_calls'] == 72 and not result['critical_defects'] and not result['automatic_failures'] and
            all(result['modes'][m]['not_worse_than_legacy'] >= 20 for m in MODES[1:])) else 'failed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence.resolve(); repo = Path(__file__).resolve().parents[1]
    if root == repo or repo in root.parents: parser.error('private evidence must stay outside Git')
    data = json.loads((root/'comparison.json').read_text(encoding='utf-8'))
    # No mode, account, source/request ID, configuration or context in blind view.
    atomic_json(root/'blind-outputs.json', [{'anonymous_id': r['anonymous_id'], 'scenario': r['scenario'],
        'input': r['text'], 'reply': r['turn'].get('reply'), 'status': r['turn']['status']} for r in data['cases'] if 'turn' in r])
    reviews = json.loads((root/'reviews.json').read_text(encoding='utf-8')) if (root/'reviews.json').exists() else {}
    result = summarize(data, reviews)
    atomic_json(root/'summary.json', result)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__': main()
