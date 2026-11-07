"""Normal Workbench on one approved disposable copy; no automatic generation."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from aiohttp import web
from experiments import companion_expression_adoption as adoption
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.context_policy import values
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import expression_version
from persona_runtime.server import create_app
from persona_runtime.storage import Store

TITLE = 'PCAR · 正常工作台短聊（私人副本）'


def prepared_copy(binding_path, source_home):
    paths, _, _, metadata, versions = adoption._saved(binding_path, source_home)
    state = json.loads(Path(binding_path).with_suffix('.copy.json').read_text(encoding='utf-8'))
    if state.get('status') != 'prepared': raise RuntimeErrorBase('workbench_copy_not_prepared')
    marker = adoption._copy_check(state['home'], metadata, versions, paths)
    if marker['persona_id'] != state.get('persona_id'): raise RuntimeErrorBase('workbench_copy_binding_changed')
    return RuntimePaths(Path(state['home'])), marker['persona_id']


def check_start(paths, persona_id, binding_path):
    store = Store(paths); cfg = ConfigStore(paths).load()
    ready = json.loads((paths.home / 'runtime/adoption-workbench-ready.json').read_text(encoding='utf-8'))
    marker = json.loads((paths.home / 'private-sandbox.json').read_text(encoding='utf-8'))
    if (ready.get('status') != 'ready' or ready.get('persona_id') != persona_id
            or Path(ready.get('binding_path', '')).resolve() != Path(binding_path).resolve()
            or ready.get('adoption_sha256') != marker['adoption_sha256']
            or ready.get('source_main_provider_fingerprint') != provider_identity(cfg['model'])
            or ready.get('real_calls_at_prepare') != 0
            or ready.get('qq_enabled') is not False
            or ready.get('background_organization_enabled') is not False):
        raise RuntimeErrorBase('workbench_ready_binding_changed')
    if cfg['owner_qq'] or cfg['bot_qq'] or cfg['organization']['daily_enabled'] or cfg['organization']['reuse_chat']:
        raise RuntimeErrorBase('workbench_copy_background_forbidden')
    guard_cloud_dispatch(store, persona_id, 'main', cfg['model'])
    with store.connection() as db:
        if store.current(db) != persona_id: raise RuntimeErrorBase('workbench_copy_selection_changed')
        for table, condition in (
            ('turns', "status IN ('queued','running')"),
            ('input_batches', "state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')"),
            ('quota_reservations', "state='active'"),
            ('organization_tasks', "status IN ('queued','running','waiting_quota','interrupted')")):
            if db.execute('SELECT 1 FROM '+table+' WHERE '+condition+' LIMIT 1').fetchone():
                raise RuntimeErrorBase('workbench_copy_unfinished_requires_review')
        policy = db.execute('SELECT p.* FROM interaction_heads h JOIN interaction_policies p ON p.id=h.version_id '
                            'WHERE h.persona_id=? AND h.channel=?', (persona_id, 'desktop')).fetchone()
        if not policy: raise RuntimeErrorBase('workbench_companion_policy_missing')
        effective = values(dict(policy))
        if (effective.get('companion') != choose('minimal_structural')
                or effective['disposition_mode'] != 'baseline'
                or effective['situation_observation_mode'] != 'disabled'
                or effective['character_retrieval']['mode'] != 'hybrid'
                or policy['reply_mode'] != 'natural_structured'
                or expression_version(json.loads(policy['data_json']).get('expression'), policy['reply_mode']) != 3):
            raise RuntimeErrorBase('workbench_companion_policy_changed')
        attempts = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
    return attempts


def configure_local(engine, paths, persona_id):
    from persona_runtime.character_embedding import CharacterEmbeddingProvider
    from persona_runtime.character_retrieval import load_config
    from persona_runtime.character_semantic_index import CharacterSemanticIndex
    from experiments.companion_trial import candidate_retrieval_ready
    identity = load_config()['model_fingerprint']
    provider = CharacterEmbeddingProvider(paths, paths.home / 'models/character-embedding' / identity)
    provider.identity()
    runtime = engine.cognitive.characters
    if runtime.indexes: raise RuntimeErrorBase('workbench_embedding_setup_order')
    runtime.index_factory = lambda _: CharacterSemanticIndex(engine.store, provider)
    candidate_retrieval_ready(engine, persona_id, build=False)


class OwnedController:
    def __init__(self, runtime): self.runtime = runtime; self.notify = lambda text: None
    def owned(self, role): return role == 'service' and self.runtime.running
    def snapshot(self):
        return {role: {'role': role, 'owned': self.owned(role),
                      'state': '运行' if self.owned(role) else '未启用',
                      'reason': '本轮私人副本；仅运行所属本机人格服务'}
                for role in ('service', 'astrbot', 'napcat')}
    @staticmethod
    def reject(*args, **kwargs):
        raise RuntimeErrorBase('本轮仅运行私人副本；组件及全局启停不可用，关闭窗口结束本轮服务。')
    start = stop = start_all = stop_all = force_stop_all = force_stop_qq = management_url = reject


class TrialRuntime:
    def __init__(self, binding_path=None, source_home=DEFAULT_HOME, *, client=None, trial_manifest=None, reopen=False):
        self.trial_manifest = Path(trial_manifest) if trial_manifest else None
        self.binding_path = Path(binding_path) if binding_path else None
        self.source_home = Path(source_home)
        self.reopen = reopen
        self.trial_owner = None
        self.started_successfully = False
        if self.trial_manifest:
            if binding_path is not None:
                raise RuntimeErrorBase('workbench_trial_input_conflict')
            from experiments import companion_c2_chat
            owner, _ = companion_c2_chat.verify(self.trial_manifest)
            self.trial_owner = owner
            self.paths, self.persona_id = RuntimePaths(Path(owner['home'])), owner['persona_id']
        else:
            if reopen:
                raise RuntimeErrorBase('workbench_reopen_requires_trial_manifest')
            if self.binding_path is None:
                raise RuntimeErrorBase('workbench_trial_input_missing')
            self.paths, self.persona_id = prepared_copy(self.binding_path, self.source_home)
        self.client = client; self.app = self.runner = None; self.running = False
        self.owns_lifecycle = False
        self.loop = asyncio.new_event_loop(); self.stack = ExitStack()
        self.thread = threading.Thread(target=self.loop.run_forever, name='private-workbench-api', daemon=True)
        self.state_file = self.paths.home / 'runtime/workbench-trial-state.json'

    def __enter__(self):
        try:
            for name in ('service', 'desktop'):
                lock = InstanceLock(self.paths.home / 'runtime' / (name+'.lock'))
                self.stack.callback(lock.close)
            # Check before create_app, whose normal recovery must never replay a breakpoint.
            if self.trial_manifest:
                from experiments import companion_c2_chat
                companion_c2_chat.begin(self.trial_manifest, reopen=self.reopen)
                with Store(self.paths).connection() as db:
                    self.start_attempts = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            else:
                self.start_attempts = check_start(self.paths, self.persona_id, self.binding_path)
            self.owns_lifecycle = True
            self.thread.start()
            asyncio.run_coroutine_threadsafe(self.start(), self.loop).result(60)
            self.started_successfully = True
            atomic_json(self.state_file, {'status': 'running', 'started_at': timestamp(),
                        'startup_generation_calls': 0, 'persona_id': self.persona_id,
                        'port': self.port, 'runtime': 'normal_workbench'})
            return self
        except BaseException:
            try:
                self.__exit__(None, None, None)
            except BaseException:
                # Cleanup records its own failure; preserve the startup cause.
                pass
            raise

    async def start(self):
        self.app = create_app(self.paths, self.client)
        if self.trial_manifest:
            from experiments import companion_c2_chat
            companion_c2_chat.install(self.trial_manifest, self.app['engine'])
        await asyncio.to_thread(configure_local, self.app['engine'], self.paths, self.persona_id)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0); await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        ConfigStore(self.paths).update({'service_port': self.port})
        with self.app['store'].connection() as db:
            if db.execute('SELECT count(*) FROM attempts').fetchone()[0] != self.start_attempts:
                raise RuntimeErrorBase('workbench_startup_unexpected_generation')
        from experiments import companion_c2_lifecycle as lifecycle
        if self.trial_owner and self.trial_owner['version'] == lifecycle.VERSION:
            from experiments.companion_c2_chat import body
            lifecycle.started(self.trial_owner, body(self.trial_owner))
        self.running = True

    async def stop(self):
        if self.app is not None:
            engine = self.app['engine']; engine.draining = True
            with engine.store.connection() as db:
                batches = [dict(r) for r in db.execute("SELECT * FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')")]
            for batch in batches:
                self.app['ingress'].action(batch['id'], 'cancel', batch['revision'], identifier(), batch['persona_id'])
            with engine.store.connection() as db:
                queued = [r[0] for r in db.execute("SELECT id FROM turns WHERE status='queued'")]
            for turn_id in queued: engine.cancel_queued(turn_id)
            for task in list(engine.tasks.values()): task.cancel('private_workbench_closed')
            await engine.wait()
        if self.runner is not None:
            await self.runner.cleanup()
        elif self.app is not None:
            await self.app['disposition_compiler'].drain()
            await self.app['organization'].drain()
            await engine.observer.drain()
            await asyncio.to_thread(engine.cognitive.characters.close)
            await asyncio.to_thread(engine.cognitive.shadow.close)
        self.running = False

    def __exit__(self, *args):
        try:
            if self.thread.is_alive(): asyncio.run_coroutine_threadsafe(self.stop(), self.loop).result(60)
        finally:
            if self.thread.is_alive(): self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join(10)
            self.stack.close()
            if not self.thread.is_alive() and not self.loop.is_closed(): self.loop.close()
        if not self.owns_lifecycle:
            return
        with Store(self.paths).connection() as db:
            pending = db.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0]
            reservations = db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
        clean = not self.thread.is_alive() and pending == 0 and reservations == 0
        atomic_json(self.state_file, {'status': 'closed' if clean else 'cleanup_pending', 'closed_at': timestamp(),
                    'thread_stopped': not self.thread.is_alive(), 'pending_turns': pending,
                    'active_reservations': reservations})
        if not clean: raise RuntimeErrorBase('private_workbench_cleanup_incomplete')
        from experiments import companion_c2_lifecycle as lifecycle
        if self.trial_owner and self.trial_owner['version'] == lifecycle.VERSION:
            lifecycle.close(self.trial_manifest, normal_exit=self.started_successfully and args[0] is None)


def open_window(binding_path=None, source_home=DEFAULT_HOME, *, trial_manifest=None, reopen=False):
    from persona_runtime.desktop import Workbench
    with TrialRuntime(binding_path, source_home, trial_manifest=trial_manifest, reopen=reopen) as runtime:
        window = Workbench(runtime.paths, managed_lifecycle=False, controller=OwnedController(runtime))
        if trial_manifest:
            from experiments.companion_c2_chat import TITLE as title
            from experiments import companion_c2_lifecycle as lifecycle
            if runtime.trial_owner['version'] == lifecycle.VERSION:
                title = lifecycle.TITLE
            from experiments import companion_c2_chat
            if runtime.trial_owner["version"] == companion_c2_chat.PURPOSE_VERSION:
                title = companion_c2_chat.PURPOSE_TITLE
            if runtime.trial_owner["version"] == companion_c2_chat.RULES_VERSION:
                title = companion_c2_chat.RULES_TITLE
            if runtime.trial_owner["version"] == companion_c2_chat.LIGHT_VERSION:
                title = companion_c2_chat.LIGHT_TITLE
            if runtime.trial_owner["version"] == companion_c2_chat.NATIVE_VERSION:
                title = companion_c2_chat.NATIVE_TITLE
            if runtime.trial_owner["version"] == companion_c2_chat.SELECTIVE_VERSION:
                title = companion_c2_chat.SELECTIVE_TITLE
            if runtime.trial_owner["version"] == companion_c2_chat.IM_VERSION:
                title = companion_c2_chat.IM_TITLE
        else:
            title = TITLE
        window.title(title); window.persona_id = runtime.persona_id
        window.show_page('chat'); window.refresh_personas()
        window.notice.set('已加载获批新版私人副本；发送消息会真实调用当前模型，可随时结束。')
        try: window.mainloop()
        finally: window.hub.pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binding', nargs='?', type=Path)
    parser.add_argument('--trial-manifest', type=Path)
    parser.add_argument('--reopen', action='store_true')
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    args = parser.parse_args()
    if bool(args.binding) == bool(args.trial_manifest):
        parser.error('provide either a binding or --trial-manifest')
    open_window(args.binding, args.home, trial_manifest=args.trial_manifest, reopen=args.reopen)


if __name__ == '__main__': main()
