"""Accepted reference bindings and per-execution metadata, never model retries."""
import json
from .archive import (binding, encoded, read_exact, resolve, scope,
                      reference_group, reference_selection, serialize_reference_block)
from .models import LOCAL_USER_ID, identifier, timestamp


class ReferenceError(ValueError):
    def __init__(self, code, manifest):
        super().__init__(code)
        self.manifest = manifest


def capture(conn, event_id, selected_scope, target):
    result = resolve(conn, selected_scope, target)
    bound = binding(result)
    conn.execute('INSERT INTO input_references VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (event_id, selected_scope['persona_id'], LOCAL_USER_ID, selected_scope['channel'], selected_scope['bot'], selected_scope['peer'], encoded(target), encoded(bound), 1, timestamp()))


def prepare(conn, turn):
    rows = conn.execute('SELECT r.* FROM input_references r JOIN input_batch_members m ON m.event_id=r.event_id WHERE m.batch_id=? ORDER BY m.member_order', (turn.get('input_batch_id'),)).fetchall()
    manifest = {'references':[], 'turn_ids':[], 'reference_characters':0}
    groups = []; seen = set(); selections = []
    for row in rows:
        row = dict(row); bound = json.loads(row['binding_json'])
        entry = {'input_event_id':row['event_id'],'accepted_status':bound['status'],'target':bound.get('target')}
        manifest['references'].append(entry)
        if bound['status'] != 'found':
            entry['status']=bound['status'];raise ReferenceError('reference_'+bound['status'],manifest)
        selected = scope(row['persona_id'],row['channel'],row['bot'],row['peer'])
        current = read_exact(conn, selected, bound['target'])
        if current['status']!='found':
            entry['status']=current['status'];raise ReferenceError('reference_unavailable',manifest)
        reply_order = {reply['id']: index for index,reply in enumerate(current['replies'])}
        # A newly confirmed output is not silently added to an accepted reference.
        current['replies']=[r for r in current['replies'] if r['id'] in bound['reply_ids']]
        if binding(current)['hash'] != bound['hash']:
            entry['status']='changed';raise ReferenceError('reference_source_changed',manifest)
        entry['status']='found'; entry['turn_id']=bound['turn_id']
        selections.append(reference_selection(row['event_id'],current))
        if bound['turn_id'] not in seen:
            seen.add(bound['turn_id']);manifest['turn_ids'].append(bound['turn_id'])
            groups.append(reference_group(current))
        group=next(g for g in groups if g['turn_id']==bound['turn_id'])
        if bound['target'] not in group['targets']:group['targets'].append(bound['target'])
        # Each accepted member may have a different confirmed-reply snapshot.
        # Merge only replies explicitly fixed by those bindings, never newly
        # sent text that was absent from every accepted choice.
        existing = {reply['id'] for reply in group['replies']}
        group['replies'].extend(reply for reply in current['replies'] if reply['id'] not in existing)
        group['replies'].sort(key=lambda reply:reply_order[reply['id']])
    block = serialize_reference_block(groups,selections)
    manifest['reference_characters']=len(block)
    if len(block)>12000:raise ReferenceError('reference_budget_exceeded',manifest)
    return block,manifest


def record_check(conn, turn, manifest, error=None, attempt_id=None):
    if not manifest.get('references'): return
    previous = conn.execute('SELECT 1 FROM reference_checks WHERE turn_id=?',(turn['id'],)).fetchone()
    conn.execute('INSERT INTO reference_checks VALUES (?,?,?,?,?,?,?,?,?)',
                 (identifier(),turn['id'],turn['persona_id'],LOCAL_USER_ID,'retry' if previous else 'initial',attempt_id,encoded(manifest),error,timestamp()))
