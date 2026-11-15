"""Native protocol receipts share the existing attempt transaction, away from text."""
from copy import deepcopy
import hashlib
import json

from .archive import fingerprint
from .deepseek_native_contract import VERSION, ReplayScope
from .models import RuntimeErrorBase


def scope(task, model=None):
    model = model if model is not None else json.loads(task['model_snapshot'])
    binding = fingerprint({'base_url': model['base_url'], 'model': model['name'],
        'credential': hashlib.sha256(model.get('encrypted_key', '').encode()).hexdigest()})
    return ReplayScope(task['persona_id'], task['channel'] or 'desktop',
        task['session_id'] or task['id'], binding, model['name']).checked()


def save_receipt(db, task, attempt_id, native):
    if native is None: return None
    if (not isinstance(native, dict) or native.get('version') != VERSION
            or native.get('outcome') not in ('completed', 'failed', 'unknown')):
        raise RuntimeErrorBase('native_receipt_invalid')
    owner = db.execute('SELECT task_id FROM organization_attempts WHERE id=?', (attempt_id,)).fetchone()
    if not owner or owner[0] != task['id']:
        raise RuntimeErrorBase('native_receipt_attempt_mismatch')
    value = {'revision': 0, 'data': {'scope': scope(task), 'task_id': task['id'],
        'attempt_id': attempt_id, 'packet': deepcopy(native)}}
    key = 'knowledge.native.receipt.' + attempt_id
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    existing = db.execute('SELECT value FROM knowledge_control WHERE key=?', (key,)).fetchone()
    if existing and json.loads(existing[0]) != value:
        raise RuntimeErrorBase('native_receipt_conflict')
    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO NOTHING', (key, encoded))
    return {'native_receipt_key': key, 'native_receipt_sha256': fingerprint(value)}


def load_receipt(db, task, attempt_id, *, model=None, expected_hash=None):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?',
        ('knowledge.native.receipt.' + attempt_id,)).fetchone()
    if not row: raise RuntimeErrorBase('native_receipt_missing')
    envelope = json.loads(row[0])
    if expected_hash is not None and fingerprint(envelope) != expected_hash:
        raise RuntimeErrorBase('native_receipt_corrupt')
    value = envelope['data']
    if (value.get('scope') != scope(task, model) or value.get('task_id') != task['id']
            or value.get('attempt_id') != attempt_id or value.get('packet', {}).get('version') != VERSION):
        raise RuntimeErrorBase('native_receipt_scope_mismatch')
    return deepcopy(value['packet'])
