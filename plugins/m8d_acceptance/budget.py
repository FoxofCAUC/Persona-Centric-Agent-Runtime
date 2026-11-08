"""Additional finite acceptance cap across isolated and formal runtime ledgers.

This gate can only delay/reject test calls; it cannot reserve or release PCAR
quota, dispatch a model, or alter a product task. Simulations use no paid calls.
"""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import time
from persona_runtime.chat import ModelError
from persona_runtime.instance import InstanceLock


def records(root):
    root=Path(root);paths=list(root.glob('real-*/calls/*.json'))+list((root/'formal-calls').glob('*.json'))
    return [json.loads(p.read_text(encoding='utf-8')) for p in paths]


def next_window(calls,now,organization,slots=1):
    if not organization:return None if sum(not c['organization'] for c in calls)+slots>8 else now
    epochs=[datetime.fromisoformat(c['at']).timestamp() for c in calls if c['organization']]
    waits=[now]
    for span,cap in ((3600,6),(86400,24)):
        used=sorted(t for t in epochs if t>now-span)
        if len(used)+slots>cap:
            if slots>cap:return None
            waits.append(used[len(used)+slots-cap-1]+span+.01)
    return max(waits)


def record_dispatch(root,path,row):
    """Atomic cross-root admission, immediately before the saved client call."""
    root=Path(root).resolve();path=Path(path).resolve()
    if not path.is_relative_to(root):raise ValueError('Acceptance call outside evidence root')
    with closing(InstanceLock(root/'finite-call-limit.lock')):
        now=time.time();next_epoch=next_window(records(root),now,row['organization'])
        if next_epoch is None or next_epoch>now:
            raise ModelError('acceptance_call_limit_wait',diagnostics={'request_headers_sent':False,'acceptance_next_epoch':next_epoch})
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(row,stream,ensure_ascii=False,indent=2)


def wait_for_formal_window(root):
    # Formal acceptance can require six background positions. Wait before the
    # managed host starts, so no provider timeout or unknown task is created.
    while True:
        now=time.time();next_epoch=next_window(records(root),now,True,slots=6)
        if next_epoch is not None and next_epoch<=now:return
        time.sleep(1)
