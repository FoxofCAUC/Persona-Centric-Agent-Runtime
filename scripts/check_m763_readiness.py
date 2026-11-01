"""P01 gate: explicit synthetic contracts and actual source inspection, zero models."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from experiments.persona_evaluation.contracts import ranked_situations, readiness, metric_gate
from experiments.persona_evaluation.source_probe import inspect_adapter
from persona_runtime.disposition_core.contracts import Member, Context
from persona_runtime.disposition_core.engine import interpret, load_policy


def evaluate():
    fixture = REPO / 'experiments/persona_evaluation/development_contracts.json'
    raw = fixture.read_bytes()
    rows = json.loads(raw)['cases']
    if len(rows) != 24 or len({r['id'] for r in rows}) != 24:
        raise ValueError('invalid_development_contracts')
    policy = load_policy(REPO / 'persona_runtime/disposition_core/rules.json')
    adapter = inspect_adapter(REPO / 'persona_runtime/disposition_shadow.py')
    results = []
    for row in rows:
        if row['kind'] == 'ranking':
            members = tuple(Member(f'm{i}', text) for i, text in enumerate(row['members']))
            context = Context('neutral', '', False, False, False, '')
            frame = interpret(members, context, policy)
            value = [r['category'] for r in ranked_situations(frame, members, policy)['candidates']]
        elif row['kind'] == 'readiness':
            value = adapter['ready'] if row['capabilities'] == 'production' else readiness(row['capabilities'])['ready']
        elif row['kind'] == 'metric':
            value = metric_gate(row['metric'], row['value'])
        else:
            raise ValueError('unknown_contract')
        results.append({'id': row['id'], 'passed': value == row['expected'], 'observed': value})
    passed = all(r['passed'] for r in results)
    return {'phase': 'P01', 'development_fixture_sha256': hashlib.sha256(raw).hexdigest(),
            'contract_results': results, 'development_passed': passed,
            'production_adapter': adapter, 'paid_generation_allowed': passed and adapter['ready'],
            'independent_set_created': False, 'independent_cases_consumed': 0,
            'main_calls': 0, 'selector_calls': 0, 'compiler_calls': 0, 'qq_sends': 0,
            'next': 'freeze_independent_set' if passed and adapter['ready'] else 'stop_at_readiness_gate'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Freeze the fixture fingerprint before any observed result is calculated.
    fixture = REPO / 'experiments/persona_evaluation/development_contracts.json'
    freeze = args.output.with_suffix('.freeze.json')
    with freeze.open('x', encoding='utf-8') as handle:
        json.dump({'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest()}, handle)
    report = evaluate()
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ('development_passed','paid_generation_allowed',
        'independent_set_created','main_calls','next')}, ensure_ascii=False))
    return 0 if report['paid_generation_allowed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
