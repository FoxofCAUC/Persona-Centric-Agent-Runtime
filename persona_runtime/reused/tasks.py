"""Bounded background work, explicit resource conflicts, and small UI events."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import queue
import secrets
import threading


@dataclass
class Operation:
    name: str
    resources: frozenset
    cancel: threading.Event = field(default_factory=threading.Event)
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    phase: str = "准备中"
    state: str = "running"


class TaskHub:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="manager")
        self.events = queue.Queue()
        self.active = {}
        self.local = threading.local()
        self.closed = False

    def start(self, name, resources, action, success=None, failure=None, quiet=False, urgent=False):
        resources = frozenset(resources)
        if self.closed: return None
        if any(resources & op.resources for op in self.active.values()): return None
        op = Operation(name, resources); self.active[op.id] = op
        def work():
            self.local.operation = op
            try: self.events.put((op, "success", action(op.cancel), success, quiet))
            except Exception as error: self.events.put((op, "error", error, failure, quiet))
            finally: self.local.operation = None
        if urgent:
            threading.Thread(target=work, daemon=False, name="manager-emergency").start()
        else:
            self.pool.submit(work)
        return op

    def progress(self, text):
        op = getattr(self.local, "operation", None)
        if op: self.events.put((op, "progress", str(text), None, True))

    def drain(self):
        result = []
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            op, kind, value, _, _ = event
            if kind == "progress": op.phase = value
            else:
                op.state = "completed" if kind == "success" else "failed"
                self.active.pop(op.id, None)
            result.append(event)
        return result

    def close(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
