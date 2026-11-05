"""One bounded history-style diagnosis through normal isolated runtime requests.

No production import, service, QQ connection, or automatic paid continuation.
The external trial directory holds ownership only; all bodies stay in its copy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_trial import configure, configure_policy, copy_weights
from experiments.companion_comparison import local_retrieval_ready, public_model
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient, ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, provider_identity, safe
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import SessionService
from persona_runtime.storage import Store

VERSION = 'companion-formulaic-diagnosis-v1'
NAMESPACE = 'companion-formulaic'
BODY_DIRECTORY = 'runtime/formulaic-diagnosis'


def materials():
    from experiments import companion_formulaic_materials
    return companion_formulaic_materials


def error_code(error):
    value = str(error)
    return value if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', value) else type(error).__name__


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def offline():
    # Enter only after asyncio has created its loop; do not break socketpair.
    with patch('socket.socket.connect', side_effect=RuntimeErrorBase('formulaic_network_disabled')), \
            patch('socket.create_connection', side_effect=RuntimeErrorBase('formulaic_network_disabled')), \
            patch('asyncio.BaseEventLoop.create_connection', side_effect=RuntimeErrorBase('formulaic_network_disabled')), \
            patch.object(ChatCompletionsClient, 'complete', side_effect=RuntimeErrorBase('formulaic_real_client_disabled')):
        yield


def source_hashes():
    files = list((ROOT / 'persona_runtime').rglob('*.py'))
    files += [Path(__file__), ROOT / 'experiments/companion_trial.py',
              ROOT / 'experiments/companion_comparison.py',
              ROOT / 'experiments/companion_formulaic_materials.py',
              ROOT / 'experiments/companion_formulaic_audit.py']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in files}


def body_root(owner):
    return safe(Path(owner['home']) / BODY_DIRECTORY, owner['home'])


def load_trial(root):
    root = safe(root)
    owner = read(root / 'trial.json')
    if owner.get('version') != VERSION:
        raise RuntimeErrorBase('formulaic_version_mismatch')
    source = Path(owner['source_home']).resolve()
    safe(root, source / 'tmp' / NAMESPACE)
    home = safe(owner['home'], source / 'tmp/private-acceptance')
    marker = read(home / 'private-sandbox.json')
    if marker.get('persona_id') != owner['persona_id'] or Path(marker['formal_home']).resolve() != source:
        raise RuntimeErrorBase('formulaic_owner_changed')
    if owner.get('status') != 'prepared':
        raise RuntimeErrorBase('formulaic_not_prepared')
    if datetime.now(timezone.utc) > datetime.fromisoformat(owner['review_deadline']):
        raise RuntimeErrorBase('formulaic_review_expired')
    return owner


def current_binding(paths, persona_id):
    from persona_runtime.profile import snapshot
    from persona_runtime.character import head
    store = Store(paths)
    with store.connection() as conn:
        if store.current(conn) != persona_id:
            raise RuntimeErrorBase('formulaic_persona_changed')
        profiles = snapshot(conn, persona_id)
        character = head(conn, persona_id)
    return {'profile_hash': fingerprint(profiles), 'character_hash': fingerprint(character),
            'config_hash': fingerprint(ConfigStore(paths).load()),
            'permission_hash': sha(paths.home / 'runtime/private-copy-policy.json'),
            'owner_hash': sha(paths.home / 'private-sandbox.json')}


def verify(root):
    owner = load_trial(root)
    data = body_root(owner)
    plan = read(data / 'plan.json')
    if fingerprint(plan) != owner['plan_hash']:
        raise RuntimeErrorBase('formulaic_plan_changed')
    if source_hashes() != plan['source_hashes']:
        raise RuntimeErrorBase('formulaic_source_changed')
    if current_binding(RuntimePaths(Path(owner['home'])), owner['persona_id']) != plan['binding']:
        raise RuntimeErrorBase('formulaic_conditions_changed')
    for position in plan['positions']:
        folder = data / 'preflight' / position['position_id']
        for filename, expected in position['frozen_files'].items():
            if sha(folder / filename) != expected:
                raise RuntimeErrorBase('formulaic_frozen_request_changed')
    validate_pairs(data, plan['positions'])
    return {'owner': owner, 'plan': plan, 'passed': True, 'real_calls': 0}


def validate_pairs(data, positions):
    """Only the two assistant history messages may differ within each pair."""
    pairs = {}
    for row in positions:
        pairs.setdefault(row['pair_id'], []).append(row)
    reports = []
    for pair, rows in pairs.items():
        if len(rows) != 2 or {r['arm'] for r in rows} != {'fixed', 'natural'}:
            raise RuntimeErrorBase('formulaic_pair_invalid')
        requests = [read(data / 'preflight' / r['position_id'] / 'request.json') for r in rows]
        left, right = requests
        a, b = left['messages'], right['messages']
        if len(a) != 6 or len(b) != 6 or [m['role'] for m in a] != ['system','user','assistant','user','assistant','user']:
            raise RuntimeErrorBase('formulaic_history_not_exact_two_turns')
        changes = [i for i in range(6) if a[i] != b[i]]
        if changes != [2, 4] or [m['role'] for m in b] != [m['role'] for m in a]:
            raise RuntimeErrorBase('formulaic_unexplained_message_difference')
        if ({k:v for k,v in left['wire'].items() if k != 'messages'} !=
                {k:v for k,v in right['wire'].items() if k != 'messages'}
                or left['model'] != right['model'] or left['selected_materials'] != right['selected_materials']):
            raise RuntimeErrorBase('formulaic_unexplained_condition_difference')
        reports.append({'pair_id': pair, 'changed_message_indexes': changes,
                        'only_assistant_history_changed': True})
    return reports


class FixtureClient:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    async def complete(self, model, messages):
        self.calls.append({'messages': deepcopy(messages), 'wire': build_request(model, messages)[0]})
        text = self.replies.pop(0) if self.replies else '人工离线回包仅验证接线，不评价自然度。'
        return ModelCompletion(json.dumps({'schema_version':3, 'behavior_intent':'respond',
            'messages':[{'content':text, 'content_kind':'chat', 'cancel_if_new_input':False}]}, ensure_ascii=False),
            None, {'finish_reason':'stop'})


def selected_materials(prepared):
    value = prepared['details'].get('character') or {}
    return {key: deepcopy(value.get(key)) for key in ('version_id','revision','fact_paths','lore_ids','example_ids')}


def request_record(turn, prepared):
    model = json.loads(turn['model_snapshot'])
    model.update(_reply_mode=turn['reply_mode_snapshot'], _expression_snapshot=turn['expression_snapshot'])
    wire, transport = build_request(model, prepared['messages'])
    return {'messages': deepcopy(prepared['messages']), 'model': public_model(model),
            'wire': wire, 'transport': transport, 'messages_hash': fingerprint(prepared['messages']),
            'wire_hash': fingerprint(wire), 'selected_materials': selected_materials(prepared)}


def assert_runtime(engine, pid, prepared, *, development):
    value = prepared['manifest'].get('character_retrieval') or {}
    if not development and (value.get('effective_mode') != 'hybrid' or value.get('degrade_reason')):
        raise RuntimeErrorBase('formulaic_lexical_fallback_forbidden')
    with engine.store.connection() as conn:
        for table in ('character_selection_attempts','situation_observation_attempts','organization_attempts'):
            if conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]:
                raise RuntimeErrorBase('formulaic_hidden_auxiliary')


async def admit(engine, ingress, pid, text):
    receipt = ingress.accept({'channel':'desktop','text':text,'request_id':identifier(),'persona_id':pid})
    batch = ingress.get(receipt['batch_id'])
    ingress.action(batch['id'], 'seal', batch['revision'], identifier(), pid)
    await engine.wait()
    turn = engine.get(ingress.get(batch['id'])['turn_id'])
    return turn


def seal_session(engine, pid, session_id):
    service = SessionService(engine.store)
    current = service.show(session_id, pid)
    result = service.seal(session_id, pid, current['revision'], identifier())
    if result['state'] != 'sealed':
        raise RuntimeErrorBase('formulaic_session_not_closed')


async def seed_history(engine, ingress, pid, case, arm, folder):
    engine.client = FixtureClient(case['histories'][arm])
    rows = []
    with offline():
        for user, expected in zip(case['users'], case['histories'][arm]):
            turn = await admit(engine, ingress, pid, user)
            if turn['status'] != 'completed' or turn['reply'] != expected:
                raise RuntimeErrorBase('formulaic_synthetic_history_failed')
            rows.append({'turn_id':turn['id'], 'session_id':turn['session_id'],
                         'kind':'synthetic_authored_history', 'real_calls':0})
    atomic_json(folder / 'synthetic-history.json', rows)
    return rows[-1]['session_id']


def capture_hook(folder, expected=None, *, development=False):
    captured = {}
    def before_dispatch(engine, turn, prepared):
        assert_runtime(engine, turn['persona_id'], prepared, development=development)
        request = request_record(turn, prepared)
        if expected is not None and request != expected:
            raise RuntimeErrorBase('formulaic_frozen_input_mismatch')
        assembly = prepared['manifest'].get('prompt_assembly') or {}
        if assembly.get('input_snapshot_hash') != request['messages_hash']:
            raise RuntimeErrorBase('formulaic_manifest_input_mismatch')
        from experiments.companion_formulaic_audit import audit_reachability, private_prompt_sources
        atomic_json(folder / 'reachability.json', audit_reachability(engine, turn, prepared,
                    selection=prepared.get('character_prepared')))
        atomic_json(folder / 'prompt-sources.json', private_prompt_sources(turn, prepared))
        atomic_json(folder / 'request.json', request)
        atomic_json(folder / 'manifest.json', prepared['manifest'])
        captured.update(turn_id=turn['id'], request=request)
    # Observe dispatch only; do not activate the historical three-arm material
    # transform consulted by descriptor()/variant() during normal preparation.
    return SimpleNamespace(before_dispatch=before_dispatch, get=lambda *args: None), captured


async def close_engine(engine):
    for row in engine.requests():
        if row['status'] in ('queued','running'):
            await engine.cancel(row['id'])
    await engine.wait()
    engine.cognitive.characters.close()
    engine.cognitive.shadow.close()


async def prepare(source_home=DEFAULT_HOME, *, private=False, approve_current_provider=False):
    implementation = source_hashes()
    source_paths = RuntimePaths(Path(source_home).resolve())
    source = deepcopy(ConfigStore(source_paths).load())
    if not private:
        for field in ('model','character_selector'):
            source[field]['encrypted_key'] = ''
    registry = PrivatePersonas(source_paths)
    records = [r for r in registry.load(True)['records'] if r.get('state') == 'ready'] if private else []
    if private and len(records) != 1:
        raise RuntimeErrorBase('formulaic_one_private_role_required')
    if private and not approve_current_provider and records[0].get('approved_models',{}).get('main') != provider_identity(source['model']):
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    parent = safe(source_paths.home / 'tmp' / NAMESPACE, source_paths.home)
    root = parent / identifier(); root.mkdir(parents=True)
    owner = {'version':VERSION, 'status':'preparing', 'source_home':str(source_paths.home),
             'private':bool(private), 'created_at':timestamp(), 'cleanup_homes':[],
             'review_deadline':(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()}
    atomic_json(root / 'trial.json', owner)
    engine = None
    try:
        if private:
            copied = registry.create_sandbox(records[0]['id'])
            paths, pid = RuntimePaths(Path(copied['home'])), copied['persona_id']
        else:
            paths = RuntimePaths(source_paths.home / 'tmp/private-acceptance' / identifier())
            store = Store(paths); store.initialize()
            service = RuntimeService(store); pid = service.create_persona(generic_persona())['id']; service.use_persona(pid)
            atomic_json(paths.home / 'private-sandbox.json', {'formal_home':str(source_paths.home),
                        'persona_id':pid,'private_id':'artificial-formulaic-fixture'})
        owner.update(home=str(paths.home), persona_id=pid, cleanup_homes=[str(paths.home)])
        atomic_json(root / 'trial.json', owner)
        config = configure(paths, source, pid)
        atomic_json(paths.home / 'runtime/private-copy-policy.json', {'persona_id':pid,
                    'approved_models':{'main':provider_identity(source['model'])},
                    'approval_scope':'this_formulaic_diagnosis_only'})
        if private:
            copy_weights(source_paths, paths)
        store = Store(paths)
        configure_policy(store, config, pid, 'minimal_structural', background_prepare=False)
        engine = ChatEngine(store, config, client=FixtureClient())
        ingress = IngressService(engine)
        policy = ingress.current_policy(pid, 'desktop')
        if json.loads(policy['data_json']).get('context',{}).get('cross_session'):
            raise RuntimeErrorBase('formulaic_cross_session_forbidden')
        data = body_root(owner); data.mkdir(parents=True, exist_ok=True)
        cases = {c['id']: c for c in materials().CASES}
        positions = []
        with offline():
            local_retrieval_ready(engine.cognitive.characters, pid,
                [c['current_input'] for c in cases.values()], build=True, development_material=not private)
            for item in materials().POSITIONS:
                row = deepcopy(item); case = cases[row['case_id']]
                folder = data / 'preflight' / row['position_id']; folder.mkdir(parents=True)
                engine.evaluation_session = None
                session = await seed_history(engine, ingress, pid, case, row['arm'], folder)
                hook, captured = capture_hook(folder, development=not private)
                engine.evaluation_session = hook; engine.client = FixtureClient()
                turn = await admit(engine, ingress, pid, case['current_input'])
                if turn['status'] != 'completed' or not captured:
                    atomic_json(folder / 'preflight-failure.json', turn)
                    failure = RuntimeErrorBase('formulaic_preflight_failed')
                    failure.preparation_reason = turn.get('error_code')
                    raise failure
                if engine.client.calls[0]['wire'] != captured['request']['wire']:
                    raise RuntimeErrorBase('formulaic_client_input_mismatch')
                atomic_json(folder / 'synthetic-target.json', {'kind':'synthetic_preflight_target',
                    'turn_id':turn['id'],'session_id':session,'real_calls':0})
                seal_session(engine, pid, session)
                row['frozen_files'] = {name:sha(folder/name) for name in
                    ('request.json','manifest.json','reachability.json','prompt-sources.json')}
                positions.append(row)
        checks = validate_pairs(data, positions)
        if implementation != source_hashes():
            raise RuntimeErrorBase('formulaic_source_changed_during_preparation')
        plan = {'version':VERSION, 'positions':positions, 'cases':list(cases.values()),
                'order':[r['position_id'] for r in positions], 'pair_checks':checks,
                'source_hashes':implementation, 'binding':current_binding(paths,pid),
                'preflight_kind':'synthetic_only_new_sessions', 'planned_real_positions':12,
                'independent_samples':2, 'semantic_quality':'not_evaluated'}
        atomic_json(data / 'plan.json', plan)
        atomic_json(data / 'state.json', {'status':'prepared','automatic_resume':False})
        owner.update(status='prepared',plan_hash=fingerprint(plan),planned_real_positions=12)
        atomic_json(root / 'trial.json', owner)
        verify(root)
        return root
    except BaseException as error:
        owner.update(status='preparation_blocked',error_code=error_code(error))
        atomic_json(root / 'trial.json', owner)
        error.preparation_trial_root = str(root)
        raise
    finally:
        if engine is not None:
            await close_engine(engine)


async def run_position(root, position_id, *, simulated=False, client=None):
    owner = load_trial(root)
    if simulated == bool(owner['private']):
        raise RuntimeErrorBase('formulaic_simulation_scope_mismatch')
    if not simulated and client is not None:
        raise RuntimeErrorBase('formulaic_real_client_injection_forbidden')
    lease = InstanceLock(Path(root) / 'execution.lock')
    try:
        with offline() if simulated else nullcontext():
            return await _run_position(root, position_id, simulated=simulated, client=client)
    finally:
        lease.close()


async def _run_position(root, position_id, *, simulated, client):
    checked = verify(root); owner, plan = checked['owner'], checked['plan']
    data = body_root(owner); state = read(data/'state.json')
    if state['status'] == 'stopped' or (data/'cancel.json').exists():
        raise RuntimeErrorBase('formulaic_batch_stopped')
    completed = []
    for prior in plan['order']:
        folder = data/'positions'/prior
        if not (folder/'started.json').exists(): break
        if not (folder/'terminal.json').is_file():
            raise RuntimeErrorBase('formulaic_unfinished_no_auto_resume')
        if read(folder/'terminal.json')['status'] != 'completed':
            raise RuntimeErrorBase('formulaic_batch_stopped')
        completed.append(prior)
    if position_id in completed:
        raise RuntimeErrorBase('formulaic_success_already_completed')
    if len(completed) >= 12 or position_id != plan['order'][len(completed)]:
        raise RuntimeErrorBase('formulaic_order_mismatch')
    row = next(p for p in plan['positions'] if p['position_id']==position_id)
    case = next(c for c in plan['cases'] if c['id']==row['case_id'])
    folder = data/'positions'/position_id; folder.mkdir(parents=True,exist_ok=True)
    with (folder/'started.json').open('x',encoding='utf-8') as handle:
        json.dump({'kind':'simulated' if simulated else 'real','at':timestamp()},handle)
    result = {k:row[k] for k in ('position_id','pair_id','case_id','sample','arm')}
    result.update(kind='simulated' if simulated else 'real',status='running')
    atomic_json(folder/'result.json',result)
    engine=None; audit={}; watcher=None; started=time.perf_counter(); captured={}; dispatches=0
    try:
        paths=RuntimePaths(Path(owner['home'])); store=Store(paths); config=ConfigStore(paths)
        engine=ChatEngine(store,config,client=FixtureClient()); ingress=IngressService(engine)
        local_retrieval_ready(engine.cognitive.characters,owner['persona_id'],[case['current_input']],
            build=False,development_material=simulated)
        session=await seed_history(engine,ingress,owner['persona_id'],case,row['arm'],folder)
        expected=read(data/'preflight'/position_id/'request.json')
        hook,captured=capture_hook(folder,expected,development=simulated)
        engine.evaluation_session=hook

        def save(value):
            nonlocal audit
            audit=deepcopy(value)
            for attempt in audit.get('generation_retry',{}).get('attempts',[]):
                reserve=folder/(attempt['id']+'.reserve.json')
                if not reserve.exists():
                    if attempt['status']!='running':raise RuntimeErrorBase('formulaic_reservation_missing')
                    with reserve.open('x',encoding='utf-8') as stream:json.dump({'kind':result['kind'],'attempt':attempt},stream)
                atomic_json(folder/(attempt['id']+'.attempt.json'),attempt)
            atomic_json(folder/'audit.json',audit)
        engine.generation_audit_sink=save
        target_client=client or (FixtureClient() if simulated else ChatCompletionsClient())
        class CheckedClient:
            async def complete(self,model,messages):
                nonlocal dispatches
                verify(root)
                if (data/'cancel.json').exists():raise asyncio.CancelledError('user_cancelled')
                wire=build_request(model,messages)[0]
                if messages!=expected['messages'] or wire!=expected['wire']:
                    raise RuntimeErrorBase('formulaic_dispatch_changed')
                attempts=audit.get('generation_retry',{}).get('attempts',[])
                if not attempts:raise RuntimeErrorBase('formulaic_audit_missing')
                atomic_json(folder/(attempts[-1]['id']+'.dispatch.json'),
                    {'kind':result['kind'],'attempt_id':attempts[-1]['id'],'started_at':timestamp(),
                     'wire_hash':fingerprint(wire),'receipt_status':'not_yet_received'})
                dispatches+=1
                value=await target_client.complete(model,messages)
                atomic_json(folder/(attempts[-1]['id']+'.response.json'),
                    {'kind':result['kind'],'raw':value[0],'usage':value[1],
                     'diagnostics':getattr(value,'diagnostics',{})})
                return value
        engine.client=CheckedClient()
        async def cancel_watch():
            while True:
                if (data/'cancel.json').exists():
                    for value in engine.requests():
                        if value['status'] in ('queued','running'):await engine.cancel(value['id'])
                    return
                await asyncio.sleep(.05)
        watcher=asyncio.create_task(cancel_watch())
        turn=await admit(engine,ingress,owner['persona_id'],case['current_input'])
        result.update(turn_id=turn['id'],session_id=session,status=turn['status'],
            error_code=turn.get('error_code'),reply=turn.get('reply'),display=turn.get('display_reply'),
            display_messages=turn.get('display_messages'),context=turn.get('context'))
        if turn['status']!='completed':raise RuntimeErrorBase(turn.get('error_code') or 'formulaic_target_failed')
        assert_runtime(engine,owner['persona_id'],{'manifest':read(folder/'manifest.json')},development=simulated)
        result.update(messages_hash=expected['messages_hash'],wire_hash=expected['wire_hash'],
            diagnostics=(turn.get('context') or {}).get('reply_provider',{}))
        seal_session(engine,owner['persona_id'],session)
    except BaseException as error:
        cancelled=isinstance(error,asyncio.CancelledError) or (data/'cancel.json').exists()
        result.update(status='cancelled' if cancelled else 'failed',error_code=error_code(error))
        atomic_json(data/'state.json',{'status':'stopped','position_id':position_id,'error_code':result['error_code'],'automatic_resume':False})
    finally:
        if watcher:
            watcher.cancel()
            try:await watcher
            except asyncio.CancelledError:pass
        if engine is not None:await close_engine(engine)
    attempts=audit.get('generation_retry',{}).get('attempts',[])
    result.update(usage=audit,generation_attempts=len(attempts),physical_calls=0 if simulated else dispatches,
        simulated_calls=dispatches if simulated else 0,elapsed_seconds=time.perf_counter()-started)
    atomic_json(folder/'result.json',result)
    atomic_json(folder/'terminal.json',{k:result.get(k) for k in
        ('position_id','kind','status','error_code','physical_calls','simulated_calls')})
    return result


async def execute(root, *, simulated=False, client=None):
    checked=verify(root); plan=checked['plan']; data=body_root(checked['owner'])
    # A new invocation cannot quietly continue a partially run batch.
    if any((data/'positions').glob('*/started.json')):
        raise RuntimeErrorBase('formulaic_batch_started_no_auto_resume')
    rows=[]
    for position_id in plan['order']:
        row=await run_position(root,position_id,simulated=simulated,client=client);rows.append(row)
        if row['status']!='completed':break
    return report(root)


def report(root):
    owner=load_trial(root);data=body_root(owner);plan=read(data/'plan.json')
    rows=[]
    for position in plan['positions']:
        folder=data/'positions'/position['position_id']
        if (folder/'terminal.json').is_file():
            rows.append(read(folder/'result.json'))
            continue
        row={k:position[k] for k in ('position_id','case_id','pair_id','arm','sample')}
        if (folder/'started.json').is_file():
            kind=read(folder/'started.json')['kind']
            audit=read(folder/'audit.json') if (folder/'audit.json').is_file() else {}
            count=len(list(folder.glob('*.dispatch.json')))
            row.update(status='unfinished',kind=kind,physical_calls=count if kind=='real' else 0,
                simulated_calls=count if kind=='simulated' else 0,usage=audit,
                reserved_attempts=len(list(folder.glob('*.reserve.json'))),
                response_receipts=len(list(folder.glob('*.response.json'))),
                result_status='unknown_until_audited; no_automatic_resume')
        else:row.update(status='not_executed',physical_calls=0,simulated_calls=0)
        rows.append(row)
    output={'version':VERSION,'planned_positions':12,'completed':sum(r['status']=='completed' for r in rows),
        'physical_calls':sum(r.get('physical_calls',0) for r in rows),
        'simulated_calls':sum(r.get('simulated_calls',0) for r in rows),
        'unfinished_positions':sum(r['status']=='unfinished' for r in rows),
        'real_effect':'not_evaluated','rows':rows}
    atomic_json(data/'report.json',output)
    (data/'匿名三组审核.md').write_text(materials().render_anonymous(rows),encoding='utf-8')
    if not (data/'machine-review.json').exists():
        atomic_json(data/'machine-review.json',materials().machine_report_skeleton(rows))
    return {k:v for k,v in output.items() if k!='rows'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','simulate','run','report','verify'))
    parser.add_argument('trial',nargs='?',type=Path)
    parser.add_argument('--source-home',type=Path,default=DEFAULT_HOME)
    parser.add_argument('--private',action='store_true')
    parser.add_argument('--approve-current-provider',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            root=asyncio.run(prepare(args.source_home,private=args.private,approve_current_provider=args.approve_current_provider))
            output={'status':'prepared','trial':str(root),'real_calls':0}
        elif args.action in ('run','simulate'):
            output=asyncio.run(execute(args.trial,simulated=args.action=='simulate'))
        elif args.action=='report':output=report(args.trial)
        else:verify(args.trial);output={'status':'verified','real_calls':0}
        print(json.dumps(output,ensure_ascii=False))
    except BaseException as error:
        print(json.dumps({'status':'blocked','error_code':error_code(error),
            'preparation_trial_root':getattr(error,'preparation_trial_root',None)},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__':main()
