"""Execute only newly authorized semantic-selection body requests on one local ledger."""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import encoded, fingerprint
from persona_runtime.instance import InstanceLock
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import SCHEMA, TrialLedger
from .semantic_body_comparison import (POSITIONS, VERSION, ReviewedSources,
                                       file_hash, frozen_json, require)


def funded_positions(plan, approval, manifest_hash):
    rows = plan['positions']
    require(plan['version'] == VERSION and len(rows) == 16 and {r['position'] for r in rows} == POSITIONS,
            'scope_positions')
    positions = [r['position'] for r in rows if r['reuse'] is None]
    require(approval.get('version') == 'semantic-body-use-v1' and approval.get('manifest_sha256') == manifest_hash
            and approval.get('scope') == 'changed_pro_bodies_only' and approval.get('model') == 'deepseek-v4-pro'
            and approval.get('approved_positions') == positions and type(approval.get('max_dispatches')) is int
            and approval['max_dispatches'] == plan['max_new_pro_requests'] == len(positions)
            and approval.get('body_generation_approved') is True and bool(approval.get('author_message'))
            and approval.get('retries') == 0, 'new_body_budget_required')
    return positions


def bind_ledger(root, contract, packets, ledger):
    """Use the existing receipt format. Only funded positions have a dispatchable row."""
    root = root.resolve()
    anchor = root.parent / 'semantic-body-run-binding.private.json'
    contract = dict(contract, root=str(root), ledger=str(ledger.path), positions=[p['position'] for p in packets])
    require(len(set(contract['positions'])) == len(packets)
            and all(p['reuse'] is None and p['request_hash'] == fingerprint(p['request']) for p in packets),
            'invalid_dispatch_set')
    ledger.root.mkdir(parents=True, exist_ok=True)
    staged = ledger.path.with_suffix('.new')
    if anchor.exists():
        record = read_private(anchor)
        require(record['contract'] == contract, 'original_body_ledger_required')
    else:
        require(not ledger.path.exists(), 'unexpected_body_ledger')
        # A crash before the binding is published can leave only an unspent staging DB.
        if staged.exists():
            with closing(sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True)) as db:
                require(db.execute('SELECT count(*) FROM trial_calls WHERE attempt_id IS NOT NULL').fetchone()[0] == 0,
                        'unbound_attempt')
                record = json.loads(db.execute('SELECT record FROM body_identity').fetchone()[0])
                require(record['contract'] == contract, 'unexpected_pending_ledger')
        else:
            record = {'id': str(uuid4()), 'contract': contract}
            with closing(sqlite3.connect(staged)) as db:
                db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
                db.execute('CREATE TABLE body_identity(record TEXT NOT NULL)')
                db.execute('INSERT INTO body_identity VALUES(?)', (encoded(record),))
                db.execute('INSERT INTO trial_meta VALUES(1,?,?,0)', (fingerprint(record), digest(SCHEMA.encode())))
                for p in packets:
                    # Existing ledger kind D denotes a Pro reply with expression material.
                    db.execute("""INSERT INTO trial_calls
                        (slot,position,kind,state,model_json,request_json,request_hash,dependency_hash)
                        VALUES(?,?,'D','prepared',?,?,?,?)""",
                        (f"{p['position']:02}.semantic", p['position'], encoded(p['model']), encoded(p['request']),
                         p['request_hash'], p['references']['U']['raw_hash']))
                db.execute("""CREATE TRIGGER body_fixed_slots BEFORE INSERT ON trial_calls
                    BEGIN SELECT RAISE(ABORT,'fixed body study slots'); END""")
                db.execute('PRAGMA user_version=1')
                db.commit()
        save_once(anchor, record)
    if not ledger.path.exists():
        require(staged.is_file(), 'original_body_ledger_required')
        with closing(sqlite3.connect(staged.as_uri() + '?mode=ro', uri=True)) as db:
            require(json.loads(db.execute('SELECT record FROM body_identity').fetchone()[0]) == record
                    and db.execute('SELECT count(*) FROM trial_calls WHERE attempt_id IS NOT NULL').fetchone()[0] == 0,
                    'wrong_pending_ledger')
        os.rename(staged, ledger.path)
    with ledger.connection() as db:
        require(db.execute('SELECT plan_hash FROM trial_meta').fetchone()[0] == fingerprint(record)
                and json.loads(db.execute('SELECT record FROM body_identity').fetchone()[0]) == record,
                'wrong_body_ledger')
        actual = [(r['position'], r['request_hash'], r['dependency_hash']) for r in
                  db.execute('SELECT position,request_hash,dependency_hash FROM trial_calls ORDER BY position')]
        require(actual == sorted((p['position'], p['request_hash'], p['references']['U']['raw_hash']) for p in packets),
                'body_ledger_slots_changed')


async def execute(decision_path, approval_path, *, client=None, load_credentials=None):
    sources = ReviewedSources(decision_path)
    root = sources.output
    manifest_path = root / 'manifest.private.json'
    manifest_hash, approval_hash = file_hash(manifest_path), file_hash(approval_path)
    plan = frozen_json(manifest_path, manifest_hash)
    approval = frozen_json(approval_path, approval_hash)
    funded = funded_positions(plan, approval, manifest_hash)
    require(Path(plan['decision_path']).resolve() == sources.decision_path
            and plan['selector_approval_sha256'] == sources.decision_hash
            and plan['model'] == sources.model and file_hash(root / 'blind-key.private.json') == plan['blind_key_sha256'],
            'bound_plan_changed')
    rows = {r['position']: r for r in plan['positions']}

    def verify_packet(n):
        row = rows[n]
        packet = frozen_json(root / row['file'], row['sha256'])
        require(packet == sources.position_packet(n)
                and all(row[k] == packet[k] for k in ('position', 'reuse', 'selected_ids', 'request_hash', 'references')),
                'frozen_request_changed')
        return packet

    def guard(ledger_row):
        require(file_hash(manifest_path) == manifest_hash and file_hash(approval_path) == approval_hash,
                'active_body_approval_changed')
        n = ledger_row['position']
        require(n in funded and ledger_row['request_hash'] == verify_packet(n)['request_hash']
                and ledger_row['model_json'] == encoded(sources.model), 'dispatch_outside_scope')

    with closing(InstanceLock(root / 'body-run.lock')):
        packets = [verify_packet(n) for n in sorted(POSITIONS)]
        ledger = TrialLedger(root / 'receipts', client, guard)
        bind_ledger(root, {'manifest_sha256': manifest_hash, 'use_approval_sha256': approval_hash},
                    [p for p in packets if p['position'] in funded], ledger)
        status = ledger.recover()
        if status['cancelled'] or any(row['state'] in ('unknown', 'failed', 'cancelled') for row in status['calls']):
            return status
        pending = [r for r in status['calls'] if r['state'] == 'prepared']
        if not pending:
            return status
        if load_credentials is None:
            from persona_runtime.config import ConfigStore
            from persona_runtime.paths import RuntimePaths
            credentials = ConfigStore(RuntimePaths(Path(sources.plan['source_home']))).load()['model']
        else:
            credentials = load_credentials()
        for row in pending:
            result = await ledger.perform(row['slot'], credentials)
            print(json.dumps({k: result[k] for k in ('slot', 'state', 'http_status')}), flush=True)
            if result['state'] != 'completed':
                break
        return ledger.status()


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--selection-approval', type=Path, required=True)
    cli.add_argument('--body-approval', type=Path, required=True)
    args = cli.parse_args()
    state = asyncio.run(execute(args.selection_approval, args.body_approval))
    print(json.dumps({'attempts': state['attempts'], 'states': [r['state'] for r in state['calls']]}))
