"""Encrypted local intents bridge the gap between clicking send and receiving an ACK."""
import json
import threading
import time
import msvcrt
from contextlib import contextmanager

from .config import atomic_json
from .models import RuntimeErrorBase
from .security import protect, unprotect


class SubmissionJournal:
    def __init__(self, paths, channel):
        if channel not in ("desktop", "qq"):
            raise ValueError("Unsupported journal channel")
        self.path = paths.home / "runtime" / (channel + "-submissions.dpapi.json")
        self.channel = channel
        self.lock = threading.RLock()

    @contextmanager
    def _locked(self):
        # The QQ SDK process and the admin API can update the same journal.
        # A Python lock alone would silently lose entries across those processes.
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            try:
                with lock_path.open("xb") as initial:
                    initial.write(b"0")
            except FileExistsError:
                pass
            # Windows denies reading a byte locked by another handle. Open it
            # directly and acquire the lock before inspecting protected data.
            with lock_path.open("r+b") as file:
                acquired = False
                deadline = time.monotonic()+3
                try:
                    while not acquired:
                        file.seek(0)
                        try:
                            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                            acquired = True
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise RuntimeErrorBase("Submission journal is busy; input has not been discarded") from None
                            time.sleep(.01)
                    yield
                finally:
                    if acquired:
                        file.seek(0)
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self):
        if not self.path.exists():
            return []
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        if envelope.get("version") != 1:
            raise RuntimeErrorBase("Unsupported submission journal")
        return json.loads(unprotect(envelope["encrypted"]))

    def _write(self, rows):
        atomic_json(self.path, {"version": 1, "encrypted": protect(json.dumps(rows, ensure_ascii=False))})

    def pending(self):
        with self._locked():
            return self._read()

    def remember(self, payload, *, match_draft=False):
        with self._locked():
            rows = self._read()
            for row in rows:
                if row["payload"]["request_id"] == payload["request_id"]:
                    return row["payload"]
                if match_draft and all(row["payload"].get(key) == payload.get(key) for key in ("text", "persona_id", "session_id", "channel")):
                    return row["payload"]
            rows.append({"payload": payload, "state": "unconfirmed"})
            self._write(rows)
            return payload

    def state(self, request_id, state):
        with self._locked():
            rows = self._read()
            for row in rows:
                if row["payload"]["request_id"] == request_id:
                    row["state"] = state
            self._write(rows)

    def forget(self, request_id):
        with self._locked():
            rows = self._read()
            remaining = [row for row in rows if row["payload"]["request_id"] != request_id]
            if len(remaining) != len(rows):
                self._write(remaining)
