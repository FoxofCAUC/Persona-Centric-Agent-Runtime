"""Versioned, narrow compatibility for organization conflicts; no model retries."""
import hashlib
import json

from .models import LOCAL_USER_ID, RuntimeErrorBase

VERSION = 'organization-conflicts-v2'
INSTRUCTION = ('冲突字段合同v2：conflicts只能是已有记忆ID字符串数组，'
               '非空示例为["已有记忆ID"]；无冲突为[]。不得放入id/quote对象。'
               '仅明确直接矛盾或用户明确更正才标冲突，普通补充不是冲突。')


class OutputError(RuntimeErrorBase):
    def __init__(self, path, expected, actual):
        super().__init__('organization_invalid_output')
        self.diagnostic = {'path': path, 'expected': expected, 'actual': actual}


def receipt(text):
    return {'version': VERSION, 'response_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'normalizations': []}


def parse(conn, payload, text, audit=None):
    """Return a new decoded object; never change the raw provider response."""
    audit = audit if audit is not None else receipt(text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise OutputError('$', 'JSON object', 'invalid_json') from None
    if not isinstance(data, dict):
        raise OutputError('$', 'object', type(data).__name__)
    candidates = data.get('candidates')
    if not isinstance(candidates, list):
        raise OutputError('candidates', 'array', type(candidates).__name__)
    frozen = {m['id']: m for m in payload.get('existing_memories', [])}
    for i, item in enumerate(candidates):
        path = f'candidates[{i}].conflicts'
        if not isinstance(item, dict) or not isinstance(item.get('conflicts'), list):
            raise OutputError(path, 'array', 'invalid_shape')
        converted = []
        for j, conflict in enumerate(item['conflicts']):
            field = f'{path}[{j}]'
            obj = isinstance(conflict, dict)
            if obj and (set(conflict) != {'id', 'quote'} or not isinstance(conflict['quote'], str)
                        or not conflict['quote'].strip()):
                raise OutputError(field, 'string or verified {id,quote}', 'invalid_object')
            ident = conflict['id'] if obj else conflict
            if not isinstance(ident, str) or ident not in frozen:
                raise OutputError(field, 'supplied memory ID', 'unknown_id_or_type')
            old = frozen[ident]
            live = conn.execute('SELECT * FROM memories WHERE id=? AND persona_id=? AND user_id=?',
                                (ident, payload['persona_id'], payload.get('user_id', LOCAL_USER_ID))).fetchone()
            if (not live or live['status'] != 'active' or live['revision'] != old.get('revision')
                    or live['text'] != old['text']):
                raise OutputError(field, 'current owned memory revision', 'stale_or_out_of_scope')
            ids = {live['source_event_id']}
            ids.update(r[0] for r in conn.execute('SELECT event_id FROM memory_sources WHERE memory_id=?', (ident,)))
            for event_id in ids:
                blocked = conn.execute('SELECT reason FROM source_exclusions WHERE event_id=?', (event_id,)).fetchone()
                if blocked and not (live['previous_id'] and live['revision'] > 1 and blocked[0] == 'memory_revised'):
                    raise OutputError(field, 'available memory source', 'revoked_source')
            if obj:
                if conflict['quote'] not in old['text']:
                    raise OutputError(field + '.quote', 'exact frozen memory substring', 'quote_mismatch')
                audit['normalizations'].append({'path': field, 'reason': 'verified_id_quote_to_id'})
            converted.append(ident)
        item['conflicts'] = converted
    return data


def coverage_gaps(conn, session_id, covered):
    from .organization import eligible_turns
    return sorted({t['ordinal'] for t in eligible_turns(conn, session_id, max(covered, default=0))} - set(covered))


def migrate(conn):
    conn.execute('''CREATE TABLE organization_receipts (
        attempt_id TEXT PRIMARY KEY REFERENCES organization_attempts(id) ON DELETE CASCADE,
        response_text TEXT NOT NULL, diagnostic_json TEXT NOT NULL)''')


def pending_conflicts(conn, turn, memory_ids):
    """Read only validated, still-live user evidence in the current scope."""
    from .archive import read_exact, turn_scope
    selected = turn_scope(conn, turn)
    result = []
    for row in conn.execute("SELECT * FROM candidates WHERE persona_id=? AND user_id=? AND status='pending' ORDER BY rowid DESC",
                            (turn['persona_id'], LOCAL_USER_ID)):
        conflicts = json.loads(row['conflicts_json'])
        if not set(conflicts).intersection(memory_ids):
            continue
        task = conn.execute('SELECT payload_json FROM organization_tasks WHERE id=?', (row['task_id'],)).fetchone()
        if not task: continue
        payload = json.loads(task[0])
        try:
            parse(conn, payload, json.dumps({'candidates': [{'conflicts': conflicts}]}))
        except RuntimeErrorBase:
            continue
        sources = json.loads(row['sources_json'])
        evidence = []
        for source in sources:
            found = read_exact(conn, selected, {'kind': 'event', 'id': source['event_id']})
            event = next((e for e in found.get('members', []) if e['id'] == source['event_id']), None)
            if found['status'] != 'found' or not event or event.get('role') != 'user' or source['quote'] not in event['text']:
                break
            evidence.append({'event_id': event['id'], 'quote': source['quote']})
        else:
            if evidence:
                result.append({'id': row['id'], 'text': row['text'], 'conflicts': conflicts, 'sources': evidence})
    return result
