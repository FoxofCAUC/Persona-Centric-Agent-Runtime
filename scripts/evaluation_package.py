"""Explicit local package preparation/status. No model dispatch or release action."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('create','status'));p.add_argument('--home',required=True,type=Path)
    p.add_argument('--package-id',required=True);p.add_argument('--fixture',type=Path);p.add_argument('--fixture-sha256')
    p.add_argument('--simulated',action='store_true');args=p.parse_args()
    paths=RuntimePaths(safe(args.home));store=EvaluationStore(paths)
    if args.action=='create':
        if not args.fixture or not args.fixture_sha256:raise ValueError('Explicit frozen fixture and SHA256 required')
        path=safe(args.fixture,paths.home/'exports/acceptance');raw=path.read_bytes()
        if len(raw)>4*1024*1024 or sha256(raw).hexdigest()!=args.fixture_sha256:raise ValueError('Fixture fingerprint/size mismatch')
        fixture=json.loads(raw)
        store.create(args.package_id,fixture['cases'],fixture['binding'],simulated=args.simulated)
    print(json.dumps(store.status(args.package_id),ensure_ascii=False))


if __name__=='__main__':main()
