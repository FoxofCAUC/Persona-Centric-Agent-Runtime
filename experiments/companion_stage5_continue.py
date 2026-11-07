"""Explicit continuation of stage 5; original requests and unknown receipts stay immutable."""
import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from experiments import companion_stage5 as base

VERSION = 'companion-stage5-continuation-v1'
NAME = 'continuation-v1'


def hashes():
    return {str(p.relative_to(base.ROOT)): base.sha(p) for p in
            (Path(__file__), base.ROOT / 'scripts/companion_stage5_continue.py')}


def folder(out):
    return Path(out) / NAME


def verify(out, simulated=False, resuming=False):
    owner, original = base.verify(out)
    plan = base.read(folder(out) / 'freeze.json')
    receipt = base.read(folder(out) / 'freeze-receipt.json')
    base.require(base.fingerprint(plan) == receipt['hash'], 'continuation_changed')
    if resuming:
        restart = base.read(folder(out) / 'resumption-1/freeze.json')
        proof = base.read(folder(out) / 'resumption-1/freeze-receipt.json')
        base.require(base.fingerprint(restart) == proof['hash'] and restart['code_hashes'] == hashes(), 'resumption_changed')
        base.require(restart['parent_hash'] == base.fingerprint(plan), 'resumption_parent_changed')
        for key, digest in plan['code_hashes'].items():
            base.require(base.sha(folder(out) / 'executed-source' / key) == digest, 'executed_source_archive_changed')
        for name, digest in restart['preserved_files'].items():
            base.require(base.sha(Path(out) / name) == digest, 'saved_success_changed')
        plan = plan | {'all_positions': plan['positions'], 'positions': restart['positions'],
                       'simulation_home': restart['simulation_home']}
    else:
        base.require(plan['code_hashes'] == hashes(), 'continuation_source_changed')
    base.require(base.sha(Path(out) / 'real/pause-checkpoint.json') == plan['pause_hash'], 'pause_checkpoint_changed')
    for name, digest in plan['preserved_files'].items():
        base.require(base.sha(Path(out) / name) == digest, 'old_receipt_changed')
    if simulated:
        paths = base.RuntimePaths(Path(owner['temporary_root']) / (NAME + '-resumption-1' if resuming else NAME))
        home = plan['simulation_home']
        base.require(base.fingerprint(base.ConfigStore(paths).load()['model']) == home['model_hash'], 'simulation_model_changed')
        for role, pid in home['personas'].items():
            base.require(base.binding(paths, pid) == home['bindings'][role], 'simulation_binding_changed')
    return owner, original, plan


async def prepare(out=base.OUT, *, supplement_unknown=False):
    out = Path(out)
    owner, original = base.verify(out)
    dest = folder(out)
    base.require(not dest.exists(), 'continuation_already_prepared')
    pause = base.read(out / 'real/pause-checkpoint.json')
    base.require(pause['status'] == 'paused_by_user_waiting_provider_recovery', 'not_paused')
    paths = base.RuntimePaths(Path(owner['temporary_root']) / 'real')
    base.idle(paths)
    originals = {x['id']: x for x in original['positions']}
    items = []
    unknown = pause['interrupted_unknown']
    if supplement_unknown:
        base.require(len(unknown) == 1, 'supplement_unknown_scope')
        item = deepcopy(originals[unknown[0]['position']])
        item['canonical_id'] = item['id']
        item['id'] += '-supplement-1'
        item['authorization'] = 'user_explicit_new_admission_after_unknown_receipt'
        items.append(item)
    items += [deepcopy(originals[key]) for key in pause['never_dispatched']]
    for item in items:
        base.require(not (out / 'real/positions' / item['id']).exists(), 'position_already_dispatched')
        item.setdefault('canonical_id', item['id'])
    preserved = {p.relative_to(out).as_posix(): base.sha(p) for p in (out / 'real/positions').rglob('*') if p.is_file()}
    simulation_paths = base.RuntimePaths(Path(owner['temporary_root']) / NAME)
    base.require(not simulation_paths.home.exists(), 'continuation_simulation_exists')
    with base.offline():
        sim = await base.make_home(simulation_paths.home, base.ConfigStore(paths).load(),
                                   simulated=True, fixture=original['fixture'])
    dest.mkdir()
    plan = {'version': VERSION, 'created_at': base.timestamp(), 'positions': items,
            'supplement_unknown': supplement_unknown, 'original_unknown': unknown,
            'original_completed': pause['completed_positions'], 'preserved_files': preserved,
            'pause_hash': base.sha(out / 'real/pause-checkpoint.json'), 'code_hashes': hashes(),
            'simulation_home': sim, 'planned_new_positions': len(items),
            'original_freeze_hash': base.fingerprint(original), 'automatic_resume': False}
    base.atomic_json(dest / 'freeze.json', plan)
    base.atomic_json(dest / 'freeze-receipt.json', {'hash': base.fingerprint(plan)})
    return {'prepared': True, 'positions': len(items), 'real_calls': 0}


class Client(base.RecordingClient):
    resuming = False
    async def complete(self, model, messages):
        verify(self.out, self.mode != 'real', self.resuming)
        return await super().complete(model, messages)


class Observer(base.Observer):
    def before_dispatch(self, engine, turn, prepared):
        verify(self.out, self.mode != 'real', self.client.resuming)
        super().before_dispatch(engine, turn, prepared)


def combined(out, original, plan):
    rows = []
    supplements = {x['canonical_id']: x['id'] for x in plan.get('all_positions', plan['positions']) if x['canonical_id'] != x['id']}
    for item in original['positions']:
        effective = supplements.get(item['id'], item['id'])
        path = Path(out) / 'real/positions' / effective / 'result.json'
        if path.exists():
            row = base.read(path)
            rows.append(row | {'id': effective, 'canonical_id': item['id']})
    receipts = [base.read(p) for p in (Path(out) / 'real/positions').glob('*/calls/*.json')]
    return {'version': VERSION, 'status': 'completed' if len(rows) == 44 else 'incomplete',
            'completed': len(rows), 'positions': rows, 'physical_calls': len(receipts),
            'retries': sum(x['retries'] for x in rows), 'unknown_original_calls': plan['original_unknown'],
            'auxiliary_calls': 0, 'user_review': 'pending', 'qq_calls': 0}


async def prepare_resumption(out=base.OUT):
    out = Path(out)
    owner, original = base.verify(out)
    plan = base.read(folder(out) / 'freeze.json')
    base.require(base.fingerprint(plan) == base.read(folder(out) / 'freeze-receipt.json')['hash'], 'continuation_changed')
    dest = folder(out) / 'resumption-1'
    base.require(not dest.exists(), 'resumption_already_prepared')
    base.require((folder(out) / 'real/started.json').exists(), 'resumption_requires_started_parent')
    base.idle(base.RuntimePaths(Path(owner['temporary_root']) / 'real'))
    for key, digest in plan['code_hashes'].items():
        base.require(base.sha(folder(out) / 'executed-source' / key) == digest, 'executed_source_archive_changed')
    remaining = []
    for item in plan['positions']:
        position = out / 'real/positions' / item['id']
        if (position / 'result.json').exists():
            base.require(not item['id'].startswith('T'), 'multiturn_resume_needs_separate_reconciliation')
            continue
        base.require(not position.exists(), 'incomplete_position_requires_reconciliation')
        remaining.append(item)
    preserved = {p.relative_to(out).as_posix(): base.sha(p) for p in (out / 'real/positions').rglob('*') if p.is_file()}
    paths = base.RuntimePaths(Path(owner['temporary_root']) / (NAME + '-resumption-1'))
    base.require(not paths.home.exists(), 'resumption_simulation_exists')
    with base.offline():
        sim = await base.make_home(paths.home,
            base.ConfigStore(base.RuntimePaths(Path(owner['temporary_root']) / 'real')).load(),
            simulated=True, fixture=original['fixture'])
    dest.mkdir()
    restart = {'parent_hash': base.fingerprint(plan), 'code_hashes': hashes(),
               'positions': remaining, 'preserved_files': preserved, 'simulation_home': sim,
               'reason': 'explicit continuation after progress-only write failure; no successful call replay',
               'at': base.timestamp()}
    base.atomic_json(dest / 'freeze.json', restart)
    base.atomic_json(dest / 'freeze-receipt.json', {'hash': base.fingerprint(restart)})
    return {'prepared': True, 'remaining': len(remaining), 'real_calls': 0}


async def execute(out=base.OUT, *, simulated=False, client=None, resuming=False):
    out = Path(out)
    owner, original, plan = verify(out, simulated, resuming)
    base.require(simulated or not original['fixture'], 'fixture_real_execution_forbidden')
    if not simulated:
        gate = folder(out) / ('resumption-1' if resuming else '')
        base.require(base.read(gate / 'simulation/summary.json')['status'] == 'completed', 'continuation_simulation_required')
        base.require(base.read(out / 'preflight.json')['passed'], 'preflight_required')
    mode = (NAME + '-resumption-1' if resuming else NAME) if simulated else 'real'
    target = folder(out) / ('resumption-1' if resuming else '') / ('simulation' if simulated else 'real')
    base.require(not (target / 'started.json').exists(), 'continuation_no_automatic_resume')
    target.mkdir(parents=True, exist_ok=True)
    paths = base.RuntimePaths(Path(owner['temporary_root']) / mode)
    base.idle(paths)
    base.atomic_json(target / 'started.json', {'at': base.timestamp(), 'automatic_resume': False})
    recording = Client(out, mode, client or (base.SimulationClient() if simulated else base.ChatCompletionsClient()))
    recording.resuming = resuming
    engine, watcher = None, None
    results = []
    memory_id, multi_session = None, None
    parent = asyncio.current_task()

    async def stop_watch():
        while True:
            if (folder(out) / 'stop-request.json').exists():
                parent.cancel('explicit_stage5_stop_request')
                return
            await asyncio.sleep(.1)

    def progress(status, error=None):
        events = target / 'progress-events'
        events.mkdir(exist_ok=True)
        base.atomic_json(events / (f'{len(results):03}-' + base.identifier() + '.json'), {'at': base.timestamp(), 'status': status,
            'completed_new': len(results), 'planned_new': len(plan['positions']),
            'positions': [x['id'] for x in results], 'error': error, 'automatic_resume': False})

    def start():
        value = base.ChatEngine(base.Store(paths), base.ConfigStore(paths), recording)
        value.evaluation_session = Observer(out, mode, recording)
        if not original['fixture']:
            base.configure_local(value, paths)
            home = plan['simulation_home'] if simulated else owner['homes']['real']
            for pid in home['personas'].values():
                base.local_retrieval_ready(value.cognitive.characters, pid, ['资料室'], build=False)
        return value

    try:
        with base.offline() if simulated else nullcontext():
            watcher = asyncio.create_task(stop_watch())
            engine = start()
            ingress = base.IngressService(engine)
            home = plan['simulation_home'] if simulated else owner['homes']['real']
            for item in plan['positions']:
                verify(out, simulated, resuming)
                recording.position = None
                pid = home['personas'][item['role']]
                engine.service.use_persona(pid)
                dest = out / mode / 'positions' / item['id']
                dest.mkdir(parents=True, exist_ok=False)
                scene = item['scene']
                if item['id'].startswith('V') or item['id'] == 'T01':
                    engine.service.create_session()
                reference, seeds = None, []
                for text, reply in scene['history']:
                    recording.seed_reply = reply
                    prior = await base.admit(engine, ingress, pid, [text])
                    with engine.store.connection() as db:
                        event = db.execute("SELECT id FROM events WHERE session_id=? AND role='user' ORDER BY seq DESC LIMIT 1", (prior['session_id'],)).fetchone()[0]
                    reference = reference or event
                    seeds.append({'turn_id': prior['id'], 'kind': 'authored_synthetic_history', 'real_calls': 0})
                recording.seed_reply = None
                if seeds:
                    base.atomic_json(dest / 'synthetic-history.json', seeds)
                recording.position = item['id']
                base.atomic_json(dest / 'admission.json', {'status': 'admitting', 'at': base.timestamp(),
                    'inputs': scene['inputs'], 'canonical_id': item['canonical_id'],
                    'supplement': item['id'] != item['canonical_id']})
                turn = await base.admit(engine, ingress, pid, scene['inputs'], reference if scene.get('reference') else None)
                row = base.completed_record(paths, turn, dest, scene['inputs'])
                row.update(id=item['id'], canonical_id=item['canonical_id'])
                base.atomic_json(dest / 'result.json', row)
                results.append(row)
                if item['id'] == 'T01':
                    multi_session = turn['session_id']
                if item['id'].startswith('T'):
                    base.require(turn['session_id'] == multi_session, 'multiturn_session_changed')
                if item['id'] in ('T03', 'T15'):
                    text = '看展偏好先摄影，后绘画。' if item['id'] == 'T03' else '看展偏好先绘画，后摄影。'
                    memory = engine.service.add_memory('preference', text, row['input_event_ids'][0]) if memory_id is None else engine.service.revise_memory(memory_id, text, row['input_event_ids'][0])
                    memory_id = memory['id']
                    base.atomic_json(dest / 'manual-memory-operation.json', {'kind': 'explicit_artificial_memory_save' if item['id'] == 'T03' else 'explicit_artificial_memory_revision', 'result': memory, 'auxiliary_calls': 0})
                progress('running')
                if item['id'] == 'T10':
                    before = base.checkpoint(paths)
                    await base.close_engine(engine)
                    engine = None
                    base.require(base.checkpoint(paths) == before, 'close_changed_history')
                    base.atomic_json(target / 'reopen-checkpoint.json', before)
                    engine = start()
                    ingress = base.IngressService(engine)
                    base.require(base.checkpoint(paths) == before, 'startup_replayed_or_changed_history')
                    base.atomic_json(target / 'reopen-result.json', {'same_session': multi_session, 'history_equal': True, 'new_generation_calls': 0})
            base.idle(paths)
            summary = {'version': VERSION, 'status': 'completed', 'completed_new': len(results),
                       'positions': results, 'physical_calls': sum(r['physical_calls'] for r in results),
                       'simulated': simulated, 'auxiliary_calls': 0}
            base.atomic_json(target / 'summary.json', summary)
            progress('completed')
            if not simulated:
                base.atomic_json(out / 'real/summary.json', combined(out, original, plan))
            return summary
    except BaseException as exc:
        progress('stopped_no_automatic_resume', 'cancelled' if isinstance(exc, asyncio.CancelledError) else type(exc).__name__)
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if engine is not None:
            await base.close_engine(engine)


def report(out=base.OUT):
    out = Path(out)
    original = base.read(out / 'freeze.json')
    plan = base.read(folder(out) / 'freeze.json')
    value = base.report(out)
    summary = combined(out, original, plan)
    rows = {r['canonical_id']: r for r in summary['positions']}
    analysis = out / 'analysis'
    base.atomic_json(analysis / 'canonical-results.json', rows)
    text = ['# 阶段5全部人工结果', '',
            'V03甲原调用回执未知；补测是用户明确授权的新受理位置，原记录和计费未知保留。', '']
    for item in original['positions']:
        key = item['id']
        row = rows.get(key)
        text += ['## ' + key, '']
        for user, reply in item['scene']['history']:
            text += ['人工预写前文（非模型生成）', '', '用户：' + user, '', '助手：' + reply, '']
        text += ['用户：' + '\n\n'.join(item['scene']['inputs']), '',
                 '人物：' + row['reply'] if row else '未完成', '']
        if row and row['id'] != key:
            text += ['实际位置：' + row['id'] + '（明确授权补测，原未知请求保留）', '']
    (analysis / '全量人工结果.md').write_text('\n'.join(text), encoding='utf-8')
    contexts = {'01': [], '02': [], '03': ['T01', 'T02', 'T03', 'T04'],
                '04': ['T01', 'T02', 'T06', 'T09', 'T10'], '05': ['T03', 'T13', 'T14'],
                '06': ['T17', 'T18']}
    review = ['# 阶段5六段关键交流审阅', '',
              '这是单臂人工角色验证。请逐段回复“认可、出戏或无法判断”，再说明整体是否愿继续交流。', '',
              '两个人工角色的身份、知识和普通朋友关系相同，没有预设共同事件。甲更关注过程和条件；乙更坦率表达立场与取舍。01为甲，其余为乙。', '']
    for number, keys in base.material.REVIEW:
        review += ['## ' + number, '']
        for key in contexts[number] + keys:
            row = rows.get(key)
            if row is None:
                review += [key + ' 未完成', '']
                continue
            review += [('前文 ' if key not in keys else '审阅 ') + key, '',
                       '用户\n\n' + '\n\n'.join(row['inputs']), '', '人物\n\n' + row['reply'], '']
    review += ['全部结果可查看同目录《全量人工结果.md》。真实调用时间和用量单列，本文不代表长期陪伴已验证。', '']
    (analysis / '六段关键交流审阅.md').write_text('\n'.join(review), encoding='utf-8')
    return value | {'completed_effective_positions': len(rows), 'physical_calls': summary['physical_calls']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'execute', 'report', 'verify',
                                          'prepare-resume', 'simulate-resume', 'resume'))
    parser.add_argument('--out', type=Path, default=base.OUT)
    parser.add_argument('--supplement-unknown', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare-resume':
        value = asyncio.run(prepare_resumption(args.out))
    elif args.action == 'prepare':
        value = asyncio.run(prepare(args.out, supplement_unknown=args.supplement_unknown))
    elif args.action in ('simulate', 'execute', 'simulate-resume', 'resume'):
        value = asyncio.run(execute(args.out, simulated=args.action.startswith('simulate'),
                                   resuming=args.action in ('simulate-resume', 'resume')))
        value = {k: v for k, v in value.items() if k != 'positions'}
    elif args.action == 'report':
        value = report(args.out)
    else:
        verify(args.out, resuming=(folder(args.out) / 'resumption-1/freeze.json').exists())
        value = {'verified': True}
    print(base.json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
