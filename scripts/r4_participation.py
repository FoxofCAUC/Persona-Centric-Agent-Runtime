"""Fifth R4 batch; optional v7, user-authorized unlimited cumulative ledger."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import r4_expression_realization as common
from tests.r4_participation_fixture import fixture,rubric

OUT=common.OUT
VERSION='r4-participation-v1'
ARMS={'control':'natural-dialogue-v4','experiment':'natural-dialogue-v7'}
EXTRAS=('scripts/r4_participation.py','tests/r4_liveliness_fixture.py','tests/r4_participation_fixture.py')
GATE=OUT/'revision-005-offline-gate.json'


def prior_batches_unchanged():
    for index in ('001','002','003','004'):
        seal=common.read(OUT/f'batch-{index}-closure.json')
        for name,digest in seal['evidence_hashes'].items():
            if common.sha(OUT/f'real-batch-{index}'/name)!=digest:raise common.StopRun('prior_batch_evidence_changed:'+index+':'+name)
        for name,digest in seal['receipt_prefix_hashes'].items():
            if common.sha(OUT/'real-receipts'/name)!=digest:raise common.StopRun('prior_receipt_changed:'+name)


async def prepare(root,real):
    prior_batches_unchanged()
    auth=common.read(OUT/'budget-amendment-20261101.json')
    if auth['new_limit'] is not None or common.LIMIT is not None:raise common.StopRun('budget_amendment_mismatch')
    await common.prepare(root,real,data=fixture(),rules=rubric(),versions=ARMS,extras=EXTRAS,tool_version=VERSION,
        authorization={'budget_amendment':auth,'budget_amendment_hash':common.sha(OUT/'budget-amendment-20261101.json'),'route_choice':'沿用当前路由','normal_requests':48})


async def run(root,real):
    prior_batches_unchanged();await common.execute(root,real,gate_path=GATE);prior_batches_unchanged()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','run','report','verify'));args=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'execution.lock');action=args.action
    root=OUT/('simulation-batch-005' if action=='simulate' else 'real-batch-005')
    try:
        if action=='run':asyncio.run(run(root,True))
        else:
            with common.offline() as network:
                prior_batches_unchanged()
                if action=='prepare':asyncio.run(prepare(root,True))
                elif action=='simulate':asyncio.run(prepare(root,False));asyncio.run(run(root,False))
                else:
                    from scripts.r4_expression_report import report,verify
                    (report if action=='report' else verify)(root if (root/'run-started.json').exists() else OUT/'simulation-batch-005')
            common.atomic_json(OUT/('revision-005-network-'+action+'.json'),network)
    except (Exception,common.StopRun,asyncio.CancelledError) as error:
        if action=='run' and not (OUT/'blocked.json').exists():common.write_once(OUT/'blocked.json',{'reason':str(error),'action':action,'at':common.timestamp(),'automatic_resume':False})
        print(json.dumps({'stopped':str(error),'action':action},ensure_ascii=False),flush=True);sys.exit(2)
    finally:lock.close()


if __name__=='__main__':main()
