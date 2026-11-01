"""Generate blind DeepSeek review suggestions without writing human answers.

The command reuses the saved primary model.  It only supports the official
DeepSeek endpoint and writes a separate, resumable sidecar ledger.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.config import ConfigStore
from persona_runtime.evaluation_assistant import EvaluationAssistantStore, generate_one, official_main_model
from persona_runtime.paths import RuntimePaths


async def generate(args):
    paths = RuntimePaths(args.home.resolve())
    config = ConfigStore(paths)
    model = official_main_model(config)
    store = EvaluationAssistantStore(paths)
    store.prepare(args.package_id, model)
    store.recover(args.package_id)
    pending = store.pending(args.package_id)
    if args.limit is not None:
        pending = pending[:args.limit]
    if not args.confirm_billable:
        raise SystemExit(f'将使用主模型产生{len(pending)}次最多调用；确认后请添加 --confirm-billable')
    client = ChatCompletionsClient()
    completed = failed = consecutive = 0
    for number, (slot, row) in enumerate(pending, 1):
        started=time.monotonic()
        ok = await generate_one(client, model, store, args.package_id, slot, row)
        elapsed=time.monotonic()-started
        consecutive=0 if ok else consecutive+1
        completed += int(ok); failed += int(not ok)
        print(json.dumps({'progress':number,'total':len(pending),'case_id':row['case_id'],
                          'state':'success' if ok else 'failed'}, ensure_ascii=False), flush=True)
        if consecutive>=5 or (not ok and elapsed>600):
            raise RuntimeError('ai_review_model_tests_stopped')
    return store.status(args.package_id) | {'completed_this_run':completed,'failed_this_run':failed}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','generate'))
    parser.add_argument('--home',type=Path,default=Path('D:/Persona-Centric Agent Runtime'))
    parser.add_argument('--package-id',required=True)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--confirm-billable',action='store_true')
    args=parser.parse_args()
    store=EvaluationAssistantStore(RuntimePaths(args.home.resolve()))
    if args.limit is not None and args.limit<1:parser.error('--limit must be positive')
    if args.action=='status':result=store.status(args.package_id)
    else:
        from persona_runtime.instance import InstanceLock
        lock=InstanceLock(store.path(args.package_id).with_suffix('.lock'))
        try:result=asyncio.run(generate(args))
        finally:lock.close()
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':
    main()
