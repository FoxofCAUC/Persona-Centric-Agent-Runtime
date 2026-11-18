"""Run a frozen research manifest and keep the first receipt for each position."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_learning_transport import CorpusLearningHTTPClient
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.instance import InstanceLock
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import SCHEMA, TrialLedger, parse_receipt


VERSION = 'reviewed-body-repair-v1'
APPROVAL_VERSION = 'reviewed-body-repair-use-v1'


def require(condition, code):
    if not condition:
        raise CorpusError('body_repair_' + code)


class RepairTransport(CorpusLearningHTTPClient):
    @staticmethod
    def preflight(model, request):
        route = CorpusLearningHTTPClient.preflight(model, request)
        # The working credential may only travel in the HTTP authorization header.
        require(route[3] not in encoded(request), 'credential_in_payload')
        return route


def read_contract(manifest_path, approval_path):
    manifest_path, approval_path = map(outside_repository, (manifest_path, approval_path))
    raw_manifest, raw_approval = manifest_path.read_bytes(), approval_path.read_bytes()
    plan, approval = json.loads(raw_manifest), json.loads(raw_approval)
    identity = {'manifest_path': str(manifest_path), 'manifest_sha256': digest(raw_manifest),
                'approval_path': str(approval_path), 'approval_sha256': digest(raw_approval)}
    require(manifest_path.name == 'manifest.private.json' and plan.get('version') == VERSION
            and approval.get('version') == APPROVAL_VERSION
            and outside_repository(approval['manifest_path']) == manifest_path
            and approval.get('manifest_sha256') == identity['manifest_sha256']
            and 'max_dispatches' in approval and approval['max_dispatches'] is None
            and type(approval.get('retries')) is int and approval['retries'] == 0
            and isinstance(approval.get('author_message'), str) and approval['author_message'].strip(),
            'scope_mismatch')
    positions = plan['positions']
    seen = set()
    require(isinstance(positions, list) and positions, 'positions_required')
    for packet in positions:
        n, model, request = packet['position'], packet['model'], packet['request']
        require(type(n) is int and 0 < n and n not in seen, 'duplicate_or_invalid_position')
        seen.add(n)
        require_generation_provider(model)
        require(model['name'] == 'deepseek-v4-pro' and not model.get('encrypted_key')
                and 'tools' not in request and 'response_format' not in request
                and build_request(model, request['messages'])[0] == request
                and fingerprint(request) == packet['request_hash'], 'invalid_pro_wire')
        if packet['reuse'] is not None:
            reuse = packet['reuse']
            require(isinstance(reuse, dict) and bool(reuse)
                    and reuse.get('request_hash', packet['request_hash']) == packet['request_hash'],
                    'invalid_reuse')
    return plan, identity


def bind_study(root, identity, packets, *, initialize):
    """Publish the original ledger identities together, before the first send.

    Each position owns one first-receipt journal. Session length therefore does
    not inherit the old four-condition study's fixed call budget.
    """
    anchor = root.parent / (root.name + '-first-receipts.private.json')
    if anchor.exists():
        record = read_private(anchor)
        require(record['contract'] == identity, 'wrong_original_run')
        initializing = False
    else:
        require(initialize, 'uninitialized')
        record = None
        initializing = True
    for packet in packets:
        if packet['reuse'] is not None:
            continue
        n = packet['position']
        ledger = slot_ledger(root, n)
        # An interrupted setup may only leave journals that were never dispatched.
        if ledger.path.exists():
            with ledger.connection() as db:
                bound = json.loads(db.execute('SELECT record FROM repair_identity').fetchone()[0])
                require(bound['contract'] == identity, 'wrong_original_run')
                if record is None:
                    record = bound
                require(bound == record, 'wrong_original_journal')
                stored = db.execute('SELECT * FROM trial_calls').fetchall()
                require(len(stored) == 1 and stored[0]['position'] == n
                        and stored[0]['request_json'] == encoded(packet['request'])
                        and stored[0]['request_hash'] == packet['request_hash']
                        and stored[0]['model_json'] == encoded(packet['model']), 'changed_journal')
                require(not initializing or stored[0]['attempt_id'] is None, 'unbound_attempt')
        else:
            require(initializing, 'missing_original_journal')
            if record is None:
                record = {'contract': identity, 'id': str(uuid4())}
            ledger.root.mkdir(parents=True, exist_ok=True)
            staged = ledger.path.with_suffix('.new')
            # This staging path has never been exposed to a transport.
            if staged.exists():
                staged.unlink()
            with closing(sqlite3.connect(staged)) as db:
                db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
                db.execute('CREATE TABLE repair_identity(record TEXT NOT NULL)')
                db.execute('INSERT INTO repair_identity VALUES(?)', (encoded(record),))
                db.execute('INSERT INTO trial_meta VALUES(1,?,?,0)', (fingerprint(record), digest(SCHEMA.encode())))
                db.execute("""INSERT INTO trial_calls(slot,position,kind,state,model_json,request_json,request_hash)
                              VALUES(?,?,'D','prepared',?,?,?)""",
                           (slot_name(n), n, encoded(packet['model']), encoded(packet['request']), packet['request_hash']))
                db.execute("""CREATE TRIGGER repair_fixed_position BEFORE INSERT ON trial_calls
                              BEGIN SELECT RAISE(ABORT,'fixed research position'); END""")
                db.execute('PRAGMA user_version=1')
                db.commit()
            staged.rename(ledger.path)
    if initializing:
        save_once(anchor, record or {'contract': identity, 'id': str(uuid4())})


def slot_name(n):
    return f'{n:02}.repair'


def slot_ledger(root, n, client=None, guard=None):
    return TrialLedger(root / 'receipts' / f'{n:02}', client, guard)


def results(root, packets):
    calls = []
    attempts = 0
    for packet in packets:
        n = packet['position']
        if packet['reuse'] is not None:
            calls.append({'state': 'reused', 'body': None, 'usage': None, 'raw_hash': None,
                          **packet['reuse'], 'position': n, 'reused': True, 'request_hash': packet['request_hash']})
            continue
        row = slot_ledger(root, n).row(slot_name(n))
        item = {k: row[k] for k in ('position', 'slot', 'state', 'http_status', 'request_hash', 'raw_hash', 'error_code')}
        attempts += int(row['attempt_id'] is not None)
        receipt = {}
        if row['raw_response'] is not None:
            require(digest(row['raw_response']) == row['raw_hash'], 'changed_receipt')
            receipt = parse_receipt(row['raw_response'], packet['request'].get('stream', False))
            if row['result_json'] is not None:
                require(receipt == json.loads(row['result_json']), 'changed_result')
            if item['state'] == 'completed' and receipt['model'] != packet['model']['name']:
                item.update(state='failed', error_code='body_repair_unexpected_receipt_model')
        calls.append(item | {'reused': False, 'body': receipt.get('body') if item['state'] == 'completed' else None,
                             'usage': receipt.get('usage')})
    return {'attempts': attempts, 'calls': calls}


def load_results(manifest_path, approval_path):
    plan, identity = read_contract(manifest_path, approval_path)
    root = Path(identity['manifest_path']).parent
    bind_study(root, identity, plan['positions'], initialize=False)
    return results(root, plan['positions'])


async def execute(manifest_path, approval_path, *, per_position_guard=None, load_credentials=None, client=None):
    plan, identity = read_contract(manifest_path, approval_path)
    root = Path(identity['manifest_path']).parent
    packets = plan['positions']
    by_position = {p['position']: p for p in packets}
    if client is None and per_position_guard is None:
        from .body_repair_material import RepairInputs
        sources = RepairInputs(outside_repository(plan['state_path']))
        require(root == sources.root and plan['source_result_hash'] == sources.source_result_hash,
                'repair_material_source_changed')
        per_position_guard = sources.guard
    client = client or RepairTransport()

    def guard(row):
        packet = by_position[row['position']]
        for prefix in ('manifest', 'approval'):
            require(digest(Path(identity[prefix + '_path']).read_bytes()) == identity[prefix + '_sha256'],
                    'active_authority_changed')
        require(packet['reuse'] is None and row['request_hash'] == packet['request_hash'], 'wrong_position')
        if per_position_guard is not None:
            per_position_guard(packet)

    with closing(InstanceLock(root / 'repair-run.lock')):
        bind_study(root, identity, packets, initialize=True)
        credentials = None
        for packet in packets:
            if packet['reuse'] is not None:
                continue
            n = packet['position']
            ledger = slot_ledger(root, n, client, guard)
            row = ledger.recover()['calls'][0]
            if row['state'] != 'prepared':
                continue
            guard(ledger.row(row['slot']))
            if credentials is None:
                if load_credentials is None:
                    from persona_runtime.config import ConfigStore
                    from persona_runtime.paths import RuntimePaths
                    credentials = ConfigStore(RuntimePaths(outside_repository(plan['source_home']))).load()['model']
                else:
                    credentials = load_credentials()
            result = await ledger.perform(row['slot'], credentials)
            print(json.dumps({k: result[k] for k in ('slot', 'state', 'http_status')}), flush=True)
            if result['http_status'] in (401, 403, 429):
                break
        return results(root, packets)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--manifest', type=Path, required=True)
    cli.add_argument('--approval', type=Path, required=True)
    args = cli.parse_args()
    state = asyncio.run(execute(args.manifest, args.approval))
    print(json.dumps({'attempts': state['attempts'], 'states': [r['state'] for r in state['calls']]}))
