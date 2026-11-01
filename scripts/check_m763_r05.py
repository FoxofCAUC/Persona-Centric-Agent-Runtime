"""R05 targeted engineering gate. Never creates the independent dataset or dispatches models."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))


def hashes():
    return {str(p.relative_to(REPO)):sha256(p.read_bytes()).hexdigest() for p in (REPO/'persona_runtime').glob('*.py')}


def run(out):
    if out.drive.upper()!='D:':raise ValueError('D-drive evidence required')
    out.mkdir(parents=True,exist_ok=False)
    names=['tests.test_m763_live','tests.test_m763_r05','tests.test_m762.M762',
        'tests.test_m763_repair.Repair','tests.test_observation_timeout_v2','tests.test_m763_preparation',
        'tests.test_m762_interfaces.API','tests.test_m762_interfaces.Desktop','tests.test_m763_desktop',
        'tests.test_m754r2.SelectorTests.test_success_separate_attempts_and_manifest',
        'tests.test_m754r2.SelectorTests.test_model_snapshot_and_retry',
        'tests.test_m754r2.SelectorTests.test_ledger_failure_zero_calls']
    seen=set();tests=[]
    def collect(suite):
        for t in suite:
            if isinstance(t,unittest.TestSuite):collect(t)
            elif t.id() not in seen:seen.add(t.id());tests.append(t)
    collect(unittest.defaultTestLoader.loadTestsFromNames(names))
    before=hashes();started=time.monotonic()
    with (out/'tests.log').open('x',encoding='utf-8') as log:
        with patch('persona_runtime.chat.ChatCompletionsClient.complete',side_effect=AssertionError('External dispatch forbidden in R05')):
            result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite(tests))
    report={'phase':'R05','tests':result.testsRun,'seconds':round(time.monotonic()-started,3),'passed':result.wasSuccessful(),
        'failures':len(result.failures),'errors':len(result.errors),'source_before':before,'source_after':hashes(),
        'test_ids':sorted(seen),'scope':'targeted_not_full','external_calls':0,'independent_set_created':False}
    (out/'engineering.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('tests','seconds','passed','failures','errors')}))
    return result.wasSuccessful() and before==hashes()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    raise SystemExit(0 if run(parser.parse_args().output.resolve()) else 2)
