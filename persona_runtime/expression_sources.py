"""表达阶段复核主稿已使用的来源，不重新选材或改写主模型输入。"""
import json
import re

from .archive import fingerprint, turn_scope, read_exact
from .expression_assets import authority, ExpressionAssetError
from .models import LOCAL_USER_ID
from .profile import head, source_blocked
from .turn_input import load_turn_input


def capture(db, turn, details):
    selected = turn_scope(db, turn)
    events = {item['id'] for item in load_turn_input(db, turn)}
    from .knowledge_search_notice import read
    notice = read(db, turn)
    if notice:events.add(notice['event_id'])
    for group in details.get('history_groups', []):
        value = read_exact(db, selected, {'kind': 'turn', 'id': group['turn_id']})
        if value['status'] != 'found':raise ExpressionAssetError('expression_history_unavailable')
        events.update(item['id'] for item in value['members'] + value['replies'] if item.get('kind') != 'part')
    for item in load_turn_input(db, turn):
        ref = db.execute('SELECT target_json FROM input_references WHERE event_id=?', (item['id'],)).fetchone()
        if ref:
            value = read_exact(db, selected, json.loads(ref[0]))
            if value['status'] != 'found':raise ExpressionAssetError('expression_reference_unavailable')
            events.update(item['id'] for item in value['members'] + value['replies'] if item.get('kind') != 'part')
    memories = {}
    for item in details.get('memories', []):
        row = db.execute('SELECT * FROM memories WHERE id=?', (item['id'],)).fetchone()
        if row:
            memories[item['id']] = fingerprint([row['text'], row['revision'], row['status'], row['source_event_id']])
            events.add(row['source_event_id'])
            events.update(r[0] for r in db.execute('SELECT event_id FROM memory_sources WHERE memory_id=?', (item['id'],)))
    summary = None
    row = db.execute('SELECT * FROM summaries WHERE id=?', (details.get('summary_id'),)).fetchone()
    if row:
        events.update(json.loads(row['sources_json']))
        summary = {'id': row['id'], 'content_hash': fingerprint([row['items_json'], row['sources_json'], row['covered_json']])}
    frozen = json.loads(turn['profile_snapshot'])
    relation_refs = frozen['relationship']['source_ids']
    hashes = {}
    for ident in sorted(events):
        row = db.execute('SELECT * FROM events WHERE id=? AND persona_id=? AND user_id=?', (ident, turn['persona_id'], LOCAL_USER_ID)).fetchone()
        if not row:raise ExpressionAssetError('expression_source_event_missing')
        hashes[ident] = fingerprint(dict(row))
    return {'scope': selected, 'execution_revision': turn['execution_revision'], 'authority': authority(db, turn['persona_id']),
            'relationship': head(db, turn['persona_id'], 'relationship')['id'], 'events': hashes,
            'memories': memories, 'summary': summary, 'relationship_refs': relation_refs}


def validate(store, db, turn, context):
    proof, details = context['guard'], context['details']
    if turn.get('cancelled_at') or turn['execution_revision'] != proof['execution_revision'] or turn_scope(db, turn) != proof['scope']:
        raise ExpressionAssetError('expression_request_inactive')
    if authority(db, turn['persona_id']) != proof['authority'] or head(db, turn['persona_id'], 'relationship')['id'] != proof['relationship']:
        raise ExpressionAssetError('expression_persona_changed')
    if source_blocked(db, proof['relationship_refs']):raise ExpressionAssetError('expression_relationship_withdrawn')
    for ident, digest in proof['events'].items():
        row = db.execute('SELECT * FROM events WHERE id=? AND persona_id=? AND user_id=?', (ident, turn['persona_id'], LOCAL_USER_ID)).fetchone()
        if not row or fingerprint(dict(row)) != digest or db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?', (ident,)).fetchone():
            raise ExpressionAssetError('expression_source_event_changed')
    from .context_builders import memory_usable, summary_usable
    for ident, digest in proof['memories'].items():
        row = db.execute('SELECT * FROM memories WHERE id=?', (ident,)).fetchone()
        if not row or fingerprint([row['text'], row['revision'], row['status'], row['source_event_id']]) != digest or not memory_usable(db, turn, dict(row)):
            raise ExpressionAssetError('expression_memory_changed')
    if proof['summary']:
        row = db.execute('SELECT * FROM summaries WHERE id=?', (proof['summary']['id'],)).fetchone()
        if not row or fingerprint([row['items_json'], row['sources_json'], row['covered_json']]) != proof['summary']['content_hash'] or not summary_usable(db, turn, row):
            raise ExpressionAssetError('expression_summary_source_changed')
    from .knowledge_service import validate_used
    from .recall_runtime import validate as validate_recall
    validate_used(db, store.paths, turn['persona_id'], turn['channel'], details.get('knowledge'))
    validate_recall(db, turn, details.get('recall_support'))


def protected(body):
    spans = []
    # 只保护结构明确的代码、原样标记与数学片段，语义保持由人物任务和后续审阅验证。
    pattern = r'```[^\n]*\n[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$'
    for match in re.finditer(pattern, body):
        spans.append({'span_id': 'literal-' + str(len(spans) + 1), 'start': match.start(), 'end': match.end(),
                      'kind': 'code' if match.group().startswith('```') else 'formula' if match.group().startswith('$') else 'verbatim'})
    return spans
