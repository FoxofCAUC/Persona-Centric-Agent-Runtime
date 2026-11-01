"""Explicit, resumable R04 understanding-only development command."""
import argparse
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.m763_r04 import OnlyObservation, source_hashes, run
from persona_runtime.instance import InstanceLock

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formal-home',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--real',action='store_true')
    parser.add_argument('--provider-check',type=Path)
    parser.add_argument('--recheck')
    parser.add_argument('--reason')
    args=parser.parse_args()
    lock=InstanceLock(args.formal_home.resolve()/'exports/acceptance/m763-r04-execution.lock')
    try:asyncio.run(run(args.formal_home,args.output,args.real,args.provider_check,args.recheck,args.reason))
    finally:lock.close()
