"""A durable persona utterance before native retrieval, using the existing call ledger."""
import json

from .archive import fingerprint
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp

VERSION = 'persona-search-notice-v4'
SUPPORTED_VERSIONS = ('persona-search-notice-v1', 'persona-search-notice-v2', 'persona-search-notice-v3', VERSION)
STAGE = 'conversation_search_notice'


def request_model(model, profile=VERSION):
    # The persona speaks promptly; deep thinking belongs to the subsequent
    # retrieval task. Preserve its frozen voice, provider and input allowance.
    if profile not in SUPPORTED_VERSIONS:
        raise ValueError('search_notice_profile_invalid')
    result = {key: value for key, value in model.items() if key not in (
        '_native_web_profile', '_json_output', '_reasoning_effort',
        '_expression_snapshot', '_generation_profile', '_native_text_profile')}
    return result | {'max_tokens': 512, '_reply_mode': 'legacy_full', '_search_notice_profile': profile}


def read(db, turn):
    return read_many(db, [turn]).get(turn['id'])


def read_many(db, turns):
    if not turns:
        return {}
    keys = {'knowledge.search_notice.turn.' + turn['id']: turn for turn in turns}
    marks = ','.join('?' for _ in keys)
    result = {}
    for row in db.execute(f'''SELECT c.key,c.value,e.id,e.text,e.persona_id,e.session_id,e.created_at
            FROM knowledge_control c JOIN events e ON e.id=json_extract(c.value,'$.data.event_id')
            WHERE c.key IN ({marks}) AND NOT EXISTS(SELECT 1 FROM source_exclusions x WHERE x.event_id=e.id)''', tuple(keys)):
        turn = keys[row['key']]
        value = json.loads(row['value'])['data']
        if (row['persona_id'], row['session_id'], value.get('channel')) != (turn['persona_id'], turn['session_id'], turn['channel']):
            continue
        result[turn['id']] = {'event_id': row['id'], 'text': row['text'], 'created_at': row['created_at'],
            'task_id': value['task_id'], 'version': value['version']}
    return result


class SearchNotice:
    def __init__(self, processor):
        self.processor = processor
        self.store = processor.store

    def context(self, db, task, payload):
        root = db.execute('SELECT * FROM turns WHERE id=?',
            (payload['knowledge_processing']['root_turn_id'],)).fetchone()
        if not root or root['channel'] != 'desktop':
            raise RuntimeErrorBase('search_notice_scope_changed')
        turn = dict(root)
        model = json.loads(task['model_snapshot'])
        if model['_search_notice_profile'] != VERSION:
            raise RuntimeErrorBase('search_notice_profile_retired')
        # Render the same local persona/relationship/history system. Only this
        # short utterance's output purpose changes; no public-search context is
        # given personality, memory or private conversation.
        turn['_search_notice'] = True
        cognitive = self.processor.engine.chat.cognitive
        character = cognitive.characters.recheck(db, dict(root), None)
        evidence = cognitive.dialogue.basic_evidence(db, dict(root))
        messages, details = cognitive.dialogue.render(db, turn, evidence, character)
        manifest = cognitive._manifest(db, turn, messages, details)
        from .provider_compat import describe_request
        from .reply_protocol import descriptor
        manifest.update(provider_request_contract=describe_request(model),
            model_snapshot_hash=fingerprint({key: value for key, value in model.items() if key != 'encrypted_key'}),
            reply_output_contract=descriptor('legacy_full'))
        manifest.update(task_type='reflection', purpose='persona_search_notice',
            organization_task_id=task['id'], search_notice_version=VERSION)
        manifest['hard_constraints'] += ['jev_web_precedes_notice', 'notice_precedes_search', 'one_saved_utterance']
        return {'messages': messages, 'manifest': manifest}

    def publish(self, db, task, payload, response, audit):
        from .reply_batch import validate_reply
        if audit.get('provider', {}).get('finish_reason') in ('length', 'content_filter'):
            raise RuntimeErrorBase('search_notice_output_incomplete')
        text = validate_reply(response, 'legacy_full').text
        root = dict(db.execute('SELECT * FROM turns WHERE id=?',
            (payload['knowledge_processing']['root_turn_id'],)).fetchone())
        from .recall_runtime import validate as validate_recall,queue_feedback
        manifest=db.execute('SELECT organization_attempt_id,manifest_json FROM context_manifests WHERE organization_task_id=? ORDER BY rowid DESC LIMIT 1',(task['id'],)).fetchone()
        meta=json.loads(manifest['manifest_json']).get('recall_support') if manifest else None
        validate_recall(db,root,meta)
        key = 'knowledge.search_notice.turn.' + root['id']
        old = db.execute('SELECT value FROM knowledge_control WHERE key=?', (key,)).fetchone()
        if old:
            value = json.loads(old[0])['data']
            if value['task_id'] != task['id'] or value['response_hash'] != fingerprint(response):
                raise RuntimeErrorBase('search_notice_receipt_conflict')
            return {'event_id': value['event_id'], 'published': True}
        event_id = identifier()
        db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'persona',?,?)",
            (event_id, root['session_id'], root['persona_id'], LOCAL_USER_ID, text, timestamp()))
        value = {'event_id': event_id, 'task_id': task['id'], 'channel': root['channel'],
            'version': payload['knowledge_processing']['input']['notice_profile'], 'response_hash': fingerprint(response)}
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',
            (key, json.dumps({'revision': 0, 'data': value}, ensure_ascii=False)))
        from .progress import record
        record(db, root['id'], 'search_notice_published')
        if manifest:queue_feedback(db,root,manifest['organization_attempt_id'],'notice',meta)
        return {'event_id': event_id, 'published': True}
