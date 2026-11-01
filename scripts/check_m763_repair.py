"""Controlled local repair evidence. A mock pass never opens the independent paid gate."""
import argparse,json,sys,time,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
from scripts.acceptance_m763_observation import source_hashes
from persona_runtime.archive import fingerprint


def run(out):
    out=out.resolve();out.mkdir(parents=True,exist_ok=True)
    if out.drive.upper()!='D:':raise ValueError('D-drive evidence required')
    target=out/'engineering.json'
    if target.exists():raise ValueError('Use a new evidence directory; preserve previous failures')
    cases=json.loads((REPO/'experiments/persona_evaluation/repair_cases.json').read_text(encoding='utf-8'))
    freeze={'source_hashes':source_hashes(),'fixture_hash':fingerprint(cases),'fixture_count':32}
    (out/'freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    names=('tests.test_m763_repair','tests.test_m763_desktop','tests.test_m763_preparation',
        'tests.test_m761.M761.test_preview_no_persistent_effect_and_export_16',
        'tests.test_m761_api.API.test_admin_preview_compile_and_health',
        'tests.test_m762_interfaces.API.test_admin_preview_compile_and_health')
    suite=unittest.defaultTestLoader.loadTestsFromNames(names);started=time.monotonic()
    with (out/'tests.log').open('x',encoding='utf-8') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    report={**freeze,'tests':result.testsRun,'seconds':round(time.monotonic()-started,3),
        'passed':result.wasSuccessful(),'failures':len(result.failures),'errors':len(result.errors),
        'source_hashes_after':source_hashes(),'cloud_calls':0,'formal_modified':False,'scope':'targeted_not_full_suite'}
    with target.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
    print(json.dumps({k:report[k] for k in ('tests','seconds','passed','failures','errors','cloud_calls')}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args();run(a.output)
