"""Keep one durable first HTTP response for each distinct frozen research request."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.provider_compat import build_request
from .body_repair_run import RepairTransport
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import parse_receipt


SCHEMA = """
CREATE TABLE study(identity TEXT NOT NULL, manifest_hash TEXT NOT NULL);
CREATE TABLE requests(hash TEXT PRIMARY KEY, wire TEXT NOT NULL, attempt_id TEXT UNIQUE NOT NULL,
    state TEXT NOT NULL, started REAL NOT NULL, ended REAL, raw BLOB, raw_hash TEXT,
    http INTEGER, diagnostics TEXT, result TEXT, error TEXT);
CREATE TABLE uses(slot TEXT PRIMARY KEY, hash TEXT NOT NULL REFERENCES requests(hash), deps TEXT NOT NULL);
CREATE TRIGGER keep_requests BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT,'first receipt immutable'); END;
CREATE TRIGGER keep_uses BEFORE DELETE ON uses BEGIN SELECT RAISE(ABORT,'slot immutable'); END;
CREATE TRIGGER freeze_uses BEFORE UPDATE ON uses BEGIN SELECT RAISE(ABORT,'slot immutable'); END;
CREATE TRIGGER first_receipt BEFORE UPDATE ON requests
WHEN OLD.hash IS NOT NEW.hash OR OLD.wire IS NOT NEW.wire OR OLD.attempt_id IS NOT NEW.attempt_id
 OR OLD.started IS NOT NEW.started OR (OLD.raw IS NOT NULL AND (OLD.raw IS NOT NEW.raw OR OLD.http IS NOT NEW.http OR OLD.raw_hash IS NOT NEW.raw_hash))
 OR (OLD.state != 'sending' AND OLD.state IS NOT NEW.state)
 OR (OLD.result IS NOT NULL AND OLD.result IS NOT NEW.result)
BEGIN SELECT RAISE(ABORT,'first receipt immutable'); END;
"""


class Journal:
    """The caller holds the study lock for the lifetime of a writer."""
    def __init__(self, root, manifest_hash, *, client=None, guard=None, concurrency=5):
        root = outside_repository(root)
        self.path = root / 'calls.sqlite3'
        self.client = client or RepairTransport()
        self.guard = guard
        self.limit = asyncio.Semaphore(concurrency)
        self.locks = {}
        self.stop = False
        anchor = root.parent / (root.name + '-original-ledger.private.json')
        if anchor.exists():
            record = read_private(anchor)
            if (not self.path.is_file() or record['root'] != str(root) or record['manifest_hash'] != manifest_hash):
                raise CorpusError('fresh_original_ledger_required')
        else:
            if not self.path.exists():
                with closing(sqlite3.connect(self.path)) as db:
                    db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
                    db.execute('INSERT INTO study VALUES(?,?)', (str(uuid4()), manifest_hash))
                    db.commit()
            with closing(self.open()) as db:
                ident, old_hash = db.execute('SELECT * FROM study').fetchone()
                if old_hash != manifest_hash or db.execute('SELECT count(*) FROM requests').fetchone()[0]:
                    raise CorpusError('fresh_unbound_ledger')
            record = {'root': str(root), 'manifest_hash': manifest_hash, 'identity': ident}
            save_once(anchor, record)
        with closing(self.open()) as db:
            if tuple(db.execute('SELECT * FROM study').fetchone()) != (record['identity'], manifest_hash):
                raise CorpusError('fresh_original_ledger_required')
            for row in db.execute("SELECT * FROM requests WHERE state='sending'").fetchall():
                if row['raw'] is None:
                    self.fail(db, row['hash'], 'unknown', 'interrupted_without_complete_receipt')
                else:
                    self.finish(db, row['hash'])
            # Preserve a provider-wide halt across restarts. Saved results remain readable.
            self.stop = bool(db.execute("SELECT 1 FROM requests WHERE state='unknown' OR http IN(401,403,429) LIMIT 1").fetchone())
            tail = db.execute("SELECT state FROM requests ORDER BY ended DESC LIMIT 3").fetchall()
            self.consecutive_failures = 0
            for row in tail:
                if row['state'] != 'failed':
                    break
                self.consecutive_failures += 1
            self.stop |= self.consecutive_failures >= 3
            db.commit()

    def open(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def fail(db, key, state, reason):
        db.execute("UPDATE requests SET state=?,ended=?,error=? WHERE hash=? AND state='sending'",
                   (state, time.time(), reason, key))

    @staticmethod
    def finish(db, key):
        row = db.execute('SELECT * FROM requests WHERE hash=?', (key,)).fetchone()
        wire = json.loads(row['wire'])
        if digest(row['raw']) != row['raw_hash']:
            raise CorpusError('fresh_raw_hash_changed')
        parsed = parse_receipt(row['raw'], wire.get('stream', False))
        success = row['http'] == 200 and parsed['valid'] and parsed['model'] == wire['model']
        state = 'completed' if success else 'unknown' if row['http'] == 200 and parsed['incomplete_stream'] else 'failed'
        db.execute("UPDATE requests SET state=?,ended=?,result=?,error=? WHERE hash=? AND state='sending'",
                   (state, time.time(), encoded(parsed), None if success else 'provider_result_invalid', key))

    def stored(self, key):
        with closing(self.open()) as db:
            row = db.execute('SELECT * FROM requests WHERE hash=?', (key,)).fetchone()
        if row is None:
            return None
        result = json.loads(row['result'] or '{}')
        return {'state': row['state'], 'body': result.get('body') if row['state'] == 'completed' else None,
                'raw_hash': row['raw_hash'], 'request_hash': key, 'usage': result.get('usage'),
                'error': row['error'], 'http_status': row['http']}

    async def call(self, slot, model, request, deps, credentials):
        key = fingerprint(request)
        async with self.locks.setdefault(key, asyncio.Lock()), self.limit:
            deps_text = encoded(deps)
            with closing(self.open()) as db:
                old = db.execute('SELECT hash,deps FROM uses WHERE slot=?', (slot,)).fetchone()
                if old is not None and tuple(old) != (key, deps_text):
                    raise CorpusError('fresh_slot_or_history_changed')
                if self.guard:
                    self.guard(slot)
                existing = self.stored(key)
                if existing is not None:
                    db.execute('INSERT OR IGNORE INTO uses VALUES(?,?,?)', (slot,key,deps_text))
                    db.commit()
                    return existing
                if self.stop:
                    return {'state':'blocked','body':None,'error':'study_waiting_for_reconciliation'}
                if (credentials['base_url'].rstrip('/') != model['base_url'].rstrip('/')
                        or request != build_request(model,request['messages'])[0]
                        or 'tools' in request or 'encrypted_key' in model):
                    raise CorpusError('fresh_wire_contract_changed')
                live = model | {'encrypted_key': credentials['encrypted_key']}
                self.client.preflight(live, request)
                db.execute('BEGIN IMMEDIATE')
                db.execute("INSERT INTO requests(hash,wire,attempt_id,state,started) VALUES(?,?,?,'sending',?)",
                           (key,encoded(request),str(uuid4()),time.time()))
                db.execute('INSERT INTO uses VALUES(?,?,?)',(slot,key,deps_text))
                db.commit()

            def save_raw(raw, *, http_status, diagnostics):
                with closing(self.open()) as db:
                    db.execute("UPDATE requests SET raw=?,raw_hash=?,http=?,diagnostics=? WHERE hash=?",
                               (raw,digest(raw),http_status,encoded(diagnostics),key))
                    db.commit()
            try:
                await self.client.complete(live,request,save_raw=save_raw)
            except BaseException as exc:
                with closing(self.open()) as db:
                    row = db.execute('SELECT raw FROM requests WHERE hash=?',(key,)).fetchone()
                    if row['raw'] is None:
                        diag = getattr(exc,'diagnostics',{})
                        unsent = diag.get('request_headers_sent') is False and diag.get('response_headers_received') is False
                        self.fail(db,key,'failed' if unsent else 'unknown','transport_interrupted')
                        db.execute('UPDATE requests SET diagnostics=? WHERE hash=?',(encoded(diag),key))
                    else:
                        self.finish(db,key)
                    db.commit()
                if not isinstance(exc,Exception):
                    raise
            else:
                with closing(self.open()) as db:
                    self.finish(db,key)
                    db.commit()
            returned = self.stored(key)
            self.consecutive_failures = self.consecutive_failures + 1 if returned['state'] == 'failed' else 0
            if returned['state'] == 'unknown' or returned['http_status'] in (401,403,429) or self.consecutive_failures >= 3:
                self.stop = True
            if self.guard:
                self.guard(slot)
            return returned
