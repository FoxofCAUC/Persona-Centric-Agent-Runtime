"""One disposable v8 QQ acceptance, using the normal ingress and delivery stack.

Bodies never leave the owned private home. Public evidence contains only IDs,
hashes, counters and coverage. Unknown delivery is never resubmitted here.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from experiments import companion_c2 as copies
from experiments import companion_expression_adoption as author
from experiments.companion_comparison import public_model
from experiments.companion_formulaic import offline, sha, error_code
from experiments.companion_trial import configure, copy_weights
from experiments.companion_workbench_trial import configure_local
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController, port_open
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, provider_identity, safe
from persona_runtime.provider_compat import build_request
from persona_runtime.qq_bridge import QQBridge, allowed_message
from persona_runtime.qq_testing import QQTesting
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-qq-channel-v1'
ROOT = Path(__file__).resolve().parents[1]
OUT = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-5C-qq-private-channel'
NAMESPACE = 'companion-qq-channel'


def require(value, reason):
    if not value:
        raise RuntimeErrorBase('qq_acceptance_' + reason)


class OperatorInputMismatch(RuntimeErrorBase):
    """A terminal zero-dispatch rejection, not a failure of the whole channel."""


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def append(path, value):
    """Creation-only records prevent restarts from replacing success or unknowns."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()


def code_hashes():
    files = list((ROOT / 'persona_runtime').glob('*.py'))
    files += [Path(__file__), ROOT / 'scripts/companion_qq_acceptance.py']
    files += list((ROOT / 'integrations/astrbot/astrbot_plugin_persona_runtime').glob('*.*'))
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in files}


def execution_freeze(out):
    files = list(Path(out).glob('execution-freeze*.json'))
    if not files:
        return None
    revisions = [(read(p).get('revision', 0), p) for p in files]
    require(len({n for n, _ in revisions}) == len(revisions), 'duplicate_execution_revision')
    return max(revisions, key=lambda row: row[0])[1]


def stage_launcher(paths):
    """Small independent host venv; dependencies stay in the already-owned copy."""
    from persona_runtime.deployment import child_environment
    target = paths.home / '.venv'
    require(not target.exists(), 'launcher_already_present')
    subprocess.run([sys.executable, '-B', '-m', 'venv', '--without-pip', str(target)],
                   env=child_environment(paths), check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, creationflags=0x08000000)
    packages = target / 'Lib/site-packages'
    (packages / 'pcar-owned-dependencies.pth').write_text(
        str(paths.home / 'components/astrbot/.venv/Lib/site-packages') + '\n', encoding='utf-8')


def cases():
    # These inputs are artificial transport tests, not approved character prose.
    text = '\n'.join(f'记录{i:02}：原样保存编号与金额，数值为3.14，网址为https://example.invalid/a_b?q=1.25。'
                     for i in range(1, 9))
    text += '\n```python\nx = 3.14\nprint(x**2)\n```'
    return [
        {'id': 'Q01', 'inputs': ['下午好，刚才路过文具店，一只猫正趴在收银台边。'], 'kind': 'single'},
        {'id': 'Q02', 'inputs': ['我想周六去逛旧书店。', '原本打算早上出门。', '更正一下，是周日下午，不是周六，也不是早上。'], 'kind': 'batch'},
        {'id': 'Q03', 'inputs': ['我说的就是这只猫，它还把爪子搭在计算器上。'], 'kind': 'reference', 'reference_position': 'Q01'},
        {'id': 'Q04', 'inputs': ['请原样返回下面的测试文本，不加说明，不省略任何一行：\n' + text], 'kind': 'long', 'expected_visible': text.replace('```python\n', '').replace('\n```', '')},
        {'id': 'Q05', 'inputs': ['请完整编写一个Python命令行CSV检查工具，包含解析、字段验证、错误报告、边界处理、完整代码和可运行示例。'], 'kind': 'cancel'},
        {'id': 'Q06', 'inputs': ['刚才关闭后又回来了。接着说，书店的安排以我最后更正的时间为准。'], 'kind': 'reopen'},
    ]


def current_author(source_home):
    """Find the one saved approval that still matches the current author versions."""
    found = []
    for path in (Path(source_home) / 'private/personas').glob('*/author-adoptions/*.json'):
        try:
            author._saved(path, source_home)
            found.append(path)
        except RuntimeErrorBase:
            pass
    require(len(found) == 1, 'current_author_not_unique')
    return found[0]


def conditions(paths, pid):
    store = Store(paths)
    with store.connection() as db:
        require(store.current(db) == pid, 'persona_changed')
        row = db.execute('SELECT p.* FROM interaction_heads h JOIN interaction_policies p ON p.id=h.version_id '
                         "WHERE h.persona_id=? AND h.channel='qq'", (pid,)).fetchone()
    require(row is not None, 'policy_missing')
    cfg = ConfigStore(paths).load()
    return {'versions': author._descriptors(author._versions(store, pid)),
            'policy_hash': fingerprint(dict(row)), 'config_hash': fingerprint(cfg),
            'permission_hash': sha(paths.home / 'runtime/private-copy-policy.json'),
            'test_policy_hash': sha(paths.home / 'private/qq-testing.json')}


def port_bound(port):
    """Offline preparation checks availability by binding, never connecting."""
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return False
        except OSError:
            return True


def unresolved(paths):
    with Store(paths).connection() as db:
        return {name: db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + where).fetchone()[0]
                for name, table, where in (
                    ('turns', 'turns', "status IN ('queued','running')"),
                    ('inputs', 'input_batches', "state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')"),
                    ('reservations', 'quota_reservations', "state='active'"),
                    ('delivery', 'reply_delivery_parts', "state IN ('pending','sending','failed','uncertain')"))}


def prepare(out=OUT, source_home=DEFAULT_HOME, *, fixture=False, stage_components=True):
    out, source_home = Path(out).resolve(), Path(source_home).resolve()
    require(not (out / 'owner.json').exists(), 'already_prepared')
    require(fixture or (out / 'offline-result.json').exists(), 'simulate_first')
    if not fixture:
        require(read(out / 'offline-result.json')['passed'], 'simulation_failed')
    source = ConfigStore(RuntimePaths(source_home)).load()
    test = QQTesting(RuntimePaths(source_home)).load(True)
    require(test['state'] == 'inactive', 'source_test_mode_not_inactive')
    require(source['owner_qq'] == test['daily_peer'], 'source_owner_changed')
    require(fixture or (source['model']['base_url'].rstrip('/') == 'https://api.deepseek.com'
                       and source['model']['name'] == 'deepseek-flash' and source['model']['encrypted_key']), 'official_model_required')
    binding = None if fixture else current_author(source_home)
    root = source_home / 'tmp' / NAMESPACE / identifier()
    root.mkdir(parents=True)
    owner = {'version': VERSION, 'source_home': str(source_home), 'private': not fixture,
             'status': 'preparing', 'control_root': str(root), 'created_at': timestamp()}
    if not fixture:
        from persona_runtime.character_embedding import private_directory
        private_directory(source_home, root)
    append(out / 'preparing.json', {'version': VERSION, 'control_root': str(root), 'at': timestamp()})
    with offline():
        paths, pid = copies.create_copy(source_home, binding, owner, root)
        if not fixture:
            private_directory(source_home, paths.home)
        cfg = configure(paths, source, pid)
        config_value = cfg.load()
        config_value['character_selector'].update(enabled=False, encrypted_key='')
        config_value['organization']['model']['encrypted_key'] = ''
        atomic_json(cfg.file, config_value)
        cfg.update({'bot_qq': source['bot_qq'], 'owner_qq': test['daily_peer']})
        atomic_json(paths.home / 'runtime/private-copy-policy.json', {
            'persona_id': pid, 'approved_models': {'main': provider_identity(source['model'])},
            'approval_scope': VERSION})
        store = Store(paths)
        engine = ChatEngine(store, cfg)
        try:
            ingress = IngressService(engine)
            old = ingress.current_policy(pid, 'qq')
            ingress.update_policy(pid, 'qq', 'natural_batch', old['id'], identifier(),
                companion_mode='minimal_im', reply_mode='context_full', expression_policy_version=3,
                conversation_policy_version=2)
        finally:
            engine.cognitive.characters.close()
            engine.cognitive.shadow.close()
        q = QQTesting(paths)
        identity_kwargs = ({'daily_bot':test['daily_bot'],'test_bot':test['test_bot']}
                           if test['version']==2 else {})
        q.configure(test['daily_peer'], test['test_peer'], **identity_kwargs)
        with patch('persona_runtime.lifecycle.port_open', side_effect=port_bound):
            q.enter(pid)
        if not fixture:
            copy_weights(RuntimePaths(source_home), paths)
            from experiments.companion_comparison import local_retrieval_ready
            local_engine = ChatEngine(store, cfg)
            try:
                local_retrieval_ready(local_engine.cognitive.characters, pid, ['你好'], build=True)
            finally:
                local_engine.cognitive.characters.close()
                local_engine.cognitive.shadow.close()
            if stage_components:
                source_venv = source_home / 'components/astrbot/.venv'
                require((source_venv / 'Scripts/python.exe').is_file(), 'installed_astrbot_missing')
                shutil.copytree(source_venv, paths.home / 'components/astrbot/.venv',
                                ignore=shutil.ignore_patterns('__pycache__'))
                from persona_runtime.deployment import prepare as components
                components(paths, install=False, source=source_home / 'components/napcat')
                stage_launcher(paths)
        owner.update(persona_id=pid, status='prepared', source_config_hash=fingerprint(source),
                     conditions=conditions(paths, pid), code_hashes=code_hashes(),
                     model_public=public_model(cfg.load()['model']), fixture=fixture,
                     components_staged=bool(stage_components and not fixture),
                     cases=cases(), startup_generation_calls=0)
        require(not any(unresolved(paths).values()), 'prepared_unresolved')
        with store.connection() as db:
            require(db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0, 'prepare_generated')
            require(db.execute('SELECT count(*) FROM events').fetchone()[0] == 0, 'initial_history_present')
        atomic_json(root / 'trial.json', owner)
        append(out / 'owner.json', owner)
    return {'prepared': True, 'manifest': str(out / 'owner.json'), 'generation_calls': 0}


def verify(out=OUT):
    out = Path(out).resolve()
    owner = read(out / 'owner.json')
    source = Path(owner['source_home']).resolve()
    home = safe(owner['home'], source / 'tmp/private-acceptance')
    control = safe(owner['control_root'], source / 'tmp' / NAMESPACE)
    require(home.parent == source / 'tmp/private-acceptance' and control.parent == source / 'tmp' / NAMESPACE, 'scope')
    frozen_code = owner['code_hashes']
    execution = execution_freeze(out)
    if execution is not None:
        freeze = read(execution)
        require(freeze['owner_hash'] == sha(out / 'owner.json') and freeze['version'] == VERSION,
                'execution_freeze_changed')
        frozen_code = freeze['code_hashes']
        if (out / 'started.json').exists():
            require(read(out / 'started.json')['execution_freeze_hash'] == sha(execution), 'execution_freeze_changed')
    require(owner['version'] == VERSION and frozen_code == code_hashes(), 'source_changed')
    marker = read(home / 'private-sandbox.json')
    require(marker['purpose'] == VERSION and marker['persona_id'] == owner['persona_id']
            and Path(marker['formal_home']).resolve() == source, 'owner_changed')
    require(fingerprint(ConfigStore(RuntimePaths(source)).load()) == owner['source_config_hash'], 'formal_config_changed')
    if owner['private']:
        require(sha(owner['author_binding']) == owner['author_binding_hash'], 'approval_changed')
        author._saved(owner['author_binding'], source)
    paths = RuntimePaths(home)
    require(conditions(paths, owner['persona_id']) == owner['conditions'], 'frozen_conditions_changed')
    require(owner['cases'] == cases(), 'inputs_changed')
    QQTesting(paths).require_ready()
    return owner, paths


def position_for(out):
    completed = {p.stem for p in (Path(out) / 'positions').glob('Q*.json')}
    return next((c for c in cases() if c['id'] not in completed), None)


class Recorder:
    """Observe actual assembly and native physical attempts, without another retry loop."""
    def __init__(self, out, engine, target, *, verifier=verify, position=position_for):
        self.out, self.engine, self.target = Path(out), engine, target
        self.verify,self.position=verifier,position
        self.owner, self.paths = self.verify(out)
        self.body = self.paths.home / 'runtime/qq-acceptance'
        self.current = self.request = None
        self.audit = None
        self.blocked = False

    def get(self, *args):
        return None

    def stop(self, error):
        self.blocked = True
        self.engine.draining = True
        atomic_json(self.body / 'blocked.json', {'at': timestamp(), 'error': error_code(error)})

    def before_dispatch(self, engine, turn, prepared):
        try:
            self.verify(self.out)
            require(not self.blocked, 'blocked')
            case = self.position(self.out)
            require(case is not None, 'no_remaining_position')
            require(case['id'] != 'Q06' or (self.out / 'reopen.json').exists(), 'reopen_required')
            require(turn['channel'] == 'qq' and turn['persona_id'] == self.owner['persona_id'], 'wrong_entry')
            cfg = ConfigStore(self.paths).load()
            origin = json.loads(turn['origin_json'])
            require(origin.get('sender') == cfg['owner_qq'] and origin.get('bot') == cfg['bot_qq'], 'wrong_origin')
            with engine.store.connection() as db:
                rows = db.execute('SELECT e.text FROM input_batch_members m JOIN events e ON e.id=m.event_id '
                                  'WHERE m.batch_id=? ORDER BY m.member_order', (turn['input_batch_id'],)).fetchall()
            if [r[0] for r in rows] != case['inputs']:
                require(self.current is None, 'concurrent_position')
                append(self.body / 'input-rejections' / (turn['id'] + '.json'), {
                    'at': timestamp(), 'turn_id': turn['id'], 'position': case['id'],
                    'input_fingerprint': fingerprint([r[0] for r in rows]), 'model_dispatches': 0})
                raise OperatorInputMismatch('qq_acceptance_unexpected_input')
            references = prepared['details'].get('references') or {}
            if case['kind'] == 'reference':
                expected = read(self.out / 'positions/Q01.json')['turn_id']
                require(references.get('turn_ids') == [expected]
                        and bool(references.get('references'))
                        and all(r.get('status') == 'found' for r in references['references']), 'wrong_reference')
            else:
                require(not references.get('references'), 'unexpected_reference')
            require(json.loads(turn['context_policy_snapshot'])['companion'] == choose('minimal_im'), 'not_v8')
            self.folder = self.body / case['id']
            append(self.folder / 'claimed.json', {'turn_id': turn['id'], 'at': timestamp()})
            model = json.loads(turn['model_snapshot'])
            model.update(_reply_mode=turn['reply_mode_snapshot'], _expression_snapshot=turn['expression_snapshot'])
            wire = build_request(model, prepared['messages'])[0]
            self.current = case | {'turn_id': turn['id']}
            self.request = {'messages': prepared['messages'], 'wire': wire,
                            'details': prepared['details'], 'manifest': prepared['manifest']}
            diagnostic = prepared['details'].get('prompt_assembly')
            if diagnostic:
                require(diagnostic['input_snapshot_hash'] == fingerprint(prepared['messages']), 'manifest_input_mismatch')
            append(self.folder / 'request.json', self.request)
            self.audit = None
        except OperatorInputMismatch:
            raise
        except BaseException as e:
            self.stop(e)
            raise

    def save(self, value):
        try:
            require(self.current is not None, 'unowned_audit')
            self.audit = deepcopy(value)
            for attempt in value['generation_retry']['attempts']:
                reserve = self.folder / (attempt['id'] + '.reserve.json')
                if not reserve.exists():
                    require(attempt['status'] == 'running', 'missing_reservation')
                    append(reserve, {'kind': 'real' if self.owner.get('real',self.owner['private']) else 'simulated', 'attempt': attempt})
                atomic_json(self.folder / (attempt['id'] + '.attempt.json'), attempt)
            atomic_json(self.folder / 'audit.json', value)
        except BaseException as e:
            self.stop(e)
            raise

    async def complete(self, model, messages):
        try:
            self.verify(self.out)
            require(not self.blocked and self.request is not None and self.audit is not None, 'unowned_dispatch')
            wire = build_request(model, messages)[0]
            require(messages == self.request['messages'] and wire == self.request['wire'], 'input_changed')
            attempt = self.audit['generation_retry']['attempts'][-1]
            append(self.folder / (attempt['id'] + '.dispatch.json'), {
                'kind': 'real' if self.owner.get('real',self.owner['private']) else 'simulated', 'at': timestamp(),
                'wire_hash': fingerprint(wire), 'messages_hash': fingerprint(messages)})
        except BaseException as e:
            self.stop(e)
            raise
        if self.current['kind'] == 'cancel':
            async def cancel():
                await asyncio.sleep(.1)
                state = self.engine.get(self.current['turn_id'])
                if state['status'] == 'running':
                    value = await self.engine.cancel(state['id'])
                    atomic_json(self.folder / 'cancel.json', {'at': timestamp(), 'status': value['status'],
                                                           'cancelled_at': value.get('cancelled_at')})
            asyncio.create_task(cancel())
        try:
            result = await self.target.complete(model, messages)
            append(self.folder / (attempt['id'] + '.response.json'), {
                'text': result[0], 'usage': result[1], 'diagnostics': getattr(result, 'diagnostics', {})})
            return result
        except BaseException as e:
            append(self.folder / (attempt['id'] + '.error.json'), {
                'error': error_code(e), 'usage': getattr(e, 'usage', None),
                'cancelled': isinstance(e, asyncio.CancelledError)})
            raise

    def settle(self):
        if self.current is None:
            return None
        turn = self.engine.get(self.current['turn_id'])
        if turn['status'] in ('queued', 'running'):
            return None
        with self.engine.store.connection() as db:
            parts = [dict(r) for r in db.execute('SELECT * FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index', (turn['id'],))]
            aux = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                   for table in ('character_selection_attempts', 'situation_observation_attempts',
                                 'organization_attempts', 'disposition_compile_attempts')}
        if any(p['state'] in ('pending', 'sending') for p in parts):
            return None
        case = self.current
        cancelled = bool(turn.get('cancelled_at')) and not parts
        ok = cancelled if case['kind'] == 'cancel' else (turn['status'] == 'completed' and bool(parts)
                    and all(p['state'] == 'sent' and p['platform_message_id'] for p in parts))
        if case['kind'] == 'long':
            from persona_runtime.qq_text import plain_qq_text
            ok = ok and len(parts) > 1 and ''.join(p['text'] for p in parts) == plain_qq_text(case['expected_visible'])
        row = {'id': case['id'], 'turn_id': turn['id'], 'status': turn['status'], 'coverage_passed': bool(ok),
               'cancelled': cancelled, 'error_code': turn.get('error_code'),
               'message_fingerprint': fingerprint(self.request['messages']),
               'wire_fingerprint': fingerprint(self.request['wire']), 'auxiliary_counts': aux,
               'part_count': len(parts), 'part_states': [p['state'] for p in parts],
               'platform_ids': [p['platform_message_id'] for p in parts],
               'physical_attempts': len(list(self.folder.glob('*.dispatch.json'))),
               'usage': {k: v for k, v in (turn.get('usage') or {}).items()
                         if k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens')}
                        if turn.get('usage') is not None else None,
               'mobile_visible': 'awaiting_user_confirmation', 'read': 'unknown'}
        append(self.folder / 'terminal.json', {'turn': turn, 'parts': parts})
        append(self.out / 'positions' / (case['id'] + '.json'), row)
        self.current = self.request = self.audit = None
        if not ok or any(aux.values()):
            self.stop(RuntimeErrorBase('qq_acceptance_coverage_failed'))
        return row


async def simulation(out=OUT):
    """Six real runtime positions; only OneBot events and model responses are simulated."""
    out = Path(out)
    require(not (out / 'offline-result.json').exists(), 'simulation_already_run')
    root = DEFAULT_HOME / 'tmp' / NAMESPACE / ('simulation-' + identifier())
    paths = RuntimePaths(root)
    cfg = ConfigStore(paths); cfg.initialize()
    cfg.update({'bot_qq': '456', 'owner_qq': '123', 'model': {'base_url': 'https://offline.invalid', 'name': 'deepseek-flash', 'hourly_limit': 0}})
    store = Store(paths); store.initialize()
    runtime = RuntimeService(store)
    pid = runtime.create_persona(__import__('persona_runtime.models', fromlist=['generic_persona']).generic_persona())['id']
    runtime.use_persona(pid)
    calls = []
    class Client:
        async def complete(self, model, messages):
            calls.append({'profile': model['_companion_profile']['version'], 'fingerprint': fingerprint(messages)})
            if len(calls) == 5:
                await asyncio.sleep(30)
            return ('收到。' if len(calls) != 4 else cases()[3]['expected_visible']), {'total_tokens': 1, 'mock': True}
    engine = ChatEngine(store, cfg, Client())
    from unittest.mock import patch
    from persona_runtime.character_embedding import SemanticFailure
    from persona_runtime.delivery import DeliveryService
    result = []
    ingress = IngressService(engine)
    previous = ingress.current_policy(pid, 'qq')
    ingress.update_policy(pid, 'qq', 'natural_batch', previous['id'], identifier(), companion_mode='minimal_im',
                          reply_mode='context_full', expression_policy_version=3, conversation_policy_version=2)
    delivery = DeliveryService(store)
    bridge = QQBridge(paths)
    origin = {'sender': '123', 'bot': '456'}
    async def request(method, path, value=None):
        if method == 'GET': return delivery.get(path.rsplit('/', 1)[1], origin=origin)
        stream = delivery.by_batch(path.split('/')[3]); part = path.split('/')[5]
        if path.endswith('/claim'):
            return delivery.claim(stream['id'], part, value['expected_revision'], value['operation_id'], origin=origin)
        return delivery.result(stream['id'], part, value['claim_token'], value['state'], value['expected_revision'], value['operation_id'],
                               platform_message_id=value.get('platform_message_id'), error=value.get('error'), origin=origin)
    bridge.request = request
    def payload(mid, text, reference=None):
        parts = [{'type': 'text', 'data': {'text': text}}]
        if reference: parts.insert(0, {'type': 'reply', 'data': {'id': reference}})
        raw = {'message_type': 'private', 'sub_type': 'friend', 'user_id': 123, 'self_id': 456, 'message_id': mid, 'message': parts}
        return allowed_message(SimpleNamespace(platform_meta=SimpleNamespace(name='aiocqhttp', id='persona-onebot'),
            message_obj=SimpleNamespace(message_id=mid, raw_message=raw), is_private_chat=lambda: True,
            get_self_id=lambda: '456', get_sender_id=lambda: '123'), cfg.load())
    try:
        with offline(), patch.object(engine.cognitive.characters, '_hybrid', side_effect=SemanticFailure('embedding_unavailable')):
            n = 0
            for case in cases():
                for text in case['inputs']:
                    n += 1
                    value = payload(str(n), text, '1' if case['kind'] == 'reference' else None)
                    receipt = ingress.accept(value)
                    require(ingress.accept(value)['batch_id'] == receipt['batch_id'], 'duplicate_simulation')
                batch = ingress.get(receipt['batch_id'])
                require(len(batch['members']) == len(case['inputs']), 'batch_simulation')
                ingress.action(batch['id'], 'seal', batch['revision'], identifier(), pid)
                if case['kind'] == 'cancel':
                    while len(calls) != 5: await asyncio.sleep(.01)
                    await engine.cancel(ingress.get(batch['id'])['turn_id'])
                await engine.wait()
                turn = engine.get(ingress.get(batch['id'])['turn_id'])
                if case['kind'] == 'cancel':
                    require(bool(turn.get('cancelled_at')), 'cancel_simulation')
                    parts = []
                else:
                    require(turn['status'] == 'completed', 'reply_simulation_' + str(turn.get('error_code')))
                    stream = delivery.by_batch(turn['reply_batch']['id'])
                    async def send(text): return {'message_id': 'fake-' + identifier()}
                    await bridge.deliver_stream(stream, send)
                    parts = delivery.get(stream['id'])['parts']
                    require(all(p['state'] == 'sent' for p in parts), 'delivery_simulation')
                    if case['kind'] == 'long': require(len(parts) > 1, 'split_simulation')
                result.append({'id': case['id'], 'status': turn['status'], 'parts': len(parts), 'passed': True})
                if case['kind'] == 'cancel':
                    require(not any(unresolved(paths).values()), 'checkpoint_simulation')
                    before = len(calls)
                    await engine.wait()
                    require(len(calls) == before, 'simulation_replay')
        append(out / 'offline-result.json', {'version': VERSION, 'passed': True, 'positions': result,
            'simulated_generations': len(calls), 'real_generations': 0, 'simulation_home': str(root),
            'reopen': 'covered_by_runtime_tests', 'at': timestamp()})
        return {'passed': True, 'positions': len(result), 'simulated_generations': len(calls), 'real_generations': 0}
    finally:
        await engine.wait()
        engine.cognitive.characters.close(); engine.cognitive.shadow.close()


def report(out=OUT):
    out = Path(out)
    if (out / 'settlement.json').exists():
        return read(out / 'settlement.json')
    rows = [read(p) for p in sorted((out / 'positions').glob('Q*.json'))]
    rejected = None
    if (out / 'owner.json').exists():
        owner = read(out / 'owner.json')
        home = safe(owner['home'], Path(owner['source_home']) / 'tmp/private-acceptance')
        store = Store(RuntimePaths(home))
        if store.path.is_file():
            with store.connection() as db:
                rejected = db.execute("SELECT count(*) FROM turns WHERE error_code='qq_acceptance_unexpected_input'").fetchone()[0]
    return {'version': VERSION, 'planned_positions': 6, 'observed_positions': len(rows),
            'positions': rows, 'expression_repair': 'complete_and_frozen',
            'pre_dispatch_input_rejections': rejected,
            'real_channel_complete': len(rows) == 6 and all(r['coverage_passed'] for r in rows),
            'mobile_confirmation': 'pending', 'cleanup': 'pending'}


def verify_closed(out):
    """Validate retained evidence without recreating deleted runtime data."""
    out = Path(out).resolve()
    seal = read(out / 'closure.json')
    require(seal['version'] == VERSION and seal['stage_complete'], 'closure_incomplete')
    for name, digest in seal['evidence_hashes'].items():
        path = safe(out / name, out)
        require(path.is_file() and sha(path) == digest, 'closed_evidence_changed')
    for name in seal['cleanup_roots']:
        path = safe(name, Path(seal['source_home']) / 'tmp')
        require(not path.exists(), 'closed_cleanup_incomplete')
    return {'closed': True, 'evidence_intact': True, 'cleanup_verified': True,
            'current_code_matches_closing_code': seal['closing_code_hashes'] == code_hashes(),
            'stage_complete': True}


class Runtime:
    """Normal HTTP app with an injectable observer; only the tool owns its lifetime."""
    def __init__(self, out, *, target=None, verifier=verify, position=position_for):
        self.out = Path(out)
        self.verify,self.position=verifier,position
        self.owner, self.paths = self.verify(out)
        self.target = target or ChatCompletionsClient()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='qq-acceptance-api', daemon=True)
        self.running = False
        self.runner = self.app = self.recorder = None
        self.lock = None

    async def start(self):
        from persona_runtime.server import create_app
        require(not any(unresolved(self.paths).values()), 'unfinished_no_automatic_resume')
        with Store(self.paths).connection() as db:
            before = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
        self.app = create_app(self.paths, self.target)
        engine = self.app['engine']
        if self.owner['private']:
            await asyncio.to_thread(configure_local, engine, self.paths, self.owner['persona_id'])
        self.recorder = Recorder(self.out, engine, self.target, verifier=self.verify, position=self.position)
        engine.client = self.recorder
        engine.evaluation_session = self.recorder
        engine.generation_audit_sink = self.recorder.save
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', ConfigStore(self.paths).load()['service_port']).start()
        with Store(self.paths).connection() as db:
            require(db.execute('SELECT count(*) FROM attempts').fetchone()[0] == before, 'startup_generated')
        self.running = True
        self.watcher = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            await asyncio.sleep(.25)
            try:
                row = self.recorder.settle()
                if row:
                    atomic_json(self.paths.home / 'runtime/qq-progress.json', {
                        'latest': row['id'], 'coverage_passed': row['coverage_passed'],
                        'next': (self.position(self.out) or {}).get('id'), 'at': timestamp()})
            except BaseException as e:
                if isinstance(e, asyncio.CancelledError): raise
                self.recorder.stop(e)
                return

    def open(self):
        self.lock = InstanceLock(self.paths.home / 'runtime/service.lock')
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self.start(), self.loop).result(120)
        except BaseException:
            self.close()
            raise

    async def stop(self):
        if getattr(self, 'watcher', None):
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        if self.app:
            require(not any(unresolved(self.paths).values()), 'close_unfinished')
        if self.runner:
            await self.runner.cleanup()
        self.running = False

    def close(self):
        if self.thread.is_alive():
            asyncio.run_coroutine_threadsafe(self.stop(), self.loop).result(90)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
        require(not self.thread.is_alive(), 'thread_not_stopped')
        if self.lock: self.lock.close()
        if not self.loop.is_closed(): self.loop.close()


def checkpoint(paths):
    with Store(paths).connection() as db:
        return {'attempts': db.execute('SELECT count(*) FROM attempts').fetchone()[0],
                'events': fingerprint([dict(r) for r in db.execute('SELECT * FROM events ORDER BY rowid')]),
                'turns': fingerprint([dict(r) for r in db.execute('SELECT * FROM turns ORDER BY rowid')]),
                'sessions': [r[0] for r in db.execute('SELECT id FROM sessions ORDER BY rowid')]}


def listen(out=OUT, *, reopen=False):
    """A visible, window-bound controller. There is no synthetic real ingress."""
    import tkinter as tk
    out = Path(out)
    owner, paths = verify(out)
    require(not owner['fixture'], 'fixture_cannot_listen')
    require(execution_freeze(out) is not None, 'execution_freeze_required')
    require(owner['components_staged'], 'components_missing')
    require(not (out / 'completed-listen.json').exists(), 'already_listened')
    require(not any(unresolved(paths).values()), 'unfinished_no_automatic_resume')
    if reopen:
        require((out / 'checkpoint.json').exists() and not (out / 'reopen.json').exists(), 'not_clean_reopen')
        require(read(out / 'checkpoint.json')['history'] == checkpoint(paths), 'history_changed')
    else:
        require(not (out / 'started.json').exists(), 'started_no_automatic_resume')
    controller = ServiceController(paths)
    require(not controller.has_resident_processes(), 'resident_process_requires_review')
    window = tk.Tk(); window.title('PCAR · QQ渠道验收'); window.geometry('660x240')
    text = tk.StringVar(value='正在准备本轮专用QQ入口；此时请勿发送消息。')
    tk.Label(window, textvariable=text, wraplength=610, justify='left', padx=20, pady=20).pack(fill='both', expand=True)
    controller.start_console()
    runtime = Runtime(out)
    state = {'closing': False, 'error': None, 'ready': False}
    operation_lock = threading.Lock()
    def start():
        operation_lock.acquire()
        try:
            runtime.open()
            for role in ('astrbot', 'napcat'):
                require(not state['closing'], 'startup_cancelled')
                controller.start(role)
            verify(out)
            if reopen:
                require(read(out / 'checkpoint.json')['history'] == checkpoint(paths), 'reopen_generated_or_changed_history')
                append(out / 'reopen.json', {'at': timestamp(), 'startup_generation_calls': 0,
                                           'history_unchanged': True})
            else:
                with Store(paths).connection() as db:
                    require(db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0, 'early_input_during_startup')
                append(out / 'started.json', {'at': timestamp(), 'startup_generation_calls': 0,
                                              'execution_freeze_hash': sha(execution_freeze(out))})
            state['ready'] = True
        except BaseException as e:
            state['error'] = str(e) if isinstance(e, RuntimeErrorBase) else error_code(e)
            atomic_json(paths.home / 'runtime/qq-startup-error.json', {'error': state['error'], 'at': timestamp()})
        finally:
            operation_lock.release()
    def stop():
        operation_lock.acquire()
        try:
            require(not any(unresolved(paths).values()), 'stop_unfinished')
            controller.stop_all(include_qq=True)
            runtime.close()
            controller.end_console()
            if position_for(out) and position_for(out)['id'] == 'Q06':
                append(out / 'checkpoint.json', {'at': timestamp(), 'history': checkpoint(paths), 'unresolved': unresolved(paths)})
            elif position_for(out) is None:
                append(out / 'completed-listen.json', {'at': timestamp(), 'unresolved': unresolved(paths)})
            else:
                append(out / ('stopped-' + identifier() + '.json'), {'at': timestamp(), 'breakpoint': (position_for(out) or {}).get('id')})
            state['closed'] = True
        except BaseException as e:
            state['error'] = str(e) if isinstance(e, RuntimeErrorBase) else error_code(e)
            state['closing'] = False
        finally:
            operation_lock.release()
    def request_close():
        if not state['closing']:
            state['closing'] = True
            threading.Thread(target=stop, name='qq-acceptance-close', daemon=True).start()
    tk.Button(window, text='正常关闭本轮', command=request_close).pack(pady=12)
    window.protocol('WM_DELETE_WINDOW', request_close)
    def tick():
        if state.get('closed'):
            window.destroy(); return
        if state['error']:
            text.set('本轮停止在当前断点：' + state['error'] + '\n请勿继续发送或重试旧消息。原记录保留。')
        elif state['closing']:
            text.set('正在正常关闭本轮组件与服务，请等待。')
        else:
            proof_file = paths.home / 'runtime/astrbot-health.json'
            proof = read(proof_file) if proof_file.exists() else {}
            online = (state['ready'] and proof.get('verified') and proof.get('online')
                      and proof.get('plugin_version') == '0.7.2' and 0 <= time.time()-proof.get('at', 0) < 15)
            case = position_for(out)
            text.set(('已核对Bot与专用测试QQ，入口在线。' if online else '等待QQ登录及OneBot在线；暂勿发送。')
                     + '\n当前位置：' + ((case or {}).get('id') or '六项已结束')
                     + '\n启动与重开不生成回复；手机端按当前一步清单发送。')
            atomic_json(paths.home / 'runtime/qq-listening.json', {'online': bool(online), 'next': (case or {}).get('id'),
                                                                    'at': timestamp(), 'startup_generation_calls': 0})
        window.after(750, tick)
    threading.Thread(target=start, name='qq-acceptance-start', daemon=True).start()
    window.after(200, tick)
    window.mainloop()
    require(state.get('closed'), 'window_closed_without_cleanup')
    return report(out)


def close(out=OUT, *, qq_cleared=False, review_complete=False):
    """Only an explicit, reviewed cleanup; no deletion fallback on refusal."""
    out = Path(out)
    owner, paths = verify(out)
    require(qq_cleared and review_complete, 'review_and_qq_cleanup_required')
    require(not any(unresolved(paths).values()), 'cleanup_unresolved')
    controller = ServiceController(paths)
    require(not controller.has_resident_processes(), 'cleanup_running')
    require(not any(port_open(ConfigStore(paths).load()[k]) for k in ('service_port', 'napcat_port', 'astrbot_port', 'onebot_port')), 'cleanup_ports_in_use')
    # Existing identity lifecycle restores only this disposable home's settings.
    QQTesting(paths).restore()
    summary = report(out)
    summary.update(cleanup='pending', mobile_confirmation='confirmed', qq_visible_records='cleared_by_user',
                   platform_backend_retention='not_claimed_deleted', private_bodies_retained=False)
    atomic_json(out / 'settlement.json', summary)
    PrivatePersonas(RuntimePaths(Path(owner['source_home']))).cleanup_sandbox(paths.home)
    control = safe(owner['control_root'], Path(owner['source_home']) / 'tmp' / NAMESPACE)
    require(control.parent == Path(owner['source_home']) / 'tmp' / NAMESPACE, 'cleanup_control_scope')
    shutil.rmtree(control)
    summary['cleanup'] = 'complete'
    atomic_json(out / 'settlement.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'listen', 'report', 'verify', 'close'))
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--reopen', action='store_true')
    parser.add_argument('--qq-cleared', action='store_true')
    parser.add_argument('--review-complete', action='store_true')
    args = parser.parse_args()
    if args.action in ('report', 'verify') and args.out.resolve() == OUT.resolve() and (OUT / 'current-state.json').exists():
        args.out = safe(read(OUT / 'current-state.json').get('active_run', str(OUT)), OUT)
    if args.action == 'simulate': value = asyncio.run(simulation(args.out))
    elif args.action == 'prepare': value = prepare(args.out, args.home)
    elif args.action == 'listen': value = listen(args.out, reopen=args.reopen)
    elif args.action == 'close': value = close(args.out, qq_cleared=args.qq_cleared, review_complete=args.review_complete)
    else:
        with offline():
            if args.action == 'verify':
                if (args.out / 'closure.json').exists():
                    value = verify_closed(args.out)
                else:
                    owner, paths = verify(args.out)
                    value = {'frozen': True, 'status': owner['status'], 'unresolved': unresolved(paths)}
            else: value = report(args.out)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
