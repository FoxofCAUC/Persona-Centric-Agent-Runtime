"""Small isolated chat window using the normal ingress and ChatEngine.

No QQ connection, background organization, or formal database writes. The
private copies are disposable; their transcript is never a publication asset.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import random
import shutil
import socket
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths, DEFAULT_HOME
from persona_runtime.private_personas import PrivatePersonas, safe, provider_identity
from persona_runtime.storage import Store

STRUCTURAL_MODE = 'minimal_structural'
CANDIDATE_PURPOSE = 'continuous_chat_candidate'


def free_ports():
    sockets = [socket.socket() for _ in range(4)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def configure(paths, source, persona_id):
    """Copy encrypted provider settings, never the formal admin/bridge tokens."""
    config = ConfigStore(paths)
    config.initialize()
    value = config.load()
    value['model'] = deepcopy(source['model'])
    value['model']['hourly_limit'] = 0
    value['character_selector'] = deepcopy(source['character_selector'])
    value['character_selector'].update(hourly_limit=0, daily_limit=0)
    value['organization']['daily_enabled'] = False
    value['organization']['reuse_chat'] = False
    value['interaction']['cancel_queued'] = True
    for key, port in zip(('service_port', 'napcat_port', 'astrbot_port', 'onebot_port'), free_ports()):
        value[key] = port
    value['bot_qq'] = value['owner_qq'] = ''
    ConfigStore.validate(value)
    atomic_json(config.file, value)
    atomic_json(paths.home / 'runtime/companion-trial.json', {
        'version': 1, 'persona_id': persona_id, 'created_at': timestamp(),
        'organization_enabled': False, 'qq_enabled': False,
    })
    return config


def copy_weights(source_paths, paths):
    from persona_runtime.character_policy import choose
    identity = choose('hybrid')['config']['model_fingerprint']
    source = safe(source_paths.home / 'models/character-embedding' / identity, source_paths.home)
    target = paths.home / 'models/character-embedding' / identity
    if not (source / 'manifest.json').is_file():
        raise RuntimeErrorBase('embedding_unavailable')
    for item in source.rglob('*'):
        safe(item, source)
    # Regular copies keep the original model and its cache completely untouched.
    shutil.copytree(source, target)


def configure_policy(store, config, persona_id, mode, *, background_prepare=True):
    from persona_runtime.chat import ChatEngine
    from persona_runtime.ingress import IngressService
    previous_callback = getattr(store, 'character_committed', None)
    engine = ChatEngine(store, config)
    ingress = IngressService(engine)
    if not background_prepare:
        # Batch preparation owns synchronous index construction. Do not leave
        # this short-lived engine's worker holding the process-wide model gate.
        store.character_committed = None
    try:
        current = ingress.current_policy(persona_id, 'desktop')
        ingress.update_policy(persona_id, 'desktop', 'natural_batch', current['id'], identifier(),
            companion_mode=mode, conversation_policy_version=2, expression_policy_version=3,
            disposition_mode='baseline', situation_observation_mode='disabled',
            reply_mode='context_full' if mode in ('minimal_native', 'minimal_im') else 'natural_structured',
            character_retrieval_mode='hybrid_llm' if mode == 'current' else 'hybrid')
    finally:
        if not background_prepare:
            store.character_committed = previous_callback
        engine.cognitive.characters.close()
        engine.cognitive.shadow.close()


def candidate_retrieval_ready(engine, persona_id, *, build):
    """Own the local preparation synchronously; never dispatch a generation."""
    from experiments.companion_comparison import local_retrieval_ready
    return local_retrieval_ready(engine.cognitive.characters, persona_id, ['你好'], build=build)


def prepare_candidate_retrieval(store, config, persona_id):
    from persona_runtime.chat import ChatEngine
    engine = ChatEngine(store, config)
    try:
        return candidate_retrieval_ready(engine, persona_id, build=True)
    finally:
        engine.cognitive.characters.close()
        engine.cognitive.shadow.close()


def prepare(source_home=DEFAULT_HOME, *, private=False, approve_current_provider=False,
            structural_candidate=False):
    if structural_candidate and not private:
        raise RuntimeErrorBase('structural_trial_private_required')
    source_paths = RuntimePaths(Path(source_home).resolve())
    source = ConfigStore(source_paths).load()
    records = PrivatePersonas(source_paths).load(required=private)['records'] if private else []
    if private:
        ready = [row for row in records if row.get('state') == 'ready']
        if len(ready) != 1:
            raise RuntimeErrorBase('private_trial_requires_one_ready_role')
        approved = ready[0].get('approved_models', {})
        if not approve_current_provider and any(approved.get(kind) != provider_identity(source[field])
               for kind, field in (('main', 'model'), ('selector', 'character_selector'))):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    trials = safe(source_paths.home / 'tmp/companion-trials')
    trials.mkdir(parents=True, exist_ok=True)
    root = safe(trials / identifier(), trials)
    root.mkdir()
    modes = [STRUCTURAL_MODE] if structural_candidate else ['current', 'minimal']
    if not structural_candidate:
        random.SystemRandom().shuffle(modes)
    labels = ('C',) if structural_candidate else ('A', 'B')
    manifest = {'version': 1, 'created_at': timestamp(), 'private': private,
                'source_home': str(source_paths.home), 'status': 'preparing', 'arms': {},
                'current_provider_explicitly_approved': bool(approve_current_provider),
                'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                'cleanup_after': 'this_short_chat_round_and_necessary_review'}
    if structural_candidate:
        manifest.update(purpose=CANDIDATE_PURPOSE, cleanup_homes=[],
                        transcript_storage='role_home_only; trial metadata contains no transcript')
    atomic_json(root / 'trial.json', manifest)
    try:
        for label, mode in zip(labels, modes):
            if private:
                copy = PrivatePersonas(source_paths).create_sandbox(ready[0]['id'])
                paths = RuntimePaths(Path(copy['home']))
                store = Store(paths)
                pid = copy['persona_id']
                # Register ownership before any later configuration/readiness can fail.
                manifest['arms'][label] = {'home': str(paths.home), 'mode': mode, 'persona_id': pid}
                if structural_candidate:
                    manifest['cleanup_homes'].append(str(paths.home))
                atomic_json(root / 'trial.json', manifest)
                if approve_current_provider:
                    # A trial-specific user decision never changes the original registry.
                    approved_policy = paths.home / 'runtime/private-copy-policy.json'
                    policy = json.loads(approved_policy.read_text(encoding='utf-8'))
                    policy['approved_models'] = {kind: provider_identity(source[field])
                        for kind, field in (('main', 'model'), ('selector', 'character_selector'))}
                    policy['approval_scope'] = 'this_companion_trial_only'
                    atomic_json(approved_policy, policy)
            else:
                paths = RuntimePaths(root / label)
                store = Store(paths)
                store.initialize()
                from persona_runtime.service import RuntimeService
                service = RuntimeService(store)
                pid = service.create_persona(generic_persona())['id']
                service.use_persona(pid)
            manifest['arms'][label] = {'home': str(paths.home), 'mode': mode, 'persona_id': pid}
            atomic_json(root / 'trial.json', manifest)
            config = configure(paths, source, pid)
            copy_weights(source_paths, paths)
            configure_policy(store, config, pid, mode, background_prepare=not structural_candidate)
            if structural_candidate:
                manifest['local_retrieval'] = prepare_candidate_retrieval(store, config, pid)
                atomic_json(root / 'trial.json', manifest)
    except BaseException as error:
        manifest['status'] = 'preparation_blocked'
        manifest['error_code'] = str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__
        if getattr(error, 'preparation_reason', None):
            manifest['preparation_reason'] = error.preparation_reason
        atomic_json(root / 'trial.json', manifest)
        error.preparation_trial_root = str(root)
        raise
    manifest['status'] = 'prepared'
    atomic_json(root / 'trial.json', manifest)
    return root


def load_trial(root):
    root = Path(root).resolve()
    data = json.loads((root / 'trial.json').read_text(encoding='utf-8'))
    parent = Path(data['source_home']).resolve() / 'tmp/companion-trials'
    safe(root, parent)
    if data.get('version') != 1 or data.get('status') != 'prepared':
        raise RuntimeErrorBase('trial_not_prepared')
    deadline = data.get('review_deadline')
    if deadline and datetime.now(timezone.utc) > datetime.fromisoformat(deadline):
        raise RuntimeErrorBase('trial_review_window_expired_cleanup_required')
    for arm in data['arms'].values():
        home = safe(arm['home'], Path(data['source_home']) / 'tmp')
        marker = json.loads((home / 'runtime/companion-trial.json').read_text(encoding='utf-8'))
        if marker['persona_id'] != arm['persona_id']:
            raise RuntimeErrorBase('trial_scope_mismatch')
    return data


class TrialWorker:
    def __init__(self, home, events):
        self.paths, self.events = RuntimePaths(Path(home)), events
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.accepting = threading.Event()
        self.stopping = False
        self.components_closed = False
        self.thread = threading.Thread(target=self.run, name='companion-trial', daemon=False)
        self.thread.start()

    def run(self):
        asyncio.set_event_loop(self.loop)
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        from persona_runtime.instance import InstanceLock
        lock = None
        try:
            lock = InstanceLock(self.paths.home / 'runtime/service.lock')
            self.engine = ChatEngine(Store(self.paths), ConfigStore(self.paths))
            self.ingress = IngressService(self.engine)
            self.engine.recover()
            self.ingress.recover()
            with self.engine.store.connection() as conn:
                persona_id = self.engine.store.current(conn)
            profile = self.ingress.current_policy(persona_id, 'desktop').get('companion') or {}
            structural = profile.get('format_contract') == 'schema3-structural-v1'
            if structural:
                # The candidate must not advertise readiness while a background
                # loader competes for the same process-wide embedding gate.
                candidate_retrieval_ready(self.engine, persona_id, build=False)
            old = list(reversed(self.engine.requests()))
            self.seen = {turn['id'] for turn in old}
            for turn in old:
                self.events.put(('history', {'user': turn.get('text') or '',
                    'reply': turn.get('display_reply') or turn.get('reply') or '',
                    'error': turn.get('error_code') if turn['status'] != 'completed' else None}))
            if any(turn.get('error_code') == 'service_restarted' for turn in old):
                self.events.put(('status', '上次未完成的请求已标记中断，没有自动重发。'))
            if not structural:
                self.engine.cognitive.characters.notify(None)
            self.accepting.set()
            self.ready.set()
            self.loop.run_until_complete(self.monitor())
        except Exception as error:
            self.events.put(('error', str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__))
            self.ready.set()
        finally:
            self.accepting.clear()
            self.ready.set()
            self.close_components()
            if lock is not None:
                lock.close()
            self.loop.close()
            self.events.put(('closed', ''))

    def close_components(self):
        if not self.components_closed and hasattr(self, 'engine'):
            self.engine.cognitive.characters.close()
            self.engine.cognitive.shadow.close()
            self.components_closed = True

    async def monitor(self):
        seen = self.seen
        try:
            while not self.stopping:
                self.ingress.tick()
                for turn in reversed(self.engine.requests()):
                    if turn['id'] in seen or turn['status'] in ('queued', 'running'):
                        continue
                    seen.add(turn['id'])
                    self.events.put(('reply' if turn['status'] == 'completed' else 'error',
                                     turn.get('display_reply') or turn.get('reply') or turn.get('error_code') or turn['status']))
                await asyncio.sleep(.1)
        finally:
            self.accepting.clear()
            await self.cancel()
            await self.engine.wait()
            self.close_components()

    async def send(self, text):
        if not self.accepting.is_set() or self.stopping:
            self.events.put(('status', '聊天尚未准备好，未发送这条消息。'))
            return False
        try:
            self.ingress.accept({'channel': 'desktop', 'text': text, 'request_id': identifier()})
            return True
        except Exception as error:
            self.events.put(('error', str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__))

    async def cancel(self):
        with self.engine.store.connection() as conn:
            pid = self.engine.store.current(conn)
        for batch in self.ingress.list(pid):
            if batch['state'] in ('collecting', 'sealed_waiting_capacity', 'interrupted_buffer'):
                self.ingress.action(batch['id'], 'cancel', batch['revision'], identifier(), batch['persona_id'])
        for turn in self.engine.requests():
            if turn['status'] in ('queued', 'running'):
                await self.engine.cancel(turn['id'])
        self.events.put(('status', '已取消当前等待或生成；上游用量以实际回执为准。'))


def open_window(root, label):
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    trial = load_trial(root)
    if label not in trial['arms']:
        raise RuntimeErrorBase('trial_label_invalid')
    events = queue.Queue()
    window = tk.Tk()
    window.title('PCAR · 候选短聊' if trial.get('purpose') == CANDIDATE_PURPOSE else 'PCAR · 体验 ' + label)
    window.geometry('820x680')
    worker = TrialWorker(trial['arms'][label]['home'], events)
    ttk.Label(window, text='像平常一样聊天。可以分享、开玩笑，也可以认真讨论。', padding=12).pack(anchor='w')
    transcript = scrolledtext.ScrolledText(window, wrap='word', state='disabled', font=('Microsoft YaHei UI', 11))
    transcript.pack(fill='both', expand=True, padx=12)
    draft = tk.Text(window, height=4, wrap='word', font=('Microsoft YaHei UI', 11))
    draft.pack(fill='x', padx=12, pady=8)
    status = tk.StringVar(value='正在准备聊天…')
    footer = ttk.Frame(window, padding=12)
    footer.pack(fill='x')
    ttk.Label(footer, textvariable=status).pack(side='left')

    def append(who, text):
        transcript.configure(state='normal')
        transcript.insert('end', who + '\n' + text + '\n\n')
        transcript.see('end')
        transcript.configure(state='disabled')

    def send():
        if not worker.accepting.is_set() or not hasattr(worker, 'engine'):
            return
        if not worker.thread.is_alive() or not worker.loop.is_running():
            status.set('聊天已结束，请关闭后重新打开。未发送这条草稿。')
            return
        text = draft.get('1.0', 'end').strip()
        if not text:
            return
        draft.delete('1.0', 'end')
        append('你', text)
        status.set('正在接收和回复…')
        asyncio.run_coroutine_threadsafe(worker.send(text), worker.loop)

    def cancel():
        if worker.ready.is_set() and worker.loop.is_running():
            asyncio.run_coroutine_threadsafe(worker.cancel(), worker.loop)

    closing = False
    def close():
        nonlocal closing
        if closing:
            return
        closing = True
        status.set('正在结束聊天…')
        worker.stopping = True
        if not worker.thread.is_alive():
            window.destroy()
            return
        if worker.loop.is_running():
            worker.loop.call_soon_threadsafe(setattr, worker, 'stopping', True)

    def tick():
        while True:
            try:
                kind, text = events.get_nowait()
            except queue.Empty:
                break
            if kind == 'reply':
                append('对方', text)
                status.set('')
            elif kind == 'history':
                if text['user']:
                    append('你', text['user'])
                if text['reply']:
                    append('对方', text['reply'])
                elif text['error']:
                    append('记录', '上次请求未完成，没有自动重发。')
            elif kind == 'error':
                status.set('本次未完成：' + text)
            elif kind == 'status':
                status.set(text)
            elif kind == 'closed':
                if closing:
                    window.destroy()
                    return
                if not status.get().startswith('本次未完成：'):
                    status.set('聊天已结束')
        if worker.accepting.is_set() and status.get() == '正在准备聊天…':
            status.set('可以开始聊天')
        window.after(100, tick)

    ttk.Button(footer, text='发送', command=send).pack(side='right')
    ttk.Button(footer, text='取消当前回复', command=cancel).pack(side='right', padx=8)
    window.protocol('WM_DELETE_WINDOW', close)
    window.after(100, tick)
    window.mainloop()
    worker.thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    p.add_argument('--private', action='store_true')
    p.add_argument('--structural-candidate', action='store_true', help='Prepare one isolated continuous-chat candidate instead of A/B')
    p.add_argument('--approve-current-provider', action='store_true', help='Explicit approval for this trial only; does not alter the private registry')
    p = sub.add_parser('open')
    p.add_argument('trial', type=Path)
    p.add_argument('label', choices=('A', 'B', 'C'))
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps({'trial': str(prepare(args.source_home, private=args.private,
            approve_current_provider=args.approve_current_provider,
            structural_candidate=args.structural_candidate)), 'status': 'prepared'}, ensure_ascii=False))
    else:
        open_window(args.trial, args.label)


if __name__ == '__main__':
    main()
