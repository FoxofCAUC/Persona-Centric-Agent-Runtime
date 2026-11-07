"""Independent supervisor and hidden real Workbench, using normal PCAR HTTP APIs.

Only the supervisor reads the factual answer rubric. The worker sees admitted
user text and normal runtime context. No private persona/database is copied.
"""
import argparse
import asyncio
from contextlib import ExitStack, closing
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from aiohttp import web
from experiments import companion_stage5 as base
from experiments.companion_workbench_check import configure_local
from experiments.companion_workbench_trial import OwnedController
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'pcar-background-acceptance-v2'
ROOT = Path(__file__).resolve().parents[2]
OUT = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-6-memory-repair-20261108'
TEMP = DEFAULT_HOME / 'tmp/pcar-background-acceptance'
read, sha = base.read, base.sha

def require(value, code):
    if not value:
        raise RuntimeErrorBase('background_acceptance_' + code)

def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)

def code_hashes():
    files = list((ROOT / 'persona_runtime').rglob('*.py'))
    files += list((ROOT / 'plugins/background_acceptance').glob('*.py'))
    files += [ROOT / 'experiments' / name for name in (
        'companion_stage5.py', 'companion_stage5_materials.py', 'companion_comparison.py',
        'companion_trial.py', 'companion_formulaic.py', 'companion_workbench_check.py',
        'companion_workbench_trial.py')]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}

def verify(out):
    out = Path(out).resolve()
    owner, plan = read(out / 'owner.json'), read(out / 'plan.json')
    home = Path(owner['home']).resolve()
    require(home.parent == TEMP.resolve(), 'wrong_scope')
    require(read(home / 'ownership.json') == {'version': VERSION, 'out': str(out), 'artificial_only': True}, 'ownership')
    expected_code = owner['code_hashes']
    if (out / 'continuation.json').exists():
        continuation = read(out / 'continuation.json')
        require(continuation['original_owner_hash'] == sha(out / 'owner.json'), 'original_owner_changed')
        require(all(sha(out / p) == digest for p, digest in continuation['preserved'].items()), 'prior_evidence_changed')
        expected_code = continuation['code_hashes']
    require(owner['plan_hash'] == fingerprint(plan) and expected_code == code_hashes(), 'freeze_changed')
    require(all(Path(p).is_file() and sha(p) == digest for p, digest in owner['protected'].items()), 'formal_files_changed')
    cfg = ConfigStore(RuntimePaths(home)).load()
    require(not cfg['bot_qq'] and not cfg['owner_qq'], 'qq_forbidden')
    require(fingerprint(cfg['model']) == owner['model_hash'], 'model_changed')
    require(base.binding(RuntimePaths(home), owner['persona_id']) == owner['binding'], 'persona_changed')
    return owner, plan

async def prepare(out, simulated=False, smoke=False):
    from .materials import dialogue, RESTARTS, PROBES, smoke_dialogue, SMOKE_PROBES
    out = Path(out).resolve()
    require(not (out / 'owner.json').exists(), 'already_prepared')
    source_paths = RuntimePaths(DEFAULT_HOME)
    source = ConfigStore(source_paths).load()
    require(source['model']['name'] == 'deepseek-flash', 'unapproved_model')
    require(source['model']['base_url'].rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'), 'unapproved_provider')
    require(bool(source['model']['encrypted_key']), 'missing_key')
    home = TEMP / identifier()
    home.mkdir(parents=True)
    from persona_runtime.character_embedding import private_directory
    private_directory(DEFAULT_HOME, home)
    # Existing setup creates two entirely authored artificial personas. Only B
    # is used; neither persona is derived from a private registry.
    result = await base.make_home(home, source, simulated=simulated, fixture=simulated)
    paths = RuntimePaths(home)
    cfg = ConfigStore(paths)
    value = cfg.load()
    value['bot_qq'] = value['owner_qq'] = ''
    value['organization'].update(daily_enabled=False, reuse_chat=False, hourly_limit=0, daily_limit=0)
    value['organization']['model'] = deepcopy(source['organization']['model'])
    value['organization']['model']['hourly_limit'] = 0
    if simulated:
        value['organization']['model']['encrypted_key'] = ''
    atomic_json(cfg.file, value)
    pid = result['personas']['B']
    RuntimeService(Store(paths)).use_persona(pid)
    protected = {}
    for name in ('config', 'private', 'data/personas', 'data/database'):
        for p in (DEFAULT_HOME / name).rglob('*'):
            if p.is_file() and not p.name.endswith(('-shm', '.lock')):
                protected[str(p)] = sha(p)
    plan = {'version': VERSION, 'at': timestamp(), 'simulated': simulated,
            'positions': smoke_dialogue() if smoke else dialogue(), 'restarts_after': [3,5] if smoke else list(RESTARTS), 'organization_every': 2 if smoke else 10,
            'scenario': 'correction-smoke' if smoke else 'memory-repair-100',
            'planned_main_replies': 6 if smoke else 100, 'planned_organization_batches': 3 if smoke else 10,
            'chat_retry': 'native maximum three retries; no outer regeneration',
            'memory_write': 'artificial-only reviewed correction v1; unique live target and explicit user correction; actual candidate text; no rubric injection',
            'approve_conflicts_after': 5 if smoke else 20,
            'hidden_real_workbench': True, 'computer_use': False}
    write_new(out / 'plan.json', plan)
    write_new(out / 'evaluation-only.json', SMOKE_PROBES if smoke else PROBES)
    write_new(home / 'ownership.json', {'version': VERSION, 'out': str(out), 'artificial_only': True})
    write_new(out / 'owner.json', {'home': str(home), 'persona_id': pid,
              'plan_hash': fingerprint(plan), 'code_hashes': code_hashes(), 'protected': protected,
              'model_hash': fingerprint(value['model']), 'binding': base.binding(paths, pid)})
    return {'prepared': True, 'simulated': simulated, 'out': str(out)}

class RecordedClient:
    def __init__(self, out, simulated):
        self.out, self.simulated = Path(out), simulated
        self.inner = ChatCompletionsClient()

    async def complete(self, model, messages):
        verify(self.out)
        active = read(self.out / 'active.json')
        folder = self.out / 'calls' / active['id']
        row = {'id': identifier(), 'position': active['id'], 'kind': active['kind'],
               'at': timestamp(), 'status': 'reserved', 'messages': messages,
               'messages_hash': fingerprint(messages), 'wire_hash': fingerprint(build_request(model, messages)[0])}
        path = folder / (row['id'] + '.json')
        write_new(path, row)
        try:
            if self.simulated:
                if active['kind'] == 'organization':
                    payload = json.loads(messages[-1]['content'])
                    e = next(e for e in payload['events'] if e['role'] == 'user')
                    answer = json.dumps({'format_version': 1, 'summary': [
                        {'text': e['text'], 'sources': [{'event_id': e['id'], 'quote': e['text']}]}],
                        'candidates': []}, ensure_ascii=False)
                else:
                    answer = '这件事确实有点意思，我想先听听你最在意的细节。'
                result = answer, {'total_tokens': 1, 'mock': True}
            else:
                result = await self.inner.complete(model, messages)
            row.update(status='completed', finished_at=timestamp(), reply=result[0], usage=result[1])
            atomic_json(path, row)
            return result
        except BaseException as exc:
            row.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                       finished_at=timestamp(), error=base.re.sub('[^a-zA-Z0-9_]', '_', str(exc))[:100],
                       usage=getattr(exc, 'usage', None))
            atomic_json(path, row)
            raise

class Observer:
    def __init__(self, out): self.out = Path(out)
    def get(self, *args): return None
    def before_dispatch(self, engine, turn, prepared):
        verify(self.out)
        active = read(self.out / 'active.json')
        require(active['kind'] == 'chat', 'unexpected_generation')
        require(prepared['manifest']['input_snapshot_hash'] == fingerprint(prepared['messages']), 'manifest')
        atomic_json(self.out / 'requests' / (active['id'] + '.json'),
                    {'turn_id': turn['id'], 'manifest': prepared['manifest'],
                     'messages_hash': fingerprint(prepared['messages'])})

def snapshot(paths):
    idle = base.idle(paths)
    with Store(paths).connection() as db:
        counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                  for t in ('turns', 'attempts', 'organization_attempts', 'memories')}
        require(not db.execute("SELECT 1 FROM organization_tasks WHERE status IN ('queued','running','waiting_quota')").fetchone(), 'organization_pending')
    return {'idle': idle, 'history': base.history_hash(paths), 'counts': counts}

def worker(out, opening, *, verifier=None, client_factory=None, observer_factory=None):
    """A real Tk Workbench plus normal API in a fresh, owned OS process."""
    owner, plan = (verifier or verify)(out)
    out = Path(out)
    paths = RuntimePaths(Path(owner['home']))
    folder = out / 'openings' / str(opening)
    folder.mkdir(parents=True, exist_ok=False)
    before = snapshot(paths)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    state = {}
    async def start():
        app = create_app(paths, (client_factory or RecordedClient)(out, plan['simulated']))
        state['app'] = app
        app['engine'].evaluation_session = (observer_factory or Observer)(out)
        if not plan['simulated'] and plan.get('local_embeddings',True):
            configure_local(app['engine'], paths)
        runner = web.AppRunner(app, access_log=None)
        state['runner'] = runner
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        ConfigStore(paths).update({'service_port': port})
        return port
    window = None
    with ExitStack() as stack:
        for name in ('service', 'desktop'):
            stack.enter_context(closing(InstanceLock(paths.home / 'runtime' / (name + '.lock'))))
        try:
            port = asyncio.run_coroutine_threadsafe(start(), loop).result(120)
            require(snapshot(paths) == before, 'startup_generated_or_changed_history')
            from persona_runtime.desktop import Workbench
            from types import SimpleNamespace
            runtime = SimpleNamespace(running=True)
            window = Workbench(paths, managed_lifecycle=False, controller=OwnedController(runtime))
            window.withdraw()
            window.persona_id = owner['persona_id']
            window.title('PCAR 后台百轮验收')
            def control():
                if (folder / 'close.request').exists():
                    window.close_window()
                else:
                    window.after(100, control)
            window.after(100, control)
            write_new(folder / 'ready.json', {'pid': os.getpid(), 'port': port, 'at': timestamp(),
                       'snapshot': before, 'workbench_constructed': True, 'withdrawn': True,
                       'startup_generation_calls': 0})
            window.mainloop()
            window.hub.pool.shutdown(wait=True, cancel_futures=True)
        finally:
            if state.get('runner'):
                asyncio.run_coroutine_threadsafe(state['runner'].cleanup(), loop).result(120)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(15)
            require(not thread.is_alive(), 'worker_thread_alive')
            loop.close()
    write_new(folder / 'closed.json', {'pid': os.getpid(), 'at': timestamp(), 'snapshot': snapshot(paths),
                                     'tk_destroyed': bool(window and window.closed), 'thread_stopped': True})

def wait_until(fn, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value is not None:
            return value
        require(time.monotonic() < deadline, 'operation_timeout')
        time.sleep(.2)

def spawn_worker(out, opening, *, module='plugins.background_acceptance'):
    folder = out / 'openings' / str(opening)
    log = (out / f'worker-{opening}.log').open('wb')
    env = os.environ | {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1',
                        'TEMP': str(DEFAULT_HOME / 'tmp'), 'TMP': str(DEFAULT_HOME / 'tmp')}
    process = subprocess.Popen([sys.executable, '-B', '-m', module,
            'worker', '--out', str(out), '--opening', str(opening)], cwd=ROOT,
            env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    log.close()
    def ready():
        require(process.poll() is None, 'worker_start_failed')
        return read(folder / 'ready.json') if (folder / 'ready.json').exists() else None
    try:
        proof = wait_until(ready)
        from persona_runtime.lifecycle import process_info
        identity = process_info(proof['pid'])
        require(identity and (proof['pid'] == process.pid or identity['ParentProcessId'] == process.pid), 'worker_pid')
        process.worker_pid = proof['pid']
    except BaseException:
        if process.poll() is None:
            write_new(folder / 'close.request', {'at': timestamp(), 'reason': 'startup_failed'})
            process.wait(timeout=150)
        raise
    return process, proof

def close_worker(out, opening, process, paths):
    before = snapshot(paths)
    folder = out / 'openings' / str(opening)
    write_new(folder / 'close.request', {'at': timestamp()})
    require(process.wait(timeout=150) == 0, 'worker_exit_failed')
    closed = read(folder / 'closed.json')
    require(closed['snapshot'] == before and closed['tk_destroyed'], 'shutdown_changed_history')
    with ExitStack() as stack:
        for name in ('service', 'desktop'):
            stack.enter_context(closing(InstanceLock(paths.home / 'runtime' / (name + '.lock'))))
    # Closed process and released instance locks, not just a replaced Python object.
    write_new(folder / 'exit-verified.json', {'pid': process.pid, 'exit_code': process.returncode,
              'locks_released': True, 'snapshot': before})
    return before

def review_candidates(client, out, number):
    owner, plan = verify(out)
    accepted = []
    for candidate in client.manage('candidate.list', status='pending'):
        detail = client.manage('candidate.show', candidate_id=candidate['id'])
        require(detail['persona_id'] == owner['persona_id'], 'review_persona')
        options = {}
        if detail['conflicts']:
            if number < plan['approve_conflicts_after'] or len(detail['conflicts']) != 1:
                continue
            if not any(e['role']=='user' and any(marker in e['text'] for marker in ('更正','改为','以这次为准')) for e in detail['evidence']):
                continue
            old = client.manage('memory.show', memory_id=detail['conflicts'][0])
            source = client.manage('organization.show', task_id=detail['task_id'])['source']
            frozen = next((m for m in source['existing_memories'] if m['id'] == old['id']), None)
            if not frozen or old['status']!='active' or old['revision']!=frozen['revision'] or old['text']!=frozen['text']:
                continue
            options = {'target_memory_id': old['id'], 'expected_revision': old['revision']}
        operation = identifier()
        write_new(out/'reviews'/(operation+'-intent.json'), {'candidate_id':detail['id'],'number':number,
                  'policy':'artificial-reviewed-correction-v1','options':options,'candidate_text_hash':fingerprint(detail['text'])})
        result = client.manage('candidate.accept', candidate_id=detail['id'], operation_id=operation, **options)
        write_new(out/'reviews'/(operation+'-result.json'), result)
        accepted.append(result)
    return accepted


def organize(client, out, session, number, every=10):
    active = {'id': f'M{number:03}', 'kind': 'organization'}
    atomic_json(out / 'active.json', active)
    preview = client.manage('organization.preview', session_id=session, mode='both',
                            start_row=number - every + 1, end_row=number)
    require(preview['rows'] == list(range(number - every + 1, number + 1)), 'organization_range_incomplete')
    request_id = identifier()
    write_new(out / 'organization' / (active['id'] + '-intent.json'), {'request_id': request_id, 'preview': preview})
    task = client.manage('organization.submit', preview=preview, request_id=request_id)
    def done():
        current = client.manage('organization.show', task_id=task['id'])
        return current if current['status'] not in ('queued', 'running') else None
    result = wait_until(done, 1200)
    accepted = []
    if result['status'] == 'completed':
        accepted = review_candidates(client, out, number)
    write_new(out / 'organization' / (active['id'] + '-result.json'), {'task': result, 'accepted': accepted})
    # A fully received but schema-rejected summary is a failed memory test,
    # not an unknown charge or a reason to replay completed dialogue.
    if result['status'] == 'failed' and result.get('error_code') == 'organization_invalid_output':
        calls = [read(p) for p in (out / 'calls' / active['id']).glob('*.json')]
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and not accepted, 'rejected_output_not_settled')
        return result
    require(result['status'] == 'completed', 'organization_' + result['status'])

def continue_plan(out):
    """Explicit once-only continuation of known completed chat, never a retry."""
    out = Path(out).resolve()
    owner = read(out / 'owner.json')
    previous = read(out / 'progress.json')
    require(previous['status'] == 'stopped' and previous['error'] == 'background_acceptance_organization_failed', 'continuation_not_applicable')
    n = previous['completed_rounds']
    require(n in (25, 50, 75), 'continuation_not_checkpoint')
    failed = read(out / 'organization' / f'M{n:03}-result.json')['task']
    require(failed['status'] == 'failed' and failed['error_code'] == 'organization_invalid_output', 'continuation_unknown_failure')
    current = code_hashes()
    require(all(current.get(p) == digest for p, digest in owner['code_hashes'].items()
                if not p.startswith('plugins/background_acceptance/')), 'runtime_changed')
    rows = [read(p) for p in sorted((out / 'results').glob('*.json'))]
    require([r['number'] for r in rows] == list(range(1, n + 1)), 'completed_prefix_gap')
    calls = [read(p) for p in (out / 'calls').glob('*/*.json')]
    require(all(c['status'] == 'completed' for c in calls), 'unknown_or_failed_call')
    require(not (out / 'intents' / f'T{n+1:03}.json').exists(), 'next_position_already_admitted')
    closed = read(out / 'openings' / str(previous['opening']) / 'exit-verified.json')
    require(closed['snapshot'] == snapshot(RuntimePaths(Path(owner['home']))), 'closed_checkpoint_changed')
    preserved = {}
    for folder in ('results', 'calls', 'requests', 'intents', 'organization', 'openings', 'restarts'):
        for p in (out / folder).rglob('*'):
            if p.is_file(): preserved[p.relative_to(out).as_posix()] = sha(p)
    write_new(out / 'continuation.json', {'at': timestamp(), 'original_owner_hash': sha(out / 'owner.json'),
              'code_hashes': current, 'completed_rounds': n, 'previous_opening': previous['opening'],
              'preserved': preserved, 'reason': 'Keep rejected M050 unchanged; continue only unadmitted dialogue.',
              'snapshot': closed['snapshot']})

def execute(out, continuing=False):
    from .materials import evaluate
    out = Path(out).resolve()
    owner, plan = verify(out)
    paths = RuntimePaths(Path(owner['home']))
    with closing(InstanceLock(out / 'supervisor.lock')):
        if continuing:
            require((out / 'continuation.json').exists() and not (out / 'continuation-started.json').exists(), 'continuation_not_ready')
        else:
            require(not (out / 'started.json').exists(), 'already_started_no_replay')
        probes = {int(k):v for k,v in read(out/'evaluation-only.json').items()}
        if not plan['simulated']:
            simulation = read(Path(read(OUT / 'simulation-current.json')['out']) / 'summary.json')
            require(simulation['completed_rounds'] == 100 and simulation['restart_count'] == 3
                    and simulation['code_hashes'] == code_hashes(), 'simulation_required')
            if plan.get('scenario') == 'memory-repair-100':
                smoke = read(Path(read(OUT/'real-smoke-current.json')['out'])/'summary.json')
                require(smoke['completed_rounds']==6 and smoke['all_recall_checks_passed']
                        and not smoke['organization_failures'] and smoke['code_hashes']==code_hashes(), 'real_smoke_required')
        write_new(out / ('continuation-started.json' if continuing else 'started.json'),
                  {'at': timestamp(), 'pid': os.getpid(), 'simulated': plan['simulated']})
        process = None
        opening, results, restarts = 1, [], []
        if continuing:
            continuation = read(out / 'continuation.json')
            opening = continuation['previous_opening'] + 1
            results = [read(p) for p in sorted((out / 'results').glob('*.json'))]
            restarts = [read(p) for p in (out / 'restarts').glob('*.json')]
        error = None
        def progress(status):
            atomic_json(out / 'progress.json', {'status': status, 'completed_rounds': len(results),
                'opening': opening, 'restarts': len(restarts), 'at': timestamp(), 'error': error})
        try:
            process, ready = spawn_worker(out, opening)
            client = LocalClient(ConfigStore(paths))
            session = results[-1]['session_id'] if results else None
            start = len(results)
            if continuing:
                require(ready['snapshot'] == continuation['snapshot'], 'continuation_startup_changed')
                old = read(out / 'openings' / str(opening-1) / 'closed.json')
                require(old['pid'] != ready['pid'], 'continuation_same_process')
                proof = {'after_round': start, 'old_pid': old['pid'], 'new_pid': ready['pid'],
                         'history_equal': True, 'startup_generation_calls': 0, 'continued_after_recorded_failure': True}
                write_new(out / 'restarts' / f'{start}.json', proof)
                restarts.append(proof)
            for position in plan['positions'][start:]:
                verify(out)
                require(not (out / 'stop.request').exists(), 'user_stop')
                progress('running')
                n, key = position['number'], position['id']
                atomic_json(out / 'active.json', {'id': key, 'kind': 'chat'})
                payload = {'channel': 'desktop', 'persona_id': owner['persona_id'],
                           'request_id': identifier(), 'text': position['text']}
                write_new(out / 'intents' / (key + '.json'), payload)
                receipt = client.submit_input(payload)
                batch = client.call('GET', '/v2/batches/' + receipt['batch_id'])
                if batch['state'] == 'collecting':
                    client.call('POST', '/v2/batches/' + batch['id'] + '/seal',
                        {'expected_revision': batch['revision'], 'operation_id': identifier(), 'persona_id': owner['persona_id']})
                def done():
                    b = client.call('GET', '/v2/batches/' + batch['id'])
                    if not b.get('turn_id'): return None
                    t = client.call('GET', '/v1/requests/' + b['turn_id'])
                    return t if t['status'] not in ('queued', 'running') else None
                turn = wait_until(done, 1500)
                require(turn['status'] == 'completed', 'chat_' + str(turn.get('error_code')))
                require(session in (None, turn['session_id']), 'session_changed')
                session = turn['session_id']
                request = read(out / 'requests' / (key + '.json'))
                with Store(paths).connection() as db:
                    persisted = json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? ORDER BY rowid DESC LIMIT 1', (turn['id'],)).fetchone()[0])
                    attempt = db.execute('SELECT usage_json FROM attempts WHERE turn_id=? ORDER BY rowid DESC LIMIT 1', (turn['id'],)).fetchone()[0]
                require(persisted['input_snapshot_hash'] == request['messages_hash'], 'persisted_manifest')
                calls = [read(p) for p in (out / 'calls' / key).glob('*.json')]
                audits = json.loads(attempt or '{}').get('generation_retry', {}).get('attempts', [])
                require(len(calls) == len(audits) and len(calls) > 0, 'ledger_count')
                require(all(c['status'] != 'reserved' and c['messages_hash'] == request['messages_hash'] for c in calls), 'receipt_unknown')
                require(all(a['request_hash'] == calls[0]['wire_hash'] for a in audits), 'wire_audit')
                evaluation = evaluate(n, turn['reply'], probes)
                if evaluation:
                    evaluation.update(included_state_refs=persisted.get('included_state_refs'),
                        evidence_refs=persisted.get('evidence_refs'), retrieval=persisted.get('retrieval'))
                result = {'number': n, 'id': key, 'turn_id': turn['id'], 'session_id': session,
                          'input': position['text'], 'reply': turn['reply'], 'usage': turn.get('usage'),
                          'evaluation': evaluation, 'physical_calls': len(calls), 'audited': True}
                write_new(out / 'results' / (key + '.json'), result)
                results.append(result)
                if n % plan['organization_every'] == 0:
                    organize(client, out, session, n, plan['organization_every'])
                elif n == plan.get('approve_conflicts_after'):
                    review_candidates(client, out, n)
                progress('running')
                if n in plan['restarts_after']:
                    before = close_worker(out, opening, process, paths)
                    old_pid = process.worker_pid
                    process = None
                    opening += 1
                    process, ready = spawn_worker(out, opening)
                    require(ready['snapshot'] == before and ready['pid'] != old_pid, 'restart_not_complete')
                    proof = {'after_round': n, 'old_pid': old_pid, 'new_pid': process.pid,
                             'history_equal': True, 'startup_generation_calls': 0}
                    write_new(out / 'restarts' / f'{n}.json', proof)
                    restarts.append(proof)
            progress('completed')
        except BaseException as exc:
            error = str(exc)[:160]
            progress('stopped')
            raise
        finally:
            if process is not None and process.poll() is None:
                close_worker(out, opening, process, paths)
            report(out)

def report(out):
    out = Path(out)
    plan = read(out / 'plan.json')
    rows = [read(p) for p in sorted((out / 'results').glob('*.json'))]
    calls = [read(p) for p in (out / 'calls').glob('*/*.json')]
    probes = [dict(round=r['number'], **r['evaluation']) for r in rows if r['evaluation']]
    for probe in probes:
        manifest = read(out / 'requests' / f"T{probe['round']:03}.json")['manifest']
        probe['included_state_refs'] = manifest.get('included_state_refs', [])
        probe['evidence_refs'] = manifest.get('evidence_refs', [])
        for legacy in ('history_rows', 'summary_id', 'memories', 'history_groups', 'source_in_recent_history'):
            probe.pop(legacy, None)
    summary = {'version': VERSION, 'at': timestamp(), 'simulated': plan['simulated'],
        'completed_rounds': len(rows), 'planned_rounds': len(plan['positions']), 'restart_count': len(list((out / 'restarts').glob('*.json'))),
        'physical_calls': len(calls), 'main_calls': sum(c['kind'] == 'chat' for c in calls),
        'organization_calls': sum(c['kind'] == 'organization' for c in calls),
        'failed_calls': sum(c['status'] != 'completed' for c in calls),
        'reported_tokens': sum((c.get('usage') or {}).get('total_tokens', 0) or 0 for c in calls),
        'unknown_usage_calls': sum(not c.get('usage') for c in calls), 'cost': 'not_reported',
        'probes': probes, 'all_recall_checks_passed': len(probes) == len(read(out/'evaluation-only.json')) and all(p['answer_check_passed'] for p in probes),
        'organization_failures': [{'file':p.name,'error':read(p)['task'].get('error_code')}
            for p in (out / 'organization').glob('*-result.json') if read(p)['task']['status'] != 'completed'],
        'code_hashes': (read(out / 'continuation.json')['code_hashes'] if (out / 'continuation.json').exists()
                        else read(out / 'owner.json')['code_hashes']), 'computer_use': False,
        'limitation': 'Accelerated artificial-user validation; not elapsed-day or human relationship evidence.'}
    atomic_json(out / 'summary.json', summary)
    if plan['simulated'] and len(rows) == 100 and summary['restart_count'] == 3:
        atomic_json(OUT / 'simulation-current.json', {'out': str(out), 'at': timestamp()})
    if (not plan['simulated'] and plan.get('scenario')=='correction-smoke'
            and len(rows)==len(plan['positions']) and summary['all_recall_checks_passed']
            and not summary['organization_failures'] and not summary['failed_calls']):
        atomic_json(OUT/'real-smoke-current.json', {'out':str(out),'at':timestamp()})
    lines = ['# PCAR 后台记忆验收', '', f"完成轮数：{len(rows)}/{len(plan['positions'])}；完整进程重启：{summary['restart_count']}/{len(plan['restarts_after'])}。", '',
             f"物理调用：{len(calls)}；已报告token：{summary['reported_tokens']}；金额未提供。", '',
             '## 远距离记忆检查', '', '|追问轮|来源轮|间隔|事实匹配|', '|---|---|---|---|']
    for p in probes:
        lines.append(f"|{p['round']}|{p['source_round']}|{p['gap']}|{'通过' if p['answer_check_passed'] else '未通过'}|")
    lines += ['', '结果为自动事实匹配，须结合请求、摘要、记忆来源复核。模拟结果不代表真实记忆能力。', '', '## 全部对话', '']
    for row in rows:
        lines += [f"### 第{row['number']}轮", '', row['input'], '', row['reply'], '']
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return {k: v for k, v in summary.items() if k not in ('code_hashes', 'probes')}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'continue', 'worker', 'status', 'report', 'stop'))
    parser.add_argument('--out', type=Path, default=OUT / 'real-v1')
    parser.add_argument('--simulated', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--opening', type=int, default=1)
    args = parser.parse_args()
    if args.action == 'prepare': result = asyncio.run(prepare(args.out, args.simulated, args.smoke))
    elif args.action == 'worker': return worker(args.out, args.opening)
    elif args.action == 'run': result = execute(args.out)
    elif args.action == 'continue':
        continue_plan(args.out)
        result = execute(args.out, continuing=True)
    elif args.action == 'report': result = report(args.out)
    elif args.action == 'status': result = read(args.out / 'progress.json')
    else:
        write_new(args.out / 'stop.request', {'at': timestamp()})
        result = {'stop_requested': True, 'finish_current_request_then_stop': True}
    print(json.dumps(result, ensure_ascii=False))
