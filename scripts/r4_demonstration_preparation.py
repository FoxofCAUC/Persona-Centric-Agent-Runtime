"""Offline-only R4 response-demonstration preparation; never dispatches real models."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import r4_expression_realization as common
from tests.r4_demonstration_fixture import fixture,rubric

VERSION='r4-demonstration-offline-v1'
ARMS={'control':'natural-dialogue-v4','experiment':'natural-dialogue-v8'}
EXTRAS=('scripts/r4_demonstration_preparation.py','tests/r4_liveliness_fixture.py','tests/r4_demonstration_fixture.py')
PACKAGE=common.OUT/'simulation-batch-008'

def prior_unchanged():
    for index in range(1,8):
        seal=common.read(common.OUT/f'batch-{index:03}-closure.json')
        for name,digest in seal['evidence_hashes'].items():
            if common.sha(common.OUT/f'real-batch-{index:03}'/name)!=digest:raise common.StopRun('prior_evidence_changed:'+name)
        for name,digest in seal['receipt_prefix_hashes'].items():
            if common.sha(common.OUT/'real-receipts'/name)!=digest:raise common.StopRun('prior_receipt_changed:'+name)

async def prepare():
    await common.prepare(PACKAGE,False,data=fixture(),rules=rubric(),versions=ARMS,extras=EXTRAS,tool_version=VERSION,
        authorization={'offline_only':True,'real_generation_action':False,'new_paid_calls':0})

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','simulate','report','verify'));args=parser.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(common.OUT/'execution.lock')
    try:
        with common.offline() as network:
            prior_unchanged()
            if args.action=='prepare':asyncio.run(prepare())
            elif args.action=='simulate':asyncio.run(common.execute(PACKAGE,False))
            else:
                from scripts.r4_expression_report import report,verify
                (report if args.action=='report' else verify)(PACKAGE)
            prior_unchanged()
        common.atomic_json(common.OUT/('revision-008-network-'+args.action+'.json'),network)
    except (Exception,common.StopRun) as error:
        print(json.dumps({'stopped':str(error),'offline':True,'action':args.action},ensure_ascii=False));sys.exit(2)
    finally:lock.close()

if __name__=='__main__':main()
