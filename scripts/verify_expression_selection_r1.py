"""Affected tests and immutable R0 oracle. All external sockets are denied."""
from pathlib import Path
import json
import shutil
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import verify_prompt_assembly_r0 as oracle
from scripts import check_prompt_baseline as frozen
from persona_runtime.config import atomic_json


def main():
    output=oracle.HOME/'exports/acceptance/M7.6.3-R1-expression-selection/offline'
    output.mkdir(parents=True,exist_ok=True)
    for name in ('original-context_m4.py','baseline-integrity.json'):
        destination=output/name
        if not destination.exists():shutil.copyfile(oracle.OUT/name,destination)
        if destination.read_bytes()!=(oracle.OUT/name).read_bytes():raise ValueError('R0 oracle integrity')
    oracle.OUT=output;frozen.EVIDENCE=output
    oracle.SUITES += ['tests.test_m754r2','tests.test_m754r3','tests.test_expression_selection_r1']
    with patch.object(sys,'argv',['verify_expression_selection_r1']):
        frozen.main();oracle.main()
    tests=json.loads((output/'targeted-results.json').read_text(encoding='utf-8'))
    baseline=json.loads((output/'scenario-diff.json').read_text(encoding='utf-8'))
    atomic_json(output/'gate.json',{'passed':tests['passed'] and baseline['passed'],
        'tests':tests['tests'],'r0_scenarios':len(baseline['scenarios']),'real_calls':0,'qq':0})


if __name__=='__main__':main()
