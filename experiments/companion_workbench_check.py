"""Artificial, offline acceptance of the normal workbench and loopback API.

This is a test fixture, never a production launcher. It has no paid client or
private-role source. Only its own loopback server may receive network traffic.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import shutil
import socket
import threading
import time
from unittest.mock import patch

from aiohttp import web

from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_embedding import CharacterEmbeddingProvider, safe_path
from persona_runtime.character_retrieval import load_config
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-workbench-check-v1'
BASE = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-4A-workbench-integration/offline'


def checked_root(root, *, require=True):
    root = safe_path(BASE, Path(root).absolute())
    if root == BASE or not root.is_relative_to(BASE):
        raise RuntimeErrorBase('offline_workbench_path_invalid')
    if require:
        marker = json.loads((root / 'ownership.json').read_text(encoding='utf-8'))
        if marker != {'version': VERSION, 'root': str(root), 'artificial_only': True}:
            raise RuntimeErrorBase('offline_workbench_ownership_invalid')
    return root


class NetworkFence:
    """Block real clients and outbound sockets; admit only the owned API port."""
    def __init__(self):
        self.port = None
        self.blocked = []
        self.stack = ExitStack()

    def __enter__(self):
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_lookup = socket.getaddrinfo
        original_async_connection = asyncio.BaseEventLoop.create_connection

        def allowed(address):
            if (not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1')
                    or address[1] != self.port):
                self.blocked.append('external_socket_blocked')
                raise RuntimeErrorBase('offline_external_network_blocked')

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6): allowed(address)
            return original_connect(sock, address)

        def connect_ex(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6): allowed(address)
            return original_connect_ex(sock, address)

        def lookup(host, port, *args, **kwargs):
            allowed((host, int(port)))
            return original_lookup(host, port, *args, **kwargs)

        async def paid(*args, **kwargs):
            self.blocked.append('real_client_blocked')
            raise RuntimeErrorBase('offline_real_client_blocked')

        async def create_connection(loop, factory, host=None, port=None, **kwargs):
            supplied = kwargs.get('sock')
            allowed(supplied.getpeername() if supplied is not None else (host, int(port)))
            return await original_async_connection(loop, factory, host, port, **kwargs)

        # Proactor performs numeric-IP connects through overlapped I/O, bypassing
        # Python socket.connect and DNS. aiohttp can also connect before creating
        # its transport, so fence both async entrypoints before any dispatch.
        from asyncio.selector_events import BaseSelectorEventLoop
        loop_classes = [BaseSelectorEventLoop]
        try:
            from asyncio.proactor_events import BaseProactorEventLoop
            loop_classes.append(BaseProactorEventLoop)
        except ImportError:
            pass
        for cls in loop_classes:
            original_sock_connect = cls.sock_connect
            def guarded(original):
                async def sock_connect(loop, sock, address):
                    allowed(address)
                    return await original(loop, sock, address)
                return sock_connect
            self.stack.enter_context(patch.object(cls, 'sock_connect', guarded(original_sock_connect)))

        self.stack.enter_context(patch('socket.socket.connect', connect))
        self.stack.enter_context(patch('socket.socket.connect_ex', connect_ex))
        self.stack.enter_context(patch('socket.getaddrinfo', lookup))
        self.stack.enter_context(patch('asyncio.BaseEventLoop.create_connection', create_connection))
        self.stack.enter_context(patch('persona_runtime.chat.ChatCompletionsClient.complete', paid))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


class ArtificialClient:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.fixture = None
        self.store = None

    async def complete(self, model, messages):
        wire, contract = build_request(model, messages)
        row = {'id': identifier(), 'started_at': timestamp(), 'kind': 'mock',
               'messages': deepcopy(messages), 'message_hash': fingerprint(messages),
               'wire': wire, 'wire_hash': fingerprint(wire), 'contract': contract,
               'status': 'reserved', 'real_call': False}
        if self.store is not None:
            matches = []
            with self.store.connection() as db:
                for attempt in db.execute("SELECT id,turn_id,usage_json FROM attempts WHERE status='running'"):
                    audit = json.loads(attempt['usage_json'] or '{}').get('generation_retry', {}).get('attempts', [])
                    for physical in audit:
                        if physical['status'] == 'running' and physical['request_hash'] == row['wire_hash']:
                            matches.append({'turn_id': attempt['turn_id'], 'attempt_id': attempt['id'],
                                            'physical_id': physical['id']})
            if len(matches) != 1: raise RuntimeErrorBase('offline_call_binding_ambiguous')
            row.update(matches[0])
        self.calls.append(row)
        path = self.root / 'calls' / (row['id'] + '.json')
        atomic_json(path, row)
        try:
            text = messages[-1]['content']
            if '[等待取消]' in text:
                await asyncio.Event().wait()
            fixture, self.fixture = self.fixture, None
            contents = fixture or [{'content': '这是人工模拟回复，用于检查工作台接线。',
                                    'content_kind': 'chat', 'cancel_if_new_input': False}]
            if model.get('_reply_mode') == 'natural_structured':
                from persona_runtime.reply_batch import expression_version
                schema = 3 if expression_version(model.get('_expression_snapshot')) == 3 else 2
                response = json.dumps({'schema_version': schema, 'behavior_intent': 'respond',
                                       'messages': contents}, ensure_ascii=False)
            else:
                response = '这是旧入口的人工模拟回复。'
            row.update(status='completed', completed_at=timestamp(), response=response,
                       usage={'total_tokens': 1, 'mock': True})
            atomic_json(path, row)
            return response, {'total_tokens': 1}
        except BaseException as exc:
            row.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                       completed_at=timestamp(), error=type(exc).__name__)
            atomic_json(path, row)
            raise


def configure_local(engine, paths):
    identity = load_config()['model_fingerprint']
    provider = CharacterEmbeddingProvider(paths, paths.home / 'models/character-embedding' / identity)
    provider.identity()
    runtime = engine.cognitive.characters
    if runtime.indexes:
        raise RuntimeErrorBase('offline_embedding_setup_order')
    runtime.index_factory = lambda _: CharacterSemanticIndex(engine.store, provider)
    return provider


def prepare(root=None):
    root = checked_root(root or BASE / identifier(), require=False)
    if root.exists(): raise RuntimeErrorBase('offline_workbench_directory_exists')
    root.mkdir(parents=True)
    atomic_json(root / 'ownership.json', {'version': VERSION, 'root': str(root), 'artificial_only': True})
    paths = RuntimePaths(root / 'home')
    config = ConfigStore(paths)
    config.initialize()
    value = config.load()
    value['model'].update(base_url='https://offline.invalid/v1', name='grok-4.7',
                          encrypted_key='', max_tokens=8192, timeout=180, hourly_limit=0,
                          max_input_chars=24000)
    value['character_selector'].update(enabled=False, base_url='', name='', encrypted_key='',
                                       hourly_limit=0, daily_limit=0)
    value['organization'].update(daily_enabled=False, reuse_chat=False)
    value['interaction']['cancel_queued'] = False
    value['bot_qq'] = value['owner_qq'] = ''
    atomic_json(config.file, value)
    store = Store(paths); store.initialize()
    service = RuntimeService(store)
    personas = []
    for name, trait in (('人工验收甲', '关注条件与过程'), ('人工验收乙', '坦率表达选择与取舍')):
        spec = PersonaSpec(1, name, '阶段4-A人工角色，不对应任何私人设定。', [trait],
                           '根据交流内容决定表达量。', ['尊重明确更正与拒绝'],
                           '人工书店的普通工作人员，与对方无预设共同经历。')
        persona = service.create_persona(spec)
        service.use_persona(persona['id'])
        data = empty_character()
        data['social']['public_image'] = trait
        data['lorebook'] = [{'id': 'blue-corner', 'title': '人工书店', 'category': 'place',
                             'keywords': ['书店'], 'text': '人工书店有一处蓝色阅读角。',
                             'priority': 5, 'enabled': True, 'visibility': 'conversation',
                             'dependencies': []}]
        cs = CharacterService(store)
        cs.update(persona['id'], data, cs.current(persona['id'])['id'], identifier())
        personas.append({'id': persona['id'], 'name': name})
    service.use_persona(personas[0]['id'])
    identity = load_config()['model_fingerprint']
    source = safe_path(DEFAULT_HOME, DEFAULT_HOME / 'models/character-embedding' / identity)
    target = paths.home / 'models/character-embedding' / identity
    if not (source / 'manifest.json').is_file():
        atomic_json(root / 'preparation.json', {'status': 'blocked', 'reason': 'embedding_unavailable'})
        raise RuntimeErrorBase('embedding_unavailable')
    for child in source.rglob('*'): safe_path(source, child)
    shutil.copytree(source, target)
    from persona_runtime.chat import ChatEngine
    from persona_runtime.ingress import IngressService
    client = ArtificialClient(root)
    with NetworkFence():
        engine = ChatEngine(store, config, client)
        try:
            configure_local(engine, paths)
            store.character_committed = None
            for persona in personas:
                version = CharacterService(store).current(persona['id'])
                engine.cognitive.characters.index(load_config()).build(persona['id'], version['id'])
            ingress = IngressService(engine)
            current = ingress.current_policy(personas[0]['id'], 'desktop')
            ingress.update_policy(personas[0]['id'], 'desktop', 'natural_batch', current['id'], identifier(),
                                  conversation_policy_version=2, reply_mode='natural_structured',
                                  disposition_mode='shadow', context_mode='working_set')
        finally:
            engine.cognitive.characters.close(); engine.cognitive.shadow.close()
    result = {'version': VERSION, 'status': 'prepared', 'root': str(root), 'home': str(paths.home),
              'personas': personas, 'local_model_fingerprint': identity, 'real_calls': 0,
              'mock_calls_at_prepare': len(client.calls), 'private_data': False,
              'source_weights': 'read_only_copy', 'candidate_initially_enabled': False}
    atomic_json(root / 'preparation.json', result)
    return result


class OwnedController:
    """Never delegates lifecycle or component preparation to the real controller."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.notify = lambda text: None

    def owned(self, role): return role == 'service' and self.runtime.running

    def snapshot(self):
        return {role: {'role': role, 'owned': self.owned(role),
                       'state': '运行' if self.owned(role) else '离线验收：未启用',
                       'reason': '仅人工本机服务，无QQ连接'}
                for role in ('service', 'astrbot', 'napcat')}

    @staticmethod
    def reject(*args, **kwargs):
        raise RuntimeErrorBase('离线验收不启动组件或执行全局启停；关闭本窗口只结束本次人工服务。')

    start = stop = start_all = stop_all = force_stop_all = force_stop_qq = management_url = reject


class OfflineRuntime:
    def __init__(self, root):
        self.root = checked_root(root)
        self.prepared = json.loads((self.root / 'preparation.json').read_text(encoding='utf-8'))
        if self.prepared.get('status') != 'prepared': raise RuntimeErrorBase('offline_preparation_incomplete')
        self.paths = RuntimePaths(self.root / 'home')
        self.config = ConfigStore(self.paths)
        self.mock = ArtificialClient(self.root)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='artificial-workbench-api', daemon=True)
        self.running = False
        self.fence = NetworkFence()
        self.stack = ExitStack()
        self.runner = None
        self.app = None

    def __enter__(self):
        self.stack.enter_context(self.fence)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self.start(), self.loop).result(60)
            self.client = LocalClient(self.config)
            atomic_json(self.root / 'window-state.json', {'status': 'running', 'port': self.fence.port,
                        'started_at': timestamp(), 'startup_mock_calls': len(self.mock.calls), 'real_calls': 0})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    async def start(self):
        cfg = self.config.load()
        if (cfg['model']['encrypted_key'] or cfg['character_selector']['encrypted_key']
                or cfg['organization']['model']['encrypted_key'] or cfg['owner_qq'] or cfg['bot_qq']):
            raise RuntimeErrorBase('offline_credentials_or_qq_forbidden')
        self.app = create_app(self.paths, self.mock)
        self.mock.store = self.app['store']
        configure_local(self.app['engine'], self.paths)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.fence.port = site._server.sockets[0].getsockname()[1]
        self.config.update({'service_port': self.fence.port})
        self.running = True

    async def stop(self):
        if self.app is not None:
            engine = self.app['engine']; engine.draining = True
            with engine.store.connection() as db:
                batches = [dict(row) for row in db.execute("SELECT * FROM input_batches WHERE state IN ('collecting','interrupted_buffer','sealed_waiting_capacity')")]
            for batch in batches:
                engine.service.use_persona(batch['persona_id'])
                self.app['ingress'].action(batch['id'], 'cancel', batch['revision'], identifier(), batch['persona_id'])
            # A scheduled coroutine can be cancelled before _run starts and thus
            # before its exception handler exists. Persist queued cancellation
            # first, instead of relying on task.cancel to settle the database.
            with engine.store.connection() as db:
                queued = [r[0] for r in db.execute("SELECT id FROM turns WHERE status='queued'")]
            # No await between queued entries: another queued legacy request
            # must not become running while the first cancellation is awaited.
            for turn_id in queued: engine.cancel_queued(turn_id)
            for task in list(engine.tasks.values()): task.cancel('offline_window_closed')
            await engine.wait()
        if self.runner is not None: await self.runner.cleanup()
        self.running = False

    def __exit__(self, *args):
        try:
            if self.thread.is_alive():
                asyncio.run_coroutine_threadsafe(self.stop(), self.loop).result(60)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
            self.stack.close()
            if not self.thread.is_alive(): self.loop.close()
        atomic_json(self.root / 'window-state.json', {'status': 'closed', 'closed_at': timestamp(),
                    'thread_stopped': not self.thread.is_alive(), 'network_blocked': self.fence.blocked,
                    'real_calls': 0, 'mock_calls_this_session': len(self.mock.calls)})
        if self.thread.is_alive(): raise RuntimeErrorBase('offline_api_thread_not_stopped')


def enable(client, pid):
    policy = client.manage('interaction.current', persona_id=pid, channel='desktop')
    return client.manage('interaction.update', persona_id=pid, channel='desktop', mode='natural_batch',
                         expected_version=policy['id'], operation_id=identifier(),
                         companion_mode='minimal_structural', conversation_policy_version=2,
                         expression_policy_version=3, reply_mode='natural_structured',
                         disposition_mode='baseline', situation_observation_mode='disabled',
                         character_retrieval_mode='hybrid')


def submit_and_wait(runtime, pid, text):
    receipt = runtime.client.submit_input({'channel': 'desktop', 'persona_id': pid,
                                          'text': text, 'request_id': identifier()})
    batch = runtime.client.call('GET', '/v2/batches/' + receipt['batch_id'])
    if batch['state'] == 'collecting':
        runtime.client.call('POST', '/v2/batches/' + batch['id'] + '/seal',
                            {'expected_revision': batch['revision'], 'operation_id': identifier(), 'persona_id': pid})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        batch = runtime.client.call('GET', '/v2/batches/' + receipt['batch_id'])
        if batch.get('turn_id'):
            turn = runtime.client.call('GET', '/v1/requests/' + batch['turn_id'])
            if turn['status'] not in ('queued', 'running'): return turn
        time.sleep(.05)
    raise RuntimeErrorBase('offline_simulation_did_not_settle')


def behavior_settings(policy):
    """Ignore only regenerated expression-policy identity, not behavioral values."""
    data = json.loads(policy['data_json'])
    if isinstance(data.get('expression'), dict): data['expression'].pop('policy_id', None)
    return {'mode': policy['mode'], 'reply_mode': policy['reply_mode'], 'data': data}


def wait_local_ready(runtime, pid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = runtime.client.manage('character.index-status', persona_id=pid)
        if status['preparation']['state'] == 'ready' and status['cache']['state'] == 'ready': return status
        if status['preparation']['state'] == 'failed': raise RuntimeErrorBase('offline_local_preparation_failed')
        time.sleep(.05)
    raise RuntimeErrorBase('offline_local_preparation_pending')


def simulate(root):
    root = checked_root(root)
    results = []
    with OfflineRuntime(root) as runtime:
        for persona in runtime.prepared['personas']:
            pid = persona['id']; runtime.client.manage('persona.use', persona_id=pid)
            before = runtime.client.manage('interaction.current', persona_id=pid, channel='desktop')
            policy = enable(runtime.client, pid)
            readiness = wait_local_ready(runtime, pid)
            cases = [('single', '人工书店今天有阵雨，先说一句就好。', ['雨停了再出门。']),
                     ('multi', '请分五条说明人工检查步骤。', ['检查输入。','读取设置。','核对版本。','发送模拟消息。','保存结果。']),
                     ('correction', '更正：书店只整理了一排，整体还没完成。', ['那一排先告一段落。']),
                     ('code', '给出可以运行的完整Python加法示例。', ['def add(a, b):\n    return a + b\n\nif __name__ == "__main__":\n    print(add(2, 3))'])]
            for name, text, contents in cases:
                runtime.mock.fixture = [{'content': part, 'content_kind': 'code' if name == 'code' else 'chat',
                                         'cancel_if_new_input': False} for part in contents]
                turn = submit_and_wait(runtime, pid, text)
                results.append({'persona_id': pid, 'case': name, 'turn_id': turn['id'],
                                'status': turn['status'], 'error': turn.get('error_code')})
                if turn['status'] != 'completed': raise RuntimeErrorBase('offline_case_failed:' + str(turn.get('error_code')))
                with runtime.app['store'].connection() as db:
                    raw = [r[0] for r in db.execute('SELECT s.text FROM reply_segments s JOIN reply_batches b ON b.id=s.batch_id WHERE b.turn_id=? ORDER BY s.segment_index', (turn['id'],))]
                    display = [dict(r) for r in db.execute('SELECT d.text,d.diagnostics_json FROM reply_display_messages d JOIN reply_batches b ON b.id=d.batch_id WHERE b.turn_id=? ORDER BY d.display_index', (turn['id'],))]
                if raw != contents or len(display) != len(contents): raise RuntimeErrorBase('offline_reply_content_changed')
                if name == 'code' and [r['text'] for r in display] != contents: raise RuntimeErrorBase('offline_code_display_changed')
                results[-1].update(raw_exact=True, bubble_count=len(display), display=display,
                                   code_exact=name == 'code', local_readiness=readiness)
            current = runtime.client.manage('interaction.current', persona_id=pid, channel='desktop')
            restored = runtime.client.manage('interaction.update', persona_id=pid, channel='desktop', mode=current['mode'],
                                             expected_version=current['id'], operation_id=identifier(), companion_mode='disabled')
            if behavior_settings(before) != behavior_settings(restored): raise RuntimeErrorBase('offline_restore_behavior_changed')
            results.append({'persona_id': pid, 'case': 'restore', 'restored_companion': restored.get('companion'),
                            'before': before, 'enabled': policy, 'restored': restored})
    atomic_json(root / 'simulation.json', {'status': 'passed', 'cases': results, 'real_calls': 0})
    return verify(root)


def verify(root):
    root = checked_root(root)
    paths = RuntimePaths(root / 'home')
    store = Store(paths)
    calls = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((root / 'calls').glob('*.json'))]
    with store.connection() as db:
        manifests = [dict(r) for r in db.execute('SELECT attempt_id,turn_id,manifest_json FROM context_manifests')]
        attempts = [dict(r) for r in db.execute('SELECT id,turn_id,status,usage_json FROM attempts')]
        turns = [dict(r) for r in db.execute('SELECT id,status,error_code FROM turns')]
        pending = db.execute("SELECT count(*) FROM turns WHERE status IN ('running','queued')").fetchone()[0]
        reservations = db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
        auxiliary = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('character_selection_attempts', 'organization_attempts', 'situation_observation_attempts')}
        batches = db.execute("SELECT count(*) FROM input_batches WHERE state IN ('collecting','interrupted_buffer','sealed_waiting_capacity')").fetchone()[0]
    comparisons = []
    claimed = set()
    for call in calls:
        linked = []
        for attempt in attempts:
            if call.get('attempt_id') and call['attempt_id'] != attempt['id']: continue
            if call.get('turn_id') and call['turn_id'] != attempt['turn_id']: continue
            physical_rows = json.loads(attempt['usage_json'] or '{}').get('generation_retry', {}).get('attempts', [])
            for physical in physical_rows:
                if call.get('physical_id') and call['physical_id'] != physical['id']: continue
                if physical['request_hash'] != call['wire_hash']: continue
                if 'response' in call and physical.get('output_fingerprint') != hashlib.sha256(call['response'].encode('utf-8')).hexdigest(): continue
                owned_manifests = [json.loads(m['manifest_json']) for m in manifests
                                   if m['attempt_id'] == attempt['id'] and m['turn_id'] == attempt['turn_id']]
                linked.append((attempt, physical, owned_manifests))
        unique = len(linked) == 1 and linked[0][1]['id'] not in claimed
        if unique: claimed.add(linked[0][1]['id'])
        comparisons.append({'call_id': call['id'], 'status': call['status'],
            'turn_id': linked[0][0]['turn_id'] if unique else None,
            'attempt_id': linked[0][0]['id'] if unique else None,
            'physical_id': linked[0][1]['id'] if unique else None,
            'binding_method': 'dispatch_ids' if call.get('physical_id') else 'unique_wire_and_output_hash',
            'unique_audit_binding': unique,
            'messages_unchanged': fingerprint(call['messages']) == call['message_hash'],
            'wire_unchanged': fingerprint(call['wire']) == call['wire_hash'],
            'manifest_matches': unique and len(linked[0][2]) == 1
                                and linked[0][2][0]['input_snapshot_hash'] == call['message_hash']
                                and linked[0][2][0].get('prompt_assembly', {}).get('input_snapshot_hash') == call['message_hash'],
            'retrieval': linked[0][2][0].get('character_retrieval') if unique and linked[0][2] else None})
    expected_physical = {physical['id'] for attempt in attempts
                         for physical in json.loads(attempt['usage_json'] or '{}').get('generation_retry', {}).get('attempts', [])}
    result = {'version': VERSION, 'mock_calls': len(calls), 'real_calls': 0, 'qq_calls': 0,
              'turns': turns, 'pending_turns': pending, 'pending_batches': batches,
              'active_reservations': reservations, 'auxiliary_attempts': auxiliary,
              'request_comparisons': comparisons, 'private_data': False,
              'audit_physical_count': len(expected_physical), 'ledger_coverage_exact': claimed == expected_physical,
              'passed': not pending and not batches and not reservations and not any(auxiliary.values())
                        and claimed == expected_physical
                        and all(c['unique_audit_binding'] and c['messages_unchanged'] and c['wire_unchanged'] and c['manifest_matches']
                                and c['status'] != 'reserved' for c in comparisons)}
    atomic_json(root / 'verification.json', result)
    return result


def supplementary_check(root):
    """One new artificial request; never replay an existing or unknown position."""
    root = checked_root(root)
    result_path = root / 'supplementary-check.json'
    if result_path.exists():
        existing = json.loads(result_path.read_text(encoding='utf-8'))
        if existing.get('status') == 'passed': return existing
        raise RuntimeErrorBase('offline_supplement_requires_review_no_replay')
    state = json.loads((root / 'window-state.json').read_text(encoding='utf-8'))
    if state.get('status') != 'closed' or not state.get('thread_stopped'):
        raise RuntimeErrorBase('offline_window_not_closed')
    port = ConfigStore(RuntimePaths(root / 'home')).load()['service_port']
    with socket.socket() as sock:
        sock.settimeout(.3)
        if sock.connect_ex(('127.0.0.1', port)) == 0:
            raise RuntimeErrorBase('offline_previous_port_still_owned_or_occupied')
    result = {'status': 'preparing', 'started_at': timestamp(), 'new_mock_positions': 1,
              'real_calls': 0, 'checks': {}}
    atomic_json(result_path, result)
    try:
        with OfflineRuntime(root) as runtime:
            pid = runtime.prepared['personas'][0]['id']
            runtime.client.manage('persona.use', persona_id=pid)
            before = runtime.client.manage('interaction.current', persona_id=pid, channel='desktop')
            policy = enable(runtime.client, pid)
            wait_local_ready(runtime, pid)
            store = runtime.app['store']; engine = runtime.app['engine']
            prior_cases = json.loads((root / 'simulation.json').read_text(encoding='utf-8'))['cases']
            old_turn_id = next(c['turn_id'] for c in prior_cases if c['persona_id'] == pid and c['case'] == 'correction')
            with store.connection() as db:
                old = dict(db.execute('SELECT * FROM turns WHERE id=?', (old_turn_id,)).fetchone())
                reference_id = db.execute('SELECT event_id FROM input_batch_members WHERE batch_id=? ORDER BY member_order',
                                          (old['input_batch_id'],)).fetchone()[0]
                reference_text = db.execute('SELECT text FROM events WHERE id=?', (reference_id,)).fetchone()[0]
            old_profile = json.loads(old['profile_snapshot'])
            current_expression = runtime.client.manage('profile.current', kind='expression')
            if current_expression['revision'] != 2 or old_profile['expression']['revision'] != 1:
                raise RuntimeErrorBase('offline_expected_artificial_profile_versions_missing')
            member_texts = ['书店整间都整理完了。',
                           '更正，刚才说错了：只整理完蓝色阅读角，其他区域还没动。结合引用的旧消息解释局部与整体，不要庆祝。']
            paragraphs = [
                '这里应当分开看局部步骤和整体目标。蓝色阅读角已经整理完，是一个明确的局部进展；其他区域尚未开始，所以书店整体仍然处于进行中。当前说明已经修正了前一条“整间完成”的说法，后续交流采用最后这条更正。',
                '引用的旧消息仍然有自己的时间位置。它可以帮助识别这次交流在谈什么，却不应该覆盖后来明确补充的情况。同一件事在不同时刻可能有不同进度，记录保留旧说法和当前采用新状态并不冲突。',
                '本次没有必要要求对方再证明已经整理了哪些具体物件。交流需要区分的对象已经给出，就是阅读角与书店其他区域；只要不把其中一个局部替换成整个店面，就能完整解释这个差别。',
                '对方同时明确表示不要庆祝，这属于当前交流意愿。尊重它并不需要机械复述“我将遵守你的要求”，也不意味着否认已经完成的工作。可以直接围绕局部进展继续说话，把庆祝和建议留作没有发生的可选动作。',
                '如果以后其他区域也完成，整体状态才会随新的说明更新。当前没有提供完成时间，也没有交代下一步计划，因此不应该把假设的日程写成已经确定的安排。保持这些未知不会妨碍眼前这段普通交流。',
                '这段长解释只是人工验收资料，用来检查完整内容能否经过原始回复、协议校验和工作台展示。它不代表真实模型理解已经通过，也不拿句子数量、气泡长短或标点多少评价自然度。完整传递和表达好不好是两个分别记录的问题。',
                '验收记录应当能够找到连续输入的先后次序、被引用的原始事件，以及这次受理时的人物表达版本。旧请求继续保留旧版本，新请求使用保存后的新版本；界面中的资料更新不应改写早先已经保存的调用证据。']
            answer = '\n\n'.join(paragraphs)
            if not 500 < len(answer) < 4000: raise RuntimeErrorBase('offline_long_fixture_invalid')
            runtime.mock.fixture = [{'content': answer, 'content_kind': 'technical', 'cancel_if_new_input': False}]
            first = runtime.client.submit_input({'channel': 'desktop', 'persona_id': pid, 'text': member_texts[0],
                                                 'request_id': identifier(), 'reference': {'kind': 'event', 'id': reference_id}})
            second = runtime.client.submit_input({'channel': 'desktop', 'persona_id': pid, 'text': member_texts[1],
                                                  'request_id': identifier()})
            if first['batch_id'] != second['batch_id']: raise RuntimeErrorBase('offline_continuous_members_not_same_batch')
            batch = runtime.client.call('GET', '/v2/batches/' + first['batch_id'])
            runtime.client.call('POST', '/v2/batches/' + batch['id'] + '/seal',
                               {'expected_revision': batch['revision'], 'operation_id': identifier(), 'persona_id': pid})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                batch = runtime.client.call('GET', '/v2/batches/' + first['batch_id'])
                if batch.get('turn_id'):
                    turn = runtime.client.call('GET', '/v1/requests/' + batch['turn_id'])
                    if turn['status'] not in ('queued', 'running'): break
                time.sleep(.05)
            else: raise RuntimeErrorBase('offline_supplement_not_settled')
            if turn['status'] != 'completed' or len(runtime.mock.calls) != 1:
                raise RuntimeErrorBase('offline_supplement_completion_invalid')
            actual = runtime.mock.calls[0]
            with store.connection() as db:
                raw = dict(db.execute('SELECT * FROM turns WHERE id=?', (turn['id'],)).fetchone())
                members = [r[0] for r in db.execute('SELECT e.text FROM input_batch_members m JOIN events e ON e.id=m.event_id WHERE m.batch_id=? ORDER BY m.member_order', (raw['input_batch_id'],))]
                manifest = json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE attempt_id=?', (actual['attempt_id'],)).fetchone()[0])
                reference = json.loads(db.execute('SELECT binding_json FROM input_references WHERE event_id=?', (batch['members'][0]['event_id'],)).fetchone()[0])
                segments = [r[0] for r in db.execute('SELECT s.text FROM reply_segments s JOIN reply_batches b ON b.id=s.batch_id WHERE b.turn_id=? ORDER BY s.segment_index', (turn['id'],))]
                display = [dict(r) for r in db.execute('SELECT d.text,d.diagnostics_json FROM reply_display_messages d JOIN reply_batches b ON b.id=d.batch_id WHERE b.turn_id=? ORDER BY d.display_index', (turn['id'],))]
                old_after = dict(db.execute('SELECT * FROM turns WHERE id=?', (old_turn_id,)).fetchone())
            profile = json.loads(raw['profile_snapshot'])
            joined = '\n'.join(m['content'] for m in actual['messages'])
            checks = {'members_in_order': members == member_texts,
                      'both_members_in_actual_input': all(t in actual['messages'][-1]['content'] for t in member_texts),
                      'reference_bound_found': reference['status'] == 'found',
                      'reference_text_in_actual_input': reference_text in joined,
                      'reference_manifest_found': any(r.get('status') == 'found' and r.get('turn_id') == old_turn_id
                                                       for r in manifest['prompt_assembly']['source_refs']['references']['references']),
                      'new_expression_v2_frozen': profile['expression']['id'] == current_expression['id'],
                      'new_expression_body_consumed': current_expression['data']['style'] in actual['messages'][0]['content'],
                      'old_expression_v1_unchanged': old_after['profile_snapshot'] == old['profile_snapshot'],
                      'long_reply_raw_exact': segments == [answer],
                      'long_reply_display_exact': [d['text'] for d in display] == [answer]}
            if not all(checks.values()): raise RuntimeErrorBase('offline_supplement_assertion_failed:' + ','.join(k for k,v in checks.items() if not v))
            # This is a fixed-input offline rendering equivalence check, not a
            # replay of old paid/private requests or a full historical baseline.
            from persona_runtime.context_policy import snapshot as policy_snapshot
            clean = deepcopy(policy); data = json.loads(clean['data_json']); data.pop('companion_restore', None)
            clean['data_json'] = json.dumps(data, ensure_ascii=False)
            with store.connection() as db:
                context_with = policy_snapshot(policy, db); context_without = policy_snapshot(clean, db)
            if context_with != context_without: raise RuntimeErrorBase('offline_restore_metadata_leaked_to_snapshot')
            evidence = engine.cognitive.dialogue.evidence(raw)
            character = engine.cognitive.characters.prepare(raw, behavior_cues=False)
            rendered = []
            for context in (context_with, context_without):
                row = deepcopy(raw); row['context_policy_snapshot'] = json.dumps(context, ensure_ascii=False)
                with store.connection() as db:
                    messages, _ = engine.cognitive.dialogue.render(db, row, evidence, character)
                model = dict(json.loads(raw['model_snapshot']), _reply_mode=raw['reply_mode_snapshot'],
                             _expression_snapshot=raw['expression_snapshot'])
                rendered.append((messages, build_request(model, messages)[0]))
            compiler_files = ('persona_runtime/context_m4.py', 'persona_runtime/context_builders.py',
                              'persona_runtime/prompt_assembly.py', 'persona_runtime/companion_contract.py',
                              'persona_runtime/provider_compat.py', 'persona_runtime/character_runtime.py',
                              'persona_runtime/cognitive.py')
            source_root = Path(__file__).resolve().parents[1]
            opening = json.loads((BASE.parent / 'opening.json').read_text(encoding='utf-8'))
            unchanged = {f: hashlib.sha256((source_root / f).read_bytes()).hexdigest() == opening['source_hashes'][f]
                         for f in compiler_files}
            equivalence = {'scope': 'same accepted request and fixed evidence/material; restore metadata present/absent',
                           'full_historical_replay': False, 'accepted_context_equal': context_with == context_without,
                           'messages_equal': rendered[0][0] == rendered[1][0], 'wire_equal': rendered[0][1] == rendered[1][1],
                           'message_hash': fingerprint(rendered[0][0]), 'wire_hash': fingerprint(rendered[0][1]),
                           'compiler_and_contract_source_unchanged': unchanged}
            if not all(unchanged.values()) or not equivalence['messages_equal'] or not equivalence['wire_equal']:
                raise RuntimeErrorBase('offline_candidate_equivalence_failed')
            result.update(status='passed', turn_id=turn['id'], old_turn_id=old_turn_id, reference_event_id=reference_id,
                          physical_call_id=actual['physical_id'], checks=checks, equivalence=equivalence,
                          long_answer_characters=len(answer), display=display, completed_at=timestamp(),
                          semantic_correctness='not_evaluated_mock_fixture', mock_calls=1)
            if not before.get('companion'):
                current = runtime.client.manage('interaction.current', persona_id=pid, channel='desktop')
                runtime.client.manage('interaction.update', persona_id=pid, channel='desktop', mode=current['mode'],
                                     expected_version=current['id'], operation_id=identifier(), companion_mode='disabled')
        result['verification'] = verify(root)
        atomic_json(result_path, result)
        return result
    except BaseException as exc:
        result.update(status='stopped', error=type(exc).__name__, reason=str(exc), stopped_at=timestamp())
        atomic_json(result_path, result)
        raise


def open_window(root):
    from persona_runtime.desktop import Workbench
    with OfflineRuntime(root) as runtime:
        window = None
        try:
            window = Workbench(runtime.paths, managed_lifecycle=False, controller=OwnedController(runtime))
            window.title('PCAR · 正常工作台离线验收（人工数据／模拟回复）')
            window.show_page('context')
            window.refresh_personas()
            window.notice.set('仅人工资料和模拟回复；本窗口不连接供应商或QQ。')
            window.mainloop()
        finally:
            if window is not None: window.hub.pool.shutdown(wait=True, cancel_futures=True)
    return verify(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'open', 'verify'))
    parser.add_argument('--root', type=Path)
    args = parser.parse_args(argv)
    if args.action != 'prepare' and args.root is None: parser.error('--root is required')
    result = {'prepare': prepare, 'simulate': simulate, 'open': open_window, 'verify': verify}[args.action](args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
