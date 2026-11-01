"""Offline R0 baseline. Freeze is exclusive and allowed only on the original source."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.prompt_baseline_fixture import capture_all
from persona_runtime.archive import fingerprint

BASE = '19bca9ee6407927117ded3bd43d9605716afce40'
FIXTURE = ROOT/'tests/fixtures/prompt-baseline-r0.json'
EVIDENCE = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/M7.6.3-R0-prompt-baseline')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--freeze', action='store_true'); args = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.freeze:
        assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == BASE
        assert not subprocess.check_output(['git', 'diff', 'HEAD', '--', 'persona_runtime'], cwd=ROOT)
        assert not FIXTURE.exists(), 'Frozen baseline already exists; do not replace it'
    results = capture_all()
    if args.freeze:
        record = {'base_commit': BASE, 'artificial_only': True, 'fixed_time': '2026-11-01T00:00:00+00:00',
                  'scenarios_hash': fingerprint(results), 'scenarios': results}
        with FIXTURE.open('x', encoding='utf-8', newline='\n') as f: json.dump(record, f, ensure_ascii=False, indent=2)
        original = subprocess.check_output(['git', 'show', BASE+':persona_runtime/context_m4.py'], cwd=ROOT)
        (EVIDENCE/'original-context_m4.py').write_bytes(original)
        (EVIDENCE/'baseline.json').write_bytes(FIXTURE.read_bytes())
        (EVIDENCE/'baseline-integrity.json').write_text(json.dumps({'base_commit': BASE,
            'baseline_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            'original_context_sha256': hashlib.sha256(original).hexdigest()}, indent=2), encoding='utf-8')
        print(json.dumps({'frozen':len(results), 'outcomes': {k:v.get('error_code','ok') for k,v in results.items()}}, ensure_ascii=False))
        return
    expected = json.loads(FIXTURE.read_text(encoding='utf-8'))
    assert fingerprint(expected['scenarios']) == expected['scenarios_hash'], 'Baseline integrity failure'
    rows = [{'scenario': name, 'equal': json.dumps(expected['scenarios'].get(name),ensure_ascii=False).encode('utf-8')==json.dumps(value,ensure_ascii=False).encode('utf-8'),
             'expected_hash': fingerprint(expected['scenarios'].get(name)), 'actual_hash': fingerprint(value),
             'outcome': value.get('error_code','ok')} for name,value in results.items()]
    passed = all(row['equal'] for row in rows) and set(results)==set(expected['scenarios'])
    (EVIDENCE/'scenario-diff.json').write_text(json.dumps({'passed':passed,'scenarios':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':passed,'scenarios':len(rows),'failed':[r['scenario'] for r in rows if not r['equal']]},ensure_ascii=False))
    if not passed: sys.exit(1)


if __name__ == '__main__': main()
