"""Disposable window for the pinned Dafeiyu native text reference.

Only a user send starts the isolated reference process. The window never sends
a startup probe, interprets PCAR reply JSON, or resumes unfinished generation.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_trial import configure
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, provider_identity, safe
from persona_runtime.storage import Store

VERSION = 'native-reference-trial-v1'
TITLE = 'PCAR · 大肥鱼文本参考'
PROFILE = {'version': 'native-text-low-v1'}


def backend():
    from experiments import companion_native_reference
    return companion_native_reference


def prepare(source_home=DEFAULT_HOME, *, private=False, approve_current_provider=False,
            reference_root=None, development_material=False):
    """Prepare one copy; freezes do not invoke a provider or import AstrBot."""
    if private == development_material:
        raise RuntimeErrorBase('native_trial_material_scope_invalid')
    source_paths = RuntimePaths(Path(source_home).resolve())
    source = deepcopy(ConfigStore(source_paths).load())
    if development_material:
        for field in ('model', 'character_selector'):
            source[field]['encrypted_key'] = ''
    reference_root = Path(reference_root or source_paths.home / 'reference/dafeiyu').resolve()
    records = PrivatePersonas(source_paths).load(required=True)['records'] if private else []
    ready = [row for row in records if row.get('state') == 'ready']
    if private:
        if len(ready) != 1:
            raise RuntimeErrorBase('private_trial_requires_one_ready_role')
        if (not approve_current_provider and ready[0].get('approved_models', {}).get('main')
                != provider_identity(source['model'])):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    # Check public installation before creating a private copy.
    status = backend().source_status(reference_root)
    if not status.get('source_ready') or not status.get('text_patches_applied'):
        raise RuntimeErrorBase('reference_source_not_ready')
    python = reference_root / '.venv/Scripts/python.exe'
    if not python.is_file():
        raise RuntimeErrorBase('reference_python_unavailable')
    parent = safe(source_paths.home / 'tmp/companion-native-reference', source_paths.home)
    parent.mkdir(parents=True, exist_ok=True)
    root = safe(parent / identifier(), parent)
    root.mkdir()
    trial = {'version': VERSION, 'status': 'preparing', 'created_at': timestamp(),
        'source_home': str(source_paths.home), 'reference_root': str(reference_root),
        'private': private, 'development_material': development_material,
        'reasoning_profile': dict(PROFILE), 'cleanup_homes': [],
        'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        'planned_rounds': [8, 12], 'planned_rounds_are_not_call_quota': True,
        'current_provider_explicitly_approved': approve_current_provider,
        'cleanup_after': 'this_short_chat_and_necessary_review', 'startup_paid_calls': 0}
    atomic_json(root / 'trial.json', trial)
    try:
        if private:
            copy = PrivatePersonas(source_paths).create_sandbox(ready[0]['id'])
            paths, pid = RuntimePaths(Path(copy['home'])), copy['persona_id']
        else:
            paths = RuntimePaths(root / 'artificial-home')
            store = Store(paths); store.initialize()
            from persona_runtime.service import RuntimeService
            service = RuntimeService(store)
            pid = service.create_persona(generic_persona())['id']
            service.use_persona(pid)
        # Ownership is persisted before configuration/freezing can fail.
        trial.update(home=str(paths.home), persona_id=pid)
        trial['cleanup_homes'].append(str(paths.home))
        atomic_json(root / 'trial.json', trial)
        if private and approve_current_provider:
            policy_file = paths.home / 'runtime/private-copy-policy.json'
            policy = json.loads(policy_file.read_text(encoding='utf-8'))
            policy['approved_models']['main'] = provider_identity(source['model'])
            policy['approval_scope'] = 'this_native_text_reference_trial_only'
            atomic_json(policy_file, policy)
        configure(paths, source, pid)
        trial['frozen'] = backend().freeze_trial(paths.home, pid, reference_root)
        atomic_json(data_root(trial) / 'session.json', {'version': 1, 'order': []})
        trial['status'] = 'prepared'
        atomic_json(root / 'trial.json', trial)
        backend().verify_trial(root, trial)
    except BaseException as error:
        trial['status'] = 'preparation_blocked'
        trial['error_code'] = str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__
        atomic_json(root / 'trial.json', trial)
        error.preparation_trial_root = str(root)
        raise
    return root


def data_root(plan):
    home = Path(plan['home']).resolve()
    return safe(home / 'runtime/native-reference', home)


def read_session(root):
    """Restore only completed records. Missing terminals never trigger replay."""
    state = json.loads((root / 'session.json').read_text(encoding='utf-8'))
    if state.get('version') != 1 or type(state.get('order')) is not list:
        raise RuntimeErrorBase('native_trial_session_invalid')
    order = state['order']
    if order != [f'{i:04d}' for i in range(1, len(order) + 1)]:
        raise RuntimeErrorBase('native_trial_sequence_invalid')
    # A turn written before a failed session update is an unresolved checkpoint.
    folders = sorted(p.name for p in (root / 'turns').iterdir()) if (root / 'turns').exists() else []
    if folders != order:
        raise RuntimeErrorBase('native_trial_unregistered_turn')
    history, visible, interrupted = [], [], False
    for turn_id in order:
        folder = safe(root / 'turns' / turn_id, root)
        value = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
        if value.get('seq') != len(visible) + 1 or value.get('history') != history:
            raise RuntimeErrorBase('native_trial_history_changed')
        terminal, result = folder / 'terminal.json', folder / 'result.json'
        if not terminal.is_file() or not result.is_file():
            interrupted = True
            visible.append({'user': value['text'], 'status': 'unfinished', 'display': ''})
            continue
        data = backend().effective_result(folder)
        if (folder / 'cancel.json').exists():
            interrupted = True
            visible.append({'user': value['text'], 'status': 'cancelled_before_display', 'display': ''})
            continue
        if data.get('status') == 'completed':
            suppressed = data.get('suppressed') is True
            if (not isinstance(data.get('display'), str)
                    or not suppressed and not data['display'].strip()
                    or not isinstance(data.get('history_reply'), str)):
                raise RuntimeErrorBase('native_trial_completed_reply_missing')
            native_history = data.get('native_history')
            if (not isinstance(native_history, list) or not native_history
                    or any(type(item) is not dict or set(item) != {'role', 'content'}
                           or item['role'] not in ('user', 'assistant')
                           or not isinstance(item['content'], str) for item in native_history)):
                raise RuntimeErrorBase('native_trial_history_receipt_missing')
            history = native_history
        else:
            interrupted = True
        visible.append({'user': value['text'], 'status': data.get('status'), 'display': data.get('display', '')})
    return state, history, visible, interrupted


class NativeTrialWorker:
    def __init__(self, root, events):
        self.root, self.events = Path(root).resolve(), events
        self.loop = asyncio.new_event_loop()
        self.ready, self.accepting = threading.Event(), threading.Event()
        self.stopping = False
        self.active_task = None
        self.active_folder = None
        self.thread = threading.Thread(target=self.run, name='native-reference-window', daemon=False)
        self.thread.start()

    def run(self):
        asyncio.set_event_loop(self.loop)
        locks = []
        try:
            self.trial = backend().load_trial(self.root)
            locks.append(InstanceLock(self.root / 'window.lock'))
            locks.append(InstanceLock(Path(self.trial['home']) / 'runtime/service.lock'))
            backend().verify_trial(self.root, self.trial)
            self.data = data_root(self.trial)
            _, _, visible, interrupted = read_session(self.data)
            for item in visible:
                self.events.put(('history', item))
            if interrupted:
                self.events.put(('error', '上次请求未完成，已停止；没有自动重发。'))
            else:
                self.accepting.set()
            self.ready.set()
            self.loop.run_until_complete(self.monitor())
        except Exception as error:
            self.events.put(('error', str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__))
        finally:
            self.accepting.clear(); self.ready.set()
            for lock in reversed(locks):
                lock.close()
            self.loop.close()
            self.events.put(('closed', ''))

    async def monitor(self):
        while not self.stopping:
            await asyncio.sleep(.1)
        self.accepting.clear()
        await self.cancel()
        if self.active_task is not None:
            await self.active_task

    async def send(self, text):
        if not self.accepting.is_set() or self.stopping or not isinstance(text, str) or not text.strip():
            return False
        # No await before this gate: two UI sends cannot dispatch concurrently.
        self.accepting.clear()
        self.active_task = asyncio.current_task()
        try:
            backend().verify_trial(self.root, self.trial)
            session, history, _, interrupted = read_session(self.data)
            if interrupted:
                raise RuntimeErrorBase('native_trial_unfinished_no_resume')
            seq = len(session['order']) + 1
            turn_id = f'{seq:04d}'
            folder = safe(self.data / 'turns' / turn_id, self.data)
            folder.mkdir(parents=True, exist_ok=False)
            self.active_folder = folder
            atomic_json(folder / 'input.json', {'seq': seq, 'text': text, 'history': history})
            session['order'].append(turn_id)
            atomic_json(self.data / 'session.json', session)
            self.events.put(('user', text))
            self.events.put(('status', '正在回复…'))
            await self.run_child(folder, turn_id)
            # Do not invent a terminal/receipt if the process exits mid-write.
            terminal, result = folder / 'terminal.json', folder / 'result.json'
            if not terminal.is_file() or not result.is_file():
                raise RuntimeErrorBase('native_reference_child_incomplete_no_resume')
            data = backend().effective_result(folder)
            if data.get('status') != 'completed':
                self.events.put(('error', '本次未完成：' + str(data.get('error_code') or data.get('status'))))
                return False
            suppressed = data.get('suppressed') is True
            if not isinstance(data.get('display'), str) or not suppressed and not data['display'].strip():
                raise RuntimeErrorBase('native_reference_display_missing')
            if not self.stopping and not (folder / 'cancel.json').exists():
                self.events.put(('suppressed', '本轮没有发送文本') if suppressed else ('reply', data['display']))
                self.accepting.set()
            return True
        except Exception as error:
            self.events.put(('error', str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__))
            return False
        finally:
            self.active_folder = None
            self.active_task = None

    async def run_child(self, folder, turn_id):
        python = Path(self.trial['reference_root']) / '.venv/Scripts/python.exe'
        script = ROOT / 'experiments/companion_native_reference.py'
        args = [str(python), '-B', str(script), 'turn', str(self.root), turn_id]
        if self.trial.get('development_material'):
            args.append('--fake')
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'TEMP': str(Path(self.trial['source_home']) / 'tmp'),
            'TMP': str(Path(self.trial['source_home']) / 'tmp'),
            'PYTHONWARNINGS': 'ignore::SyntaxWarning'}
        with (folder / 'process-console.txt').open('wb') as console:
            child = await asyncio.create_subprocess_exec(*args, cwd=str(ROOT), env=env,
                stdout=console, stderr=console,
                creationflags=0x08000000 if sys.platform == 'win32' else 0)
            code = await child.wait()
        atomic_json(folder / 'runner-exit.json', {'returncode': code, 'recorded_at': timestamp(),
            'terminal_exists': (folder / 'terminal.json').is_file(), 'automatic_retry': False})

    async def cancel(self):
        if self.active_folder is not None:
            atomic_json(self.active_folder / 'cancel.json', {'reason': 'user_cancelled', 'at': timestamp()})
            self.events.put(('status', '正在取消并结束当前请求…'))


def open_window(root):
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    from persona_runtime.instance import bind_host_job
    # Unexpected host exit kills children; their unfinished ledgers block replay.
    job = bind_host_job()
    events = queue.Queue()
    window = tk.Tk(); window.title(TITLE); window.geometry('820x680')
    worker = NativeTrialWorker(root, events)
    ttk.Label(window, text='像平常一样聊一小段，可以随时结束。', padding=12).pack(anchor='w')
    transcript = scrolledtext.ScrolledText(window, wrap='word', state='disabled', font=('Microsoft YaHei UI', 11))
    transcript.pack(fill='both', expand=True, padx=12)
    draft = tk.Text(window, height=4, wrap='word', font=('Microsoft YaHei UI', 11))
    draft.pack(fill='x', padx=12, pady=8)
    status = tk.StringVar(value='正在准备聊天…')
    footer = ttk.Frame(window, padding=12); footer.pack(fill='x')
    ttk.Label(footer, textvariable=status).pack(side='left')
    closing = False
    submitting = False

    def append(who, text):
        transcript.configure(state='normal'); transcript.insert('end', who + '\n' + text + '\n\n')
        transcript.see('end'); transcript.configure(state='disabled')

    def send():
        nonlocal submitting
        if submitting or not worker.accepting.is_set() or not worker.loop.is_running():
            return
        text = draft.get('1.0', 'end').strip()
        if text:
            submitting = True
            draft.delete('1.0', 'end')
            asyncio.run_coroutine_threadsafe(worker.send(text), worker.loop)

    def cancel():
        if worker.loop.is_running():
            asyncio.run_coroutine_threadsafe(worker.cancel(), worker.loop)

    def close():
        nonlocal closing
        if closing: return
        closing = True; status.set('正在结束聊天…')
        worker.stopping = True
        if not worker.thread.is_alive(): window.destroy()

    def tick():
        nonlocal submitting
        while True:
            try: kind, value = events.get_nowait()
            except queue.Empty: break
            if kind == 'user': append('你', value)
            elif kind == 'reply': append('对方', value); status.set(''); submitting = False
            elif kind == 'suppressed': status.set(value); submitting = False
            elif kind == 'history':
                append('你', value['user'])
                if value.get('display'): append('对方', value['display'])
            elif kind == 'error': status.set('本次未完成：' + value); submitting = False
            elif kind == 'status': status.set(value)
            elif kind == 'closed':
                if closing: window.destroy(); return
                if not status.get().startswith('本次未完成：'): status.set('聊天已结束')
        if worker.accepting.is_set() and status.get() == '正在准备聊天…':
            status.set('可以开始聊天')
        window.after(100, tick)

    ttk.Button(footer, text='发送', command=send).pack(side='right')
    ttk.Button(footer, text='取消当前回复', command=cancel).pack(side='right', padx=8)
    window.protocol('WM_DELETE_WINDOW', close); window.after(100, tick)
    window.mainloop(); worker.thread.join()
    # Keep the OS job handle alive until this independent window process exits.
    _ = job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    p.add_argument('--reference-root', type=Path)
    p.add_argument('--private', action='store_true')
    p.add_argument('--approve-current-provider', action='store_true')
    p.add_argument('--development-material', action='store_true')
    p = sub.add_parser('open'); p.add_argument('trial', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        path = prepare(args.source_home, private=args.private,
            approve_current_provider=args.approve_current_provider,
            reference_root=args.reference_root, development_material=args.development_material)
        print(json.dumps({'trial': str(path), 'status': 'prepared'}, ensure_ascii=False))
    else:
        open_window(args.trial)


if __name__ == '__main__':
    main()
