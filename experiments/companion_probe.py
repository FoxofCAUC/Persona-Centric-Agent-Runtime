"""Three declared artificial requests through the normal companion runtime."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_trial import load_trial
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.chat import ChatEngine
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.storage import Store

CASES = (
    ('ordinary', '人工连通检查：今天在路边看见一只猫趴在自行车篮里，车主只好推着车慢慢走。随意接一句。'),
    ('bubbles', '人工格式检查：用两个自然的短消息回复，一条说“看到了”，另一条说“这就出发”。'),
    ('technical', '人工技术检查：给出完整可运行的Python函数average(values)，空列表返回None，其他返回平均数；附正常与空列表调用示例。'),
)


async def run(trial_path, output):
    trial = load_trial(trial_path)
    if trial['private']:
        raise ValueError('artificial_probe_only')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = output / 'run.started.json'
    with started.open('x', encoding='utf-8') as stream:
        json.dump({'cases': [x[0] for x in CASES], 'kind': 'real', 'automatic_outer_retry': False}, stream)
    arm = next(x for x in trial['arms'].values() if x['mode'] == 'minimal')
    paths = RuntimePaths(Path(arm['home']))
    engine = ChatEngine(Store(paths), ConfigStore(paths))
    ingress = IngressService(engine)
    rows = []
    engine.cognitive.characters.notify(None)
    try:
        for case, text in CASES:
            row = {'case': case, 'status': 'reserved'}
            atomic_json(output / (case + '.json'), row)
            clock = time.perf_counter()
            result = ingress.accept({'channel': 'desktop', 'text': text, 'request_id': identifier(), 'persona_id': arm['persona_id']})
            batch = ingress.get(result['batch_id'])
            sealed = ingress.action(batch['id'], 'seal', batch['revision'], identifier(), arm['persona_id'])
            await engine.wait()
            batch = ingress.get(result['batch_id'])
            turn = engine.get(batch['turn_id'])
            row.update(status=turn['status'], elapsed_seconds=time.perf_counter()-clock,
                       turn_id=turn['id'], error_code=turn.get('error_code'),
                       reply=turn.get('reply'), display=turn.get('display_reply'))
            with engine.store.connection() as conn:
                attempts = [dict(x) for x in conn.execute('SELECT id,status,error_code,usage_json FROM attempts WHERE turn_id=? ORDER BY rowid', (turn['id'],))]
                manifests = [json.loads(x[0]) for x in conn.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type=\'dialogue\' ORDER BY rowid', (turn['id'],))]
                selections = conn.execute('SELECT count(*) FROM character_selection_attempts WHERE turn_id=?', (turn['id'],)).fetchone()[0]
                observations = conn.execute('SELECT count(*) FROM situation_observation_attempts WHERE turn_id=?', (turn['id'],)).fetchone()[0]
            row.update(attempts=attempts, manifests=manifests, selection_calls=selections, observation_calls=observations)
            atomic_json(output / (case + '.json'), row)
            rows.append(row)
            atomic_json(output / 'summary.json', {'kind': 'real', 'complete': len(rows)==len(CASES) and all(r['status']=='completed' for r in rows),
                'rows': [{k:r[k] for k in ('case','status','elapsed_seconds','error_code','selection_calls','observation_calls')} for r in rows]})
            print(json.dumps({k:row[k] for k in ('case','status','elapsed_seconds','error_code')}, ensure_ascii=False), flush=True)
            if row['status'] != 'completed':
                break
    finally:
        for turn in engine.requests():
            if turn['status'] in ('queued', 'running'):
                await engine.cancel(turn['id'])
        await engine.wait()
        engine.cognitive.characters.close()
        engine.cognitive.shadow.close()
    return len(rows)==len(CASES) and all(row['status']=='completed' for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trial', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.trial, args.output)) else 1)


if __name__ == '__main__':
    main()
