"""One frozen 5-B.1 comparison; normal ingress, native retries, append-only progress."""
import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import re

from experiments import companion_stage5 as shared
from experiments import companion_stage5_materials as material
from experiments.companion_comparison import public_model, local_retrieval_ready
from experiments.companion_formulaic import offline, close_engine
from experiments.companion_trial import configure_policy, copy_weights
from experiments.companion_workbench_check import configure_local
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.companion_im import GUIDANCE as OLD_GUIDANCE
from persona_runtime.companion_selective_reply import GUIDANCE
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.profile import ProfileService
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store

VERSION = 'companion-selective-reply-v1'
OUT = shared.OUT.parent / 'stage-5B1-selective-reply'
TEMP = DEFAULT_HOME / 'tmp/companion-selective-reply'
MODES = {'v8': 'minimal_im', 'v9': 'minimal_im_selective'}


def require(value, code):
    if not value:
        raise RuntimeErrorBase('selective_' + code)


def write(path, value):
    """Never replace progress, dispatch receipts, or a successful position."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()


def code_hashes():
    result = shared.code_hashes()
    for folder in ('experiments', 'scripts'):
        for path in (shared.ROOT / folder).glob('companion*.py'):
            result[path.relative_to(shared.ROOT).as_posix()] = shared.sha(path)
    return result


def scenes():
    rows = shared.read(shared.OUT / 'analysis/canonical-results.json')
    # Canonical evidence is already artificial, never private conversation.
    def replay(target):
        return [{'inputs': rows[f'T{i:02}']['inputs'], 'reply': rows[f'T{i:02}']['reply'],
                 'original_turn_id': rows[f'T{i:02}']['turn_id'], 'source': 'sealed_artificial_history_replay'}
                for i in range(1, target)]
    result = [{'id': f'R{n:02}', 'inputs': rows[f'T{t:02}']['inputs'], 'history': replay(t),
               'source': f'stage5-T{t:02}'} for n, t in enumerate((11, 12, 19, 20), 1)]
    result += [dict(id='R05', inputs=['我准备把一面旧软木板改成照片墙，只有一个下午，预算八十元。请给我两个可执行的布置方案，说清材料、步骤和取舍，帮我选一个。'],
                    history=[], source='new_authored_explicit_advice'),
               dict(id='R06', inputs=[material.MULTITURN[8]], history=[], source='stage5-T09-complete-code-task')]
    return result


def normalized(request):
    value = deepcopy(request)
    messages = value['messages']
    # Only normalize the exact, registered guidance in its single first-system prefix.
    text = messages[0]['content']
    count = text.count(OLD_GUIDANCE) + text.count(GUIDANCE)
    require(count == 1, 'instruction_count')
    messages[0]['content'] = text.replace(GUIDANCE, OLD_GUIDANCE, 1)
    return {'messages': messages, 'wire': value['wire'] | {'messages': messages}}


def verify(out=OUT):
    out = Path(out).resolve()
    owner, plan = shared.read(out / 'owner.json'), shared.read(out / 'freeze.json')
    root = Path(owner['temporary_root']).resolve()
    require(root.parent == TEMP.resolve() and root != DEFAULT_HOME.resolve(), 'isolation_scope')
    require(shared.read(root / 'ownership.json') == {'version': VERSION, 'out': str(out), 'artificial_only': True}, 'ownership')
    require(plan['version'] == VERSION and fingerprint(plan) == owner['freeze_hash'], 'freeze_changed')
    require(plan['code_hashes'] == code_hashes(), 'source_changed')
    require(plan['previous_evidence_hashes'] == {p: shared.sha(shared.OUT / p) for p in plan['previous_evidence_hashes']}, 'old_evidence_changed')
    source = ConfigStore(RuntimePaths(Path(owner['source_home']))).load()
    require(fingerprint(source['model']) == owner['source_model_hash'], 'source_model_changed')
    for mode, entry in owner['homes'].items():
        paths = RuntimePaths(root / mode)
        require(fingerprint(ConfigStore(paths).load()['model']) == entry['model_hash'], 'model_changed')
        for arm, pid in entry['personas'].items():
            require(shared.binding(paths, pid) == entry['bindings'][arm], 'persona_or_policy_changed')
    return owner, plan


async def prepare(out=OUT, source_home=DEFAULT_HOME, *, fixture=False):
    out = Path(out).resolve()
    require(not (out / 'freeze.json').exists(), 'already_prepared')
    source = ConfigStore(RuntimePaths(Path(source_home))).load()
    require(fixture or (source['model']['name'] == 'deepseek-flash' and source['model']['encrypted_key']), 'unapproved_model')
    root = TEMP / identifier()
    root.mkdir(parents=True)
    if not fixture:
        from persona_runtime.character_embedding import private_directory
        private_directory(DEFAULT_HOME, root)
    write(root / 'ownership.json', {'version': VERSION, 'out': str(out), 'artificial_only': True})
    frozen_scenes = scenes()
    positions = [{'id': s['id'] + '-' + arm, 'arm': arm, 'scene': s} for n,s in enumerate(frozen_scenes)
                 for arm in (('v8','v9') if n % 2 == 0 else ('v9','v8'))]
    homes = {}
    with offline():
        for mode in ('simulation', 'real'):
            paths = RuntimePaths(root / mode)
            cfg = ConfigStore(paths); cfg.initialize(); value = cfg.load()
            value['model'] = deepcopy(source['model'])
            if mode == 'simulation':
                value['model']['encrypted_key'] = ''
            value['character_selector'].update(enabled=False, encrypted_key='')
            value['organization'].update(daily_enabled=False, reuse_chat=False)
            value['organization']['model']['encrypted_key'] = ''
            from persona_runtime.config import atomic_json
            atomic_json(cfg.file, value)
            store = Store(paths); store.initialize(); service = shared.RuntimeService(store)
            personas = {}
            for item in positions:
                arm, policy = item['id'], MODES[item['arm']]
                pid = service.create_persona(PersonaSpec.from_dict(material.persona('B')))['id']
                service.use_persona(pid)
                profiles = ProfileService(store)
                profiles.update('relationship', {'description': '普通朋友，双方没有预设共同事件。'},
                                profiles.current('relationship')['id'], identifier())
                characters = CharacterService(store)
                characters.update(pid, material.character(), characters.current(pid)['id'], identifier())
                configure_policy(store, cfg, pid, policy, background_prepare=False)
                personas[arm] = pid
            if not fixture:
                copy_weights(RuntimePaths(DEFAULT_HOME), paths)
                engine = ChatEngine(store, cfg, shared.SimulationClient())
                try:
                    configure_local(engine, paths)
                    for pid in personas.values():
                        local_retrieval_ready(engine.cognitive.characters, pid, ['资料室'], build=True)
                finally:
                    await close_engine(engine)
            homes[mode] = {'personas': personas, 'bindings': {a: shared.binding(paths, p) for a,p in personas.items()},
                           'model_hash': fingerprint(cfg.load()['model'])}
    mapping = {s['id']: {'A': ('v8' if n % 2 == 0 else 'v9'), 'B': ('v9' if n % 2 == 0 else 'v8')} for n,s in enumerate(frozen_scenes)}
    write(out / 'review-mapping.json', mapping)
    plan = {'version': VERSION, 'positions': positions, 'fixture': fixture, 'guidance': GUIDANCE,
            'code_hashes': code_hashes(), 'model_public': public_model(source['model']),
            'previous_evidence_hashes': shared.read(out / 'opening.json')['old_evidence_hashes'],
            'evaluation': {'R01':'具体接话而非泛化道理', 'R02':'不自动转成办法', 'R03':'自然暂停，无多余照顾提醒',
                           'R04':'污染历史下自然道别', 'R05':'两个实用方案、材料步骤取舍与推荐',
                           'R06':'完整实现、边界处理和可运行示例'}, 'numeric_preference_gate': False}
    write(out / 'freeze.json', plan)
    write(out / 'owner.json', {'temporary_root': str(root), 'freeze_hash': fingerprint(plan), 'homes': homes,
                             'source_home': str(Path(source_home).resolve()), 'source_model_hash': fingerprint(source['model'])})
    verify(out)
    return {'prepared': True, 'positions': len(positions), 'real_calls': 0}


class Recorder:
    def __init__(self, out, mode, inner):
        self.out, self.mode, self.inner = Path(out), mode, inner
        self.position, self.seed = None, None

    async def complete(self, model, messages):
        if self.seed is not None:
            return self.seed, {'mock': True, 'total_tokens': 1}
        verify(self.out)
        folder = self.out / self.mode / 'positions' / self.position / 'calls' / identifier()
        row = {'id': folder.name, 'status': 'reserved', 'started_at': timestamp(), 'mode': self.mode,
               'messages_hash': fingerprint(messages), 'wire_hash': fingerprint(build_request(model, messages)[0])}
        write(folder / 'reserved.json', row)
        try:
            result = await self.inner.complete(model, messages)
            row.update(status='response_received', finished_at=timestamp(), raw_response=result[0], reported_usage=result[1],
                       diagnostics=getattr(result, 'diagnostics', {}))
            write(folder / 'terminal.json', row)
            return result
        except BaseException as exc:
            code = str(exc)
            row.update(status='interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                       finished_at=timestamp(), error=code if re.fullmatch('[a-z0-9_]+', code) else type(exc).__name__,
                       reported_usage=getattr(exc, 'usage', None))
            if not (folder / 'terminal.json').exists():
                write(folder / 'terminal.json', row)
            raise


class Observer:
    def __init__(self, recorder, plan):
        self.client, self.plan = recorder, plan

    def get(self, *args):
        return None

    def before_dispatch(self, engine, turn, prepared):
        client = self.client
        verify(client.out)
        if client.seed is not None:
            return
        item = next(p for p in self.plan['positions'] if p['id'] == client.position)
        require(json.loads(turn['context_policy_snapshot'])['companion'] == choose(MODES[item['arm']]), 'profile_mismatch')
        model = json.loads(turn['model_snapshot']) | {'_reply_mode': turn['reply_mode_snapshot'], '_expression_snapshot': turn['expression_snapshot']}
        wire, contract = build_request(model, prepared['messages'])
        request = {'messages': prepared['messages'], 'model': public_model(model), 'wire': wire, 'contract': contract,
                   'messages_hash': fingerprint(prepared['messages']), 'wire_hash': fingerprint(wire)}
        require(prepared['manifest']['input_snapshot_hash'] == request['messages_hash'], 'manifest_mismatch')
        if not self.plan['fixture']:
            retrieval = prepared['manifest'].get('character_retrieval') or {}
            require(retrieval.get('effective_mode') == 'hybrid' and not retrieval.get('degrade_reason'), 'retrieval_fallback')
        folder = client.out / client.mode / 'positions' / client.position
        if client.mode == 'real':
            require(normalized(request) == normalized(shared.read(client.out / 'simulation/positions' / client.position / 'request.json')), 'real_request_drift')
        # Retries must use the original, unchanged assembled request.
        if (folder / 'request.json').exists():
            require(shared.read(folder / 'request.json') == request, 'retry_request_drift')
        else:
            write(folder / 'request.json', request)
            write(folder / 'manifest.json', prepared['manifest'])


def calls(folder):
    result = []
    for p in (folder / 'calls').glob('*/reserved.json'):
        require((p.parent / 'terminal.json').exists(), 'unknown_receipt')
        result.append(shared.read(p.parent / 'terminal.json'))
    return result


def pair_check(out, mode):
    rows = []
    for n in range(1, 7):
        prefix = Path(out) / mode / 'positions' / f'R{n:02}'
        a = shared.read(prefix.with_name(prefix.name+'-v8') / 'request.json')
        b = shared.read(prefix.with_name(prefix.name+'-v9') / 'request.json')
        require(normalized(a) == normalized(b), f'pair_R{n:02}_confounded')
        require(a['messages'] != b['messages'], 'candidate_not_applied')
        rows.append({'pair': f'R{n:02}', 'only_registered_guidance_differs': True})
    return rows


async def execute(out=OUT, *, simulated=False, client=None):
    out = Path(out).resolve(); owner, plan = verify(out)
    mode = 'simulation' if simulated else 'real'; target = out / mode
    require(not (target / 'started.json').exists(), 'no_automatic_resume')
    if not simulated:
        require(not plan['fixture'], 'fixture_real_forbidden')
        require(shared.read(out / 'simulation/summary.json')['status'] == 'completed', 'simulation_required')
        require(shared.read(out / 'preflight.json')['passed'], 'preflight_required')
        pair_check(out, 'simulation')
    write(target / 'started.json', {'at': timestamp(), 'automatic_resume': False})
    paths = RuntimePaths(Path(owner['temporary_root']) / mode)
    recorder = Recorder(out, mode, client or (shared.SimulationClient() if simulated else ChatCompletionsClient()))
    engine = None; results = []
    def progress(status, error=None):
        write(target / 'progress' / (identifier()+'.json'), {'at':timestamp(), 'status':status, 'completed':len(results),
              'positions':[r['id'] for r in results], 'error':error, 'automatic_resume':False})
    try:
        with offline() if simulated else nullcontext():
            engine = ChatEngine(Store(paths), ConfigStore(paths), recorder)
            engine.evaluation_session = Observer(recorder, plan)
            if not plan['fixture']:
                configure_local(engine, paths)
            ingress = IngressService(engine)
            for item in plan['positions']:
                verify(out); recorder.position = None
                pid = owner['homes'][mode]['personas'][item['id']]
                engine.service.use_persona(pid); engine.service.create_session()
                folder = target / 'positions' / item['id']; folder.mkdir(parents=True, exist_ok=False)
                # Replay frozen artificial records through normal admission with a local seed client.
                memory_id = None
                for n, prior in enumerate(item['scene']['history'], 1):
                    recorder.seed = prior['reply']
                    turn = await shared.admit(engine, ingress, pid, prior['inputs'])
                    if n in (3, 15):
                        with engine.store.connection() as db:
                            raw = dict(db.execute('SELECT * FROM turns WHERE id=?', (turn['id'],)).fetchone())
                            eid = shared.load_turn_input(db, raw)[0]['id']
                        if n == 3:
                            memory_id = engine.service.add_memory('preference', '看展偏好先摄影，后绘画。', eid)['id']
                        else:
                            memory_id = engine.service.revise_memory(memory_id, '看展偏好先绘画，后摄影。', eid)['id']
                recorder.seed = None; recorder.position = item['id']
                write(folder / 'admission.json', {'at':timestamp(), 'inputs':item['scene']['inputs'], 'history_kind':'sealed_artificial_replay'})
                turn = await shared.admit(engine, ingress, pid, item['scene']['inputs'])
                result = shared.completed_record(paths, turn, folder, item['scene']['inputs'], calls=calls(folder))
                result['id'] = item['id']; results.append(result); progress('running')
            shared.idle(paths)
            pairs = pair_check(out, mode)
            summary = {'version':VERSION, 'status':'completed', 'positions':results, 'pair_checks':pairs,
                       'completed':len(results), 'physical_calls':0 if simulated else sum(r['physical_calls'] for r in results),
                       'simulated_calls':sum(r['physical_calls'] for r in results) if simulated else 0, 'auxiliary_calls':0}
            write(target / 'summary.json', summary); progress('completed')
            return {k:v for k,v in summary.items() if k not in ('positions','pair_checks')}
    except BaseException as exc:
        progress('stopped_no_automatic_resume', str(exc) if re.fullmatch('[a-z0-9_]+',str(exc)) else type(exc).__name__)
        raise
    finally:
        if engine is not None:
            await close_engine(engine)


def report(out=OUT):
    out = Path(out); plan = shared.read(out / 'freeze.json'); mapping = shared.read(out / 'review-mapping.json')
    text = ['# 六组匿名接话取舍对照', '', '开发偏好复核；请选择A、B、平或两条都不好。数字不是硬通过线。',
            '历史来自阶段5已封存的人工人物对话，不是新生成；每个位置独立续接一次，候选结果未接入下一组。', '']
    for scene in [p['scene'] for p in plan['positions'][::2]]:
        text += ['## '+scene['id'][1:], '']
        for prior in scene['history'][-3:]:
            text += ['前文用户：'+'\n'.join(prior['inputs']), '', '前文人物：'+prior['reply'], '']
        text += ['本轮用户：'+'\n'.join(scene['inputs']), '']
        for arm_label in ('A','B'):
            path = out / 'real/positions' / (scene['id']+'-'+mapping[scene['id']][arm_label]) / 'result.json'
            text += ['### '+arm_label, '', shared.read(path)['reply'] if path.exists() else '未完成', '']
    write(out / 'analysis/report-status.json', {'real_effect':'pending_user_review', 'numeric_hard_gate':False})
    path = out / 'analysis/六组匿名接话取舍对照.md'
    with path.open('x',encoding='utf-8') as stream:
        stream.write('\n'.join(text))
    return {'review_file':str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','simulate','execute','report','verify'))
    parser.add_argument('--out',type=Path,default=OUT)
    args = parser.parse_args()
    if args.action == 'prepare': value = asyncio.run(prepare(args.out))
    elif args.action in ('simulate','execute'): value = asyncio.run(execute(args.out,simulated=args.action=='simulate'))
    else:
        with offline():
            if args.action == 'report': value = report(args.out)
            else: verify(args.out); value = {'verified':True}
    print(json.dumps(value,ensure_ascii=False))
