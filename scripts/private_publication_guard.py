"""Fail-closed local pre-push guard. Source records remain outside Git."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import publication_scan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--outgoing',action='store_true');parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    bases=[]
    if args.outgoing:
        for line in sys.stdin.read().splitlines():
            fields=line.split()
            if len(fields)==4 and fields[3]!='0'*40:bases.append(fields[3])
        if not bases:bases=['origin/main']
    else:bases=[None]
    for base in bases:
        result=publication_scan(RuntimePaths(args.home.resolve()),args.repo,staged=args.staged,base=base)
        print(json.dumps(result,ensure_ascii=False))
        if not result['passed']:return 1
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('Private publication validation unavailable; push blocked.',file=sys.stderr)
        raise SystemExit(1)
