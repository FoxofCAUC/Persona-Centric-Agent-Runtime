"""Durable isolated scene choice. The caller supplies a scoped transport and a live source check."""
import asyncio
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time

from persona_runtime.archive import fingerprint
from persona_runtime.instance import InstanceLock
from persona_runtime.knowledge_web_decision import usage
from .en5_scene_voice import SCENES, VERSION, WAIT_SECONDS, decision, require


class SceneSet:
    def __init__(self, directory, revision, persona):
        self.directory = Path(directory)
        self.revision = revision
        self.persona = persona

    def catalog(self):
        manifest = json.loads((self.directory/'catalog.json').read_bytes())
        require(manifest['version'] == VERSION and fingerprint(manifest) == self.revision
                and manifest['persona'] == self.persona and set(manifest['cards']) == set(SCENES)
                and set(manifest['criteria']) == set(SCENES), 'catalog_changed')
        return manifest

    def card(self, key):
        catalog = self.catalog()
        require(key in SCENES and not (self.directory/(key+'.withdrawn')).exists(), 'card_unavailable')
        try:
            body = json.loads((self.directory/(key+'.json')).read_bytes())['body']
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError('scene_card_unavailable') from exc
        require(isinstance(body,str) and body.strip() and fingerprint(body) == catalog['cards'][key],
                'card_changed')
        return body


class SceneRouter:
    def __init__(self, folder, cards, *, timeout=WAIT_SECONDS):
        folder.mkdir(parents=True,exist_ok=True)
        self.owner = InstanceLock(folder/'scene.lock')
        self.path = folder/'decisions.sqlite3'
        self.cards = cards
        self.timeout = timeout
        self.jobs = set()
        try:
            with self.db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS routes(
                    turn TEXT PRIMARY KEY, scope TEXT NOT NULL, revision TEXT NOT NULL,
                    wire TEXT, request_hash TEXT, started REAL, raw TEXT, http INTEGER, usage TEXT, elapsed_ms REAL,
                    scene TEXT, reason TEXT)""")
                db.execute("""UPDATE routes SET scene='fallback', reason=CASE WHEN started IS NULL
                           THEN 'restart_before_dispatch' ELSE 'restart_unknown' END WHERE reason IS NULL""")
        except BaseException:
            self.owner.close()
            raise

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=3)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def locked(self, turn, scope):
        with self.db() as db:
            row = db.execute('SELECT * FROM routes WHERE turn=?',(turn,)).fetchone()
        require(row and row['scope']==scope and row['revision']==self.cards.revision, 'scope_changed')
        require(row['scene'] in SCENES, 'turn_inactive')
        return row['scene'], self.cards.card(row['scene'])

    def seal(self, turn, scene, reason):
        with self.db() as db:
            db.execute('UPDATE routes SET scene=?,reason=? WHERE turn=? AND reason IS NULL',(scene,reason,turn))

    async def resolve(self, turn, scope, wire, transport, check, *, wire_timing=False):
        check()
        self.cards.catalog()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT scope,reason,request_hash FROM routes WHERE turn=?',(turn,)).fetchone()
            if existing:
                require(existing['scope']==scope and existing['reason'] is not None
                        and existing['request_hash']==fingerprint(wire),'turn_inactive')
            else:
                db.execute('INSERT INTO routes(turn,scope,revision,wire,request_hash) VALUES(?,?,?,?,?)',
                           (turn,scope,self.cards.revision,json.dumps(wire,ensure_ascii=False),fingerprint(wire)))
        if existing:
            return self.locked(turn,scope)
        if wire is None:
            self.seal(turn,'fallback','budget')
            return self.locked(turn,scope)

        dispatched = asyncio.Event()
        loop = asyncio.get_running_loop()
        received = loop.create_future()
        sent_at = None

        def start_clock(at):
            nonlocal sent_at
            require(sent_at is None,'duplicate_dispatch')
            sent_at = at
            dispatched.set()

        def stop_clock(at):
            require(sent_at is not None and at>=sent_at and not received.done(),'receipt_clock')
            received.set_result(at)

        async def first_response():
            check()
            with self.db() as db:
                db.execute('UPDATE routes SET started=? WHERE turn=?',(time.time(),turn))
            try:
                if wire_timing:
                    status, raw = await transport(wire,on_dispatch=start_clock,on_receipt=stop_clock)
                else:
                    start_clock(loop.time())
                    status, raw = await transport(wire)
                    stop_clock(loop.time())
            except Exception:
                self.seal(turn,'fallback','transport_unconfirmed')
                return
            require(received.done(),'receipt_clock_missing')
            elapsed = received.result()-sent_at
            try:
                value = json.loads(raw)
                measured = usage(value) if isinstance(value,dict) else None
            except (ValueError,TypeError):
                measured = None
            scope_error = None
            if elapsed >= self.timeout:
                chosen, reason = 'fallback', 'timeout'
            else:
                try:
                    check()
                    chosen, reason = decision(raw) if status==200 else ('fallback','http_error')
                except Exception as error:
                    chosen, reason, scope_error = None, 'source_changed', error
            with self.db() as db:
                db.execute("""UPDATE routes SET http=?,raw=?,usage=?,elapsed_ms=?,
                    scene=CASE WHEN reason IS NULL THEN ? ELSE scene END, reason=coalesce(reason,?)
                    WHERE turn=? AND raw IS NULL""",(status,raw,json.dumps(measured),elapsed*1000,chosen,reason,turn))
            if scope_error is not None:
                raise scope_error

        # The live transport starts the deadline at request-body dispatch.
        job = asyncio.create_task(first_response())
        self.jobs.add(job)
        def settled(done):
            self.jobs.discard(done)
            dispatched.set()
            if not done.cancelled():
                done.exception()
        job.add_done_callback(settled)
        try:
            await dispatched.wait()
            if sent_at is not None and not received.done():
                await asyncio.wait({job,received},timeout=max(0,sent_at+self.timeout-loop.time()))
            if not job.done() and not received.done():
                self.seal(turn,'fallback','timeout')
            else:
                # The response already arrived. Its later local handling cannot time it out.
                await asyncio.shield(job)
            check()
            return self.locked(turn,scope)
        except BaseException:
            if not dispatched.is_set():
                job.cancel()
            self.seal(turn,None,'cancelled_or_source_changed')
            raise

    async def close(self):
        try:
            if self.jobs:
                await asyncio.gather(*self.jobs,return_exceptions=True)
        finally:
            self.owner.close()
