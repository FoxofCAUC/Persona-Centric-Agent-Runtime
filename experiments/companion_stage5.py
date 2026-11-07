"""Stage-5 artificial acceptance through normal ingress and native audited retries."""
import argparse
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

from experiments import companion_stage5_materials as material
from experiments.companion_comparison import local_retrieval_ready, public_model
from experiments.companion_formulaic import offline, close_engine
from experiments.companion_trial import copy_weights, configure_policy
from experiments.companion_workbench_check import configure_local
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.profile import ProfileService, snapshot
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input

VERSION = 'companion-stage5-v1'
ROOT = Path(__file__).resolve().parents[1]
OUT = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-5AB-independent-multiturn'
TEMP = DEFAULT_HOME / 'tmp/companion-stage5'


def require(condition, code):
    if not condition:
        raise RuntimeErrorBase('stage5_' + code)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    paths = list((ROOT / 'persona_runtime').rglob('*.py'))
    paths += [Path(__file__), ROOT / 'experiments/companion_stage5_materials.py',
              ROOT / 'scripts/companion_stage5.py']
    paths += [ROOT / 'experiments' / name for name in
              ('companion_comparison.py', 'companion_formulaic.py', 'companion_trial.py', 'companion_workbench_check.py')]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def positions():
    result = []
    for index, scene in enumerate(material.INDEPENDENT):
        for role in (('A', 'B') if index % 2 == 0 else ('B', 'A')):
            result.append({'id': scene['id'] + '-' + role, 'role': role, 'scene': scene})
    result += [{'id': f'T{i:02}', 'role': 'B', 'scene': {'inputs': [text], 'history': []}}
               for i, text in enumerate(material.MULTITURN, 1)]
    return result


def binding(paths, pid):
    with Store(paths).connection() as db:
        profile = snapshot(db, pid)
        character = dict(db.execute('SELECT v.* FROM character_heads h JOIN character_versions v ON v.id=h.version_id '
                                    'WHERE h.persona_id=?', (pid,)).fetchone())
        policy = dict(db.execute("SELECT p.* FROM interaction_heads h JOIN interaction_policies p ON p.id=h.version_id "
                                 "WHERE h.persona_id=? AND h.channel='desktop'", (pid,)).fetchone())
    return fingerprint({'profiles': profile, 'character': character, 'policy': policy})


def idle(paths):
    with Store(paths).connection() as db:
        counts = {table: db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + condition).fetchone()[0]
                  for table, condition in (
                      ('turns', "status IN ('queued','running')"),
                      ('input_batches', "state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')"),
                      ('quota_reservations', "state='active'"))}
    require(not any(counts.values()), 'unfinished_work')
    return counts


def history_hash(paths):
    with Store(paths).connection() as db:
        return {table: fingerprint([dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')])
                for table in ('events', 'turns', 'attempts', 'input_batches', 'input_batch_members',
                              'reply_batches', 'reply_segments', 'reply_display_messages',
                              'context_manifests', 'quota_reservations', 'memories')}


def verify(out=OUT, *, require_runtime=True):
    out = Path(out).resolve()
    plan = read(out / 'freeze.json')
    owner = read(out / 'owner.json')
    home = Path(owner['temporary_root']).resolve()
    require(home.parent == TEMP.resolve() and home != DEFAULT_HOME.resolve(), 'isolation_scope')
    require(plan['version'] == VERSION and fingerprint(plan) == owner['freeze_hash'], 'freeze_changed')
    require(plan['code_hashes'] == code_hashes(), 'source_changed')
    if require_runtime:
        require(read(home / 'ownership.json') == {'version': VERSION, 'out': str(out), 'artificial_only': True},
                'ownership_changed')
        source = ConfigStore(RuntimePaths(Path(owner['source_home']))).load()
        require(fingerprint(source['model']) == owner['source_model_hash'], 'source_model_changed')
        for mode in ('simulation', 'real'):
            paths = RuntimePaths(home / mode)
            cfg = ConfigStore(paths).load()
            require(fingerprint(cfg['model']) == owner['homes'][mode]['model_hash'], 'model_changed')
            for role, pid in owner['homes'][mode]['personas'].items():
                require(binding(paths, pid) == owner['homes'][mode]['bindings'][role], 'persona_or_policy_changed')
    return owner, plan


async def make_home(home, source, *, simulated, fixture):
    paths = RuntimePaths(home)
    cfg = ConfigStore(paths)
    cfg.initialize()
    value = cfg.load()
    value['model'] = deepcopy(source['model'])
    if simulated:
        value['model']['encrypted_key'] = ''
    value['character_selector'].update(enabled=False, encrypted_key='')
    value['organization'].update(daily_enabled=False, reuse_chat=False)
    value['organization']['model']['encrypted_key'] = ''
    value['interaction']['cancel_queued'] = True
    atomic_json(cfg.file, value)
    store = Store(paths)
    store.initialize()
    service = RuntimeService(store)
    personas = {}
    for role in ('A', 'B'):
        pid = service.create_persona(PersonaSpec.from_dict(material.persona(role)))['id']
        service.use_persona(pid)
        profiles = ProfileService(store)
        current = profiles.current('relationship')
        profiles.update('relationship', {'description': '普通朋友，双方没有预设共同事件。'}, current['id'], identifier())
        characters = CharacterService(store)
        characters.update(pid, material.character(), characters.current(pid)['id'], identifier())
        configure_policy(store, cfg, pid, 'minimal_im', background_prepare=False)
        personas[role] = pid
    if not fixture:
        copy_weights(RuntimePaths(DEFAULT_HOME), paths)
        engine = ChatEngine(store, cfg, SimulationClient())
        try:
            configure_local(engine, paths)
            for pid in personas.values():
                local_retrieval_ready(engine.cognitive.characters, pid, ['资料室'], build=True)
        finally:
            await close_engine(engine)
    return {'personas': personas, 'bindings': {r: binding(paths, p) for r, p in personas.items()},
            'model_hash': fingerprint(cfg.load()['model'])}


async def prepare(out=OUT, source_home=DEFAULT_HOME, *, fixture=False):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    require(not (out / 'freeze.json').exists(), 'already_prepared')
    source = ConfigStore(RuntimePaths(Path(source_home))).load()
    require(fixture or source['model']['name'] == 'deepseek-flash', 'model_route_not_approved')
    require(fixture or bool(source['model']['encrypted_key']), 'model_credential_missing')
    root = TEMP / identifier()
    root.mkdir(parents=True)
    atomic_json(root / 'ownership.json', {'version': VERSION, 'out': str(out), 'artificial_only': True})
    homes = {}
    with offline():
        for mode in ('simulation', 'real'):
            homes[mode] = await make_home(root / mode, source, simulated=mode == 'simulation', fixture=fixture)
    plan = {'version': VERSION, 'created_at': timestamp(), 'positions': positions(),
            'review': material.REVIEW, 'code_hashes': code_hashes(), 'model_public': public_model(source['model']),
            'profile': choose('minimal_im'), 'fixture': fixture, 'planned_positions': 44}
    atomic_json(out / 'freeze.json', plan)
    atomic_json(out / 'evaluation-only.json', material.EXPECTATIONS)
    atomic_json(out / 'owner.json', {'temporary_root': str(root), 'freeze_hash': fingerprint(plan), 'homes': homes,
                                  'source_home': str(Path(source_home).resolve()), 'source_model_hash': fingerprint(source['model'])})
    return {'status': 'prepared', 'out': str(out), 'real_calls': 0}


class SimulationClient:
    async def complete(self, model, messages):
        return '嗯，刚才那件小事还挺有意思的\n我也会多看两眼', {'total_tokens': 1, 'mock': True}


class RecordingClient:
    def __init__(self, out, mode, inner):
        self.out, self.mode, self.inner = Path(out), mode, inner
        self.position = None
        self.seed_reply = None

    async def complete(self, model, messages):
        if self.seed_reply is not None:
            return self.seed_reply, {'total_tokens': 1, 'mock': True}
        verify(self.out)
        folder = self.out / self.mode / 'positions' / self.position
        calls = folder / 'calls'
        calls.mkdir(exist_ok=True)
        path = calls / (identifier() + '.json')
        wire = build_request(model, messages)[0]
        row = {'id': path.stem, 'status': 'reserved', 'started_at': timestamp(), 'mode': self.mode,
               'messages_hash': fingerprint(messages), 'wire_hash': fingerprint(wire)}
        atomic_json(path, row)
        try:
            result = await self.inner.complete(model, messages)
            row.update(status='response_received', finished_at=timestamp(), raw_response=result[0],
                       reported_usage=result[1], diagnostics=getattr(result, 'diagnostics', {}))
            atomic_json(path, row)
            return result
        except BaseException as exc:
            code = str(exc)
            row.update(status='interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                       finished_at=timestamp(), error=code if re.fullmatch(r'[a-z_0-9]+', code) else type(exc).__name__,
                       reported_usage=getattr(exc, 'usage', None))
            atomic_json(path, row)
            raise


class Observer:
    def __init__(self, out, mode, client):
        self.out, self.mode, self.client = Path(out), mode, client

    def get(self, *args):
        return None

    def before_dispatch(self, engine, turn, prepared):
        verify(self.out)
        require(not self.client.position or self.client.seed_reply is None, 'seed_position_conflict')
        if self.client.seed_reply is not None:
            return
        require(self.client.position is not None, 'unowned_dispatch')
        require(json.loads(turn['context_policy_snapshot'])['companion'] == choose('minimal_im'), 'profile_mismatch')
        model = json.loads(turn['model_snapshot'])
        model.update(_reply_mode=turn['reply_mode_snapshot'], _expression_snapshot=turn['expression_snapshot'])
        wire, contract = build_request(model, prepared['messages'])
        require(prepared['manifest']['input_snapshot_hash'] == fingerprint(prepared['messages']), 'manifest_hash_mismatch')
        if not read(self.out / 'freeze.json')['fixture']:
            retrieval = prepared['manifest'].get('character_retrieval') or {}
            require(retrieval.get('effective_mode') == 'hybrid' and not retrieval.get('degrade_reason'), 'unexpected_retrieval_fallback')
        folder = self.out / self.mode / 'positions' / self.client.position
        atomic_json(folder / 'request.json', {'messages': prepared['messages'], 'model': public_model(model),
                    'wire': wire, 'contract': contract, 'messages_hash': fingerprint(prepared['messages']),
                    'wire_hash': fingerprint(wire)})
        atomic_json(folder / 'manifest.json', prepared['manifest'])


async def admit(engine, ingress, pid, inputs, reference=None):
    batch_id = None
    for index, text in enumerate(inputs):
        value = {'channel': 'desktop', 'persona_id': pid, 'text': text, 'request_id': identifier()}
        if index == 0 and reference:
            value['reference'] = {'kind': 'event', 'id': reference}
        receipt = ingress.accept(value)
        require(batch_id in (None, receipt['batch_id']), 'batch_split')
        batch_id = receipt['batch_id']
    batch = ingress.get(batch_id)
    ingress.action(batch_id, 'seal', batch['revision'], identifier(), pid)
    await engine.wait()
    turn = engine.get(ingress.get(batch_id)['turn_id'])
    require(turn['status'] == 'completed', 'position_' + turn['status'] + '_' + str(turn.get('error_code')))
    return turn


def checkpoint(paths):
    return {'idle': idle(paths), 'history_hash': history_hash(paths)}


def completed_record(paths, turn, folder, inputs, *, calls=None):
    with Store(paths).connection() as db:
        raw = dict(db.execute('SELECT * FROM turns WHERE id=?', (turn['id'],)).fetchone())
        members = load_turn_input(db, raw)
        attempt = dict(db.execute('SELECT * FROM attempts WHERE turn_id=? ORDER BY rowid DESC LIMIT 1', (turn['id'],)).fetchone())
        manifest = json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND attempt_id=?',
                                        (turn['id'], attempt['id'])).fetchone()[0])
        auxiliaries = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                       for t in ('character_selection_attempts', 'situation_observation_attempts', 'organization_attempts')}
    require([m['text'] for m in members] == inputs, 'input_members_mismatch')
    require(not any(auxiliaries.values()), 'hidden_auxiliary')
    request = read(folder / 'request.json')
    require(manifest['input_snapshot_hash'] == request['messages_hash'], 'persisted_manifest_mismatch')
    audits = json.loads(attempt['usage_json'] or '{}').get('generation_retry', {}).get('attempts', [])
    calls = calls if calls is not None else [read(p) for p in (folder / 'calls').glob('*.json')]
    require(len(calls) == len(audits) and bool(calls), 'physical_count_mismatch')
    require(all(c['status'] != 'reserved' and c['wire_hash'] == request['wire_hash'] and c['messages_hash'] == request['messages_hash'] for c in calls), 'call_receipt_mismatch')
    require(all(a['request_hash'] == request['wire_hash'] for a in audits), 'native_audit_mismatch')
    received = [c for c in calls if c['status'] == 'response_received']
    for call in received:
        digest = hashlib.sha256(call['raw_response'].encode('utf-8')).hexdigest()
        require(any(a.get('output_fingerprint') == digest for a in audits), 'response_audit_mismatch')
    require(audits[-1]['status'] == 'completed', 'native_terminal_mismatch')
    result = {'status': 'completed', 'turn_id': turn['id'], 'session_id': turn['session_id'],
              'inputs': inputs, 'reply': turn['reply'], 'input_event_ids': [m['id'] for m in members],
              'physical_calls': len(calls), 'retries': len(calls) - 1, 'usage': turn.get('usage'),
              'manifest_matches': True, 'wire_matches': True, 'native_audit_matches': True,
              'profile': json.loads(raw['context_policy_snapshot'])['companion'], 'auxiliaries': auxiliaries,
              'native_attempts': audits, 'reply_provider': turn.get('context_details', {}).get('reply_provider', {})}
    atomic_json(folder / 'result.json', result)
    return result


async def execute(out=OUT, *, simulated=False, client=None):
    out = Path(out).resolve()
    owner, plan = verify(out)
    require(simulated or not plan['fixture'], 'fixture_real_execution_forbidden')
    if not simulated:
        require(read(out / 'simulation/summary.json')['status'] == 'completed', 'simulation_required')
        require(read(out / 'preflight.json')['passed'], 'preflight_required')
    mode = 'simulation' if simulated else 'real'
    target = out / mode
    require(not (target / 'started.json').exists(), 'no_automatic_resume')
    target.mkdir(exist_ok=True)
    paths = RuntimePaths(Path(owner['temporary_root']) / mode)
    idle(paths)
    atomic_json(target / 'started.json', {'at': timestamp(), 'automatic_resume': False})
    recording = RecordingClient(out, mode, client or (SimulationClient() if simulated else ChatCompletionsClient()))
    engine = None
    results = []
    memory_id = None
    multi_session = None

    def progress(status, error=None):
        atomic_json(target / 'progress.json', {'at': timestamp(), 'status': status, 'completed': len(results),
                    'planned': 44, 'positions': [r['id'] for r in results], 'error': error, 'automatic_resume': False})

    def start():
        new = ChatEngine(Store(paths), ConfigStore(paths), recording)
        new.evaluation_session = Observer(out, mode, recording)
        if not plan['fixture']:
            configure_local(new, paths)
            for pid in owner['homes'][mode]['personas'].values():
                local_retrieval_ready(new.cognitive.characters, pid, ['资料室'], build=False)
        return new

    try:
        with offline() if simulated else nullcontext():
            engine = start()
            ingress = IngressService(engine)
            progress('running')
            for item in plan['positions']:
                verify(out)
                recording.position = None
                pid = owner['homes'][mode]['personas'][item['role']]
                engine.service.use_persona(pid)
                folder = target / 'positions' / item['id']
                folder.mkdir(parents=True, exist_ok=False)
                scene = item['scene']
                if item['id'].startswith('V') or item['id'] == 'T01':
                    engine.service.create_session()
                reference = None
                synthetic = []
                for user, reply in scene['history']:
                    recording.seed_reply = reply
                    prior = await admit(engine, ingress, pid, [user])
                    with engine.store.connection() as db:
                        event = db.execute("SELECT id FROM events WHERE session_id=? AND role='user' ORDER BY seq DESC LIMIT 1", (prior['session_id'],)).fetchone()[0]
                    synthetic.append({'turn_id': prior['id'], 'kind': 'authored_synthetic_history', 'real_calls': 0})
                    reference = reference or event
                recording.seed_reply = None
                if synthetic:
                    atomic_json(folder / 'synthetic-history.json', synthetic)
                recording.position = item['id']
                atomic_json(folder / 'admission.json', {'status': 'admitting', 'at': timestamp(), 'inputs': scene['inputs']})
                turn = await admit(engine, ingress, pid, scene['inputs'], reference if scene.get('reference') else None)
                result = completed_record(paths, turn, folder, scene['inputs'])
                result['id'] = item['id']
                results.append(result)
                if item['id'] == 'T01':
                    multi_session = turn['session_id']
                if item['id'].startswith('T'):
                    require(turn['session_id'] == multi_session, 'multiturn_session_changed')
                if item['id'] in ('T03', 'T15'):
                    text = '看展偏好先摄影，后绘画。' if item['id'] == 'T03' else '看展偏好先绘画，后摄影。'
                    if memory_id is None:
                        memory = engine.service.add_memory('preference', text, result['input_event_ids'][0])
                    else:
                        memory = engine.service.revise_memory(memory_id, text, result['input_event_ids'][0])
                    memory_id = memory['id']
                    atomic_json(folder / 'manual-memory-operation.json', {'kind': 'explicit_artificial_memory_save' if item['id'] == 'T03' else 'explicit_artificial_memory_revision', 'result': memory, 'auxiliary_calls': 0})
                progress('running')
                if item['id'] == 'V01-B':
                    atomic_json(target / 'smoke.json', {'completed_positions': [r['id'] for r in results], 'passed': True})
                if item['id'] == 'T10':
                    before = checkpoint(paths)
                    await close_engine(engine)
                    engine = None
                    require(checkpoint(paths) == before, 'close_changed_history')
                    atomic_json(target / 'reopen-checkpoint.json', before)
                    require(checkpoint(paths) == before, 'reopen_precheck_changed')
                    engine = start()
                    ingress = IngressService(engine)
                    require(checkpoint(paths) == before, 'startup_replayed_or_changed_history')
                    atomic_json(target / 'reopen-result.json', {'same_session': multi_session, 'history_equal': True, 'new_generation_calls': 0})
            idle(paths)
            summary = {'version': VERSION, 'status': 'completed', 'completed': len(results),
                       'physical_calls': sum(r['physical_calls'] for r in results) if not simulated else 0,
                       'simulated_calls': sum(r['physical_calls'] for r in results) if simulated else 0,
                       'retries': sum(r['retries'] for r in results), 'auxiliary_calls': 0,
                       'user_review': 'pending' if not simulated else 'not_applicable', 'positions': results}
            atomic_json(target / 'summary.json', summary)
            progress('completed')
            return summary
    except BaseException as exc:
        progress('stopped_no_automatic_resume', str(exc) if re.fullmatch(r'[a-z0-9_]+', str(exc)) else type(exc).__name__)
        raise
    finally:
        if engine is not None:
            await close_engine(engine)


def report(out=OUT):
    out = Path(out)
    rows = {p.parent.name: read(p) for p in (out / 'real/positions').glob('*/result.json')}
    pieces = ['# 阶段5六段关键交流审阅', '', '本批为单臂人工角色验证。请逐段回复认可、出戏或无法判断，并说明整体是否愿继续交流。', '']
    for number, ids in material.REVIEW:
        pieces += ['## ' + number, '']
        if ids[0].startswith('T'):
            start = max(1, int(ids[0][1:]) - 2)
            context = [f'T{i:02}' for i in range(start, int(ids[0][1:]))]
        else:
            context = []
        for key in context + ids:
            row = rows.get(key)
            if row is None:
                pieces += [key + ' 未完成', '']
                continue
            pieces += [('前文 ' if key in context else '') + key, '',
                       '用户\n\n' + '\n\n'.join(row['inputs']), '', '人物\n\n' + row['reply'], '']
    pieces += ['全部44个位置的输入、回复和调用记录保存在本目录的全量人工结果文件中。', '']
    path = out / 'analysis/六段关键交流审阅.md'
    path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(pieces), encoding='utf-8')
    atomic_json(path.parent / '全量人工结果.json', rows)
    all_text = ['# 阶段5全部人工结果', '', '人工预写前文、真实模型回复分开标识；不包含私人人物资料。', '']
    ledger = []
    for item in positions():
        key = item['id']
        all_text += ['## ' + key, '']
        for user, assistant in item['scene']['history']:
            all_text += ['人工前文（非真实生成）', '', '用户：' + user, '', '助手：' + assistant, '']
        row = rows.get(key)
        all_text += ['用户：' + '\n\n'.join(item['scene']['inputs']), '',
                     '人物：' + row['reply'] if row else '未完成', '']
        for receipt in sorted((out / 'real/positions' / key / 'calls').glob('*.json')):
            call = read(receipt)
            ledger.append({k: v for k, v in call.items() if k != 'raw_response'} | {'position': key})
    (path.parent / '全量人工结果.md').write_text('\n'.join(all_text), encoding='utf-8')
    atomic_json(path.parent / '逐次调用总账.json', ledger)
    return {'review_file': str(path), 'completed': len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'execute', 'report', 'verify'))
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args()
    if args.action == 'prepare':
        value = asyncio.run(prepare(args.out))
    elif args.action in ('simulate', 'execute'):
        value = asyncio.run(execute(args.out, simulated=args.action == 'simulate'))
        value = {k: v for k, v in value.items() if k != 'positions'}
    elif args.action == 'report':
        value = report(args.out)
    else:
        _, plan = verify(args.out)
        value = {'version': plan['version'], 'verified': True}
    print(json.dumps(value, ensure_ascii=False))
