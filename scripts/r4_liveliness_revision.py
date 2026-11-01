"""Second R4 batch under the same 192-call ledger; old successes are immutable."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import r4_expression_realization as common
from tests.r4_liveliness_fixture import fixture,rubric

VERSION='r4-liveliness-revision-v1'
EXTRAS=('scripts/r4_liveliness_revision.py','tests/r4_liveliness_fixture.py')
ARMS={'control':'natural-dialogue-v2','experiment':'natural-dialogue-v4'}
OUT=common.OUT
GATE=OUT/'revision-002-offline-gate.json'


def first_batch_unchanged():
    seal=common.read(OUT/'batch-001-closure.json')
    for name,digest in seal['evidence_hashes'].items():
        if common.sha(OUT/'real-batch-001'/name)!=digest:raise common.StopRun('first_batch_evidence_changed:'+name)
    for name,digest in seal['receipt_prefix_hashes'].items():
        if common.sha(OUT/'real-receipts'/name)!=digest:raise common.StopRun('first_batch_receipt_changed:'+name)


async def prepare(root,real):
    first_batch_unchanged()
    return await common.prepare(root,real,data=fixture(),rules=rubric(),versions=ARMS,extras=EXTRAS,tool_version=VERSION)


async def run(root,real):
    first_batch_unchanged()
    await common.execute(root,real,gate_path=GATE)
    first_batch_unchanged()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','run','report','verify'));args=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'execution.lock');action=args.action
    root=OUT/('simulation-batch-002' if action=='simulate' else 'real-batch-002')
    try:
        if action=='run':asyncio.run(run(root,True))
        else:
            with common.offline() as network:
                first_batch_unchanged()
                if action=='prepare':asyncio.run(prepare(root,True))
                elif action=='simulate':asyncio.run(prepare(root,False));asyncio.run(run(root,False))
                else:
                    from scripts.r4_expression_report import report,verify
                    (report if action=='report' else verify)(root if (root/'run-started.json').exists() else OUT/'simulation-batch-002')
            common.atomic_json(OUT/('revision-002-network-'+action+'.json'),network)
    except (Exception,common.StopRun,asyncio.CancelledError) as error:
        if action=='run' and not (OUT/'blocked.json').exists():common.write_once(OUT/'blocked.json',{'reason':str(error),'action':action,'at':common.timestamp(),'automatic_resume':False})
        print(json.dumps({'stopped':str(error),'action':action},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
