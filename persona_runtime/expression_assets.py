"""PCAR权威范围内的表达来源、资产版本和候选；不生成回复或写人物事实。"""
import json
import math
import re
from copy import deepcopy

from .archive import fingerprint, scope as make_scope, find_turn, turn_scope
from .chat import terms
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .persona_management import active
from .vendor.maibot_expression_style import normalize_expression_style_for_learning, is_prompt_example_expression_style


VERSION = 'expression-assets-v1'
DEFAULTS = {'version': VERSION, 'enabled': False, 'learning_enabled': False,
            'learning_sessions': [],
            'max_candidates': 5, 'max_context_chars': 3000, 'max_assets': 256,
            'learning_hourly_limit': 0, 'learning_daily_limit': 0,
            'learning_max_sources': 10, 'learning_max_input_chars': 12000, 'learning_max_tokens': 1024}


class ExpressionAssetError(RuntimeErrorBase):
    pass


def text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ExpressionAssetError('expression_invalid_' + field)
    return value


def permissions_checked(value):
    if not isinstance(value, (list, tuple)) or not value or any(v not in ('use', 'learn') for v in value):
        raise ExpressionAssetError('expression_source_permission_invalid')
    return sorted(set(value))


def revision_checked(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ExpressionAssetError('expression_revision_invalid')


def normalized_scope(pid, selected):
    try:
        actual = make_scope(pid, selected['channel'], selected.get('bot', ''), selected.get('peer', ''))
    except (ValueError, TypeError, KeyError, RuntimeErrorBase):
        raise ExpressionAssetError('expression_scope_invalid') from None
    if actual != selected:
        raise ExpressionAssetError('expression_scope_invalid')
    return fingerprint(actual)


def persona(db, pid):
    row = db.execute('SELECT * FROM personas WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ExpressionAssetError('expression_persona_missing')
    active(db, pid)
    return dict(row)


def authority(db, pid):
    row = persona(db, pid)
    life = db.execute('SELECT revision FROM persona_lifecycle WHERE persona_id=?', (pid,)).fetchone()
    from .profile import head
    from .character import head as character_head
    return fingerprint([row['spec_json'], life[0] if life else 0,
                        head(db, pid, 'core')['id'], head(db, pid, 'expression')['id'], character_head(db, pid)['id']])


def policy(db, pid):
    persona(db, pid)
    row = db.execute('SELECT * FROM expression_asset_policies WHERE persona_id=?', (pid,)).fetchone()
    if not row:return {'revision':0,'data':deepcopy(DEFAULTS)}
    data=json.loads(row['data_json'])
    if (data.get('learning_hourly_limit'),data.get('learning_daily_limit'))==(1,4) and not db.execute(
            "SELECT 1 FROM knowledge_control WHERE key=?",('expression.learning.limits.'+pid,)).fetchone():
        data.update(learning_hourly_limit=0,learning_daily_limit=0)
    return {'revision':row['revision'],'data':data}


def _event(db, pid, selected, event_id):
    if selected['channel'] != 'desktop':
        raise ExpressionAssetError('expression_event_learning_channel_unsupported')
    row = db.execute('SELECT * FROM events WHERE id=? AND persona_id=? AND user_id=?', (event_id, pid, LOCAL_USER_ID)).fetchone()
    if not row or row['role'] != 'user':
        raise ExpressionAssetError('expression_self_or_nonuser_source')
    if db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?', (event_id,)).fetchone():
        raise ExpressionAssetError('expression_source_withdrawn')
    turn = find_turn(db, 'event', event_id)
    if not turn or turn['status'] != 'completed' or turn_scope(db, turn) != selected:
        raise ExpressionAssetError('expression_event_scope_unavailable')
    # 明确引用或转发不作为可学习的顶层原话。
    if db.execute('SELECT 1 FROM input_references WHERE event_id=? LIMIT 1', (event_id,)).fetchone():
        raise ExpressionAssetError('expression_quoted_source_not_learnable')
    return row['text'], fingerprint(dict(row))


def source(db, pid, selected, source_id, revision=None, permission='use'):
    key = normalized_scope(pid, selected)
    row = db.execute('SELECT * FROM expression_asset_sources WHERE id=? AND persona_id=? AND scope_key=?', (source_id, pid, key)).fetchone()
    if not row or row['state'] != 'active':
        raise ExpressionAssetError('expression_source_unavailable')
    if revision is not None and row['revision'] != revision:
        raise ExpressionAssetError('expression_source_revision_changed')
    version = dict(db.execute('SELECT * FROM expression_asset_source_versions WHERE source_id=? AND revision=?', (source_id, row['revision'])).fetchone())
    if permission is not None and permission not in json.loads(version['permissions_json']):
        raise ExpressionAssetError('expression_source_permission_denied')
    if fingerprint(version['body']) != version['content_hash']:
        raise ExpressionAssetError('expression_source_content_changed')
    ref = json.loads(version['ref_json'])
    if version['kind'] == 'user_event':
        if ref.get('authorized_session') and ref['authorized_session'] not in policy(db, pid)['data']['learning_sessions']:
            raise ExpressionAssetError('expression_session_grant_withdrawn')
        body, current = _event(db, pid, selected, ref['event_id'])
        if body != version['body'] or current != ref['canonical_hash']:
            raise ExpressionAssetError('expression_canonical_source_changed')
    return version | {'persona_id': pid, 'scope_key': key, 'scope': selected}


def supports(db, pid, selected, asset_id, revision):
    refs = []
    for row in db.execute('SELECT * FROM expression_asset_supports WHERE asset_id=? AND asset_revision=? ORDER BY source_id', (asset_id, revision)):
        value = source(db, pid, selected, row['source_id'], row['source_revision'])
        refs.append({'source_id': row['source_id'], 'revision': row['source_revision'], 'content_hash': value['content_hash']})
    if not refs:
        raise ExpressionAssetError('expression_asset_without_source')
    return refs


def asset(db, pid, selected, asset_id, revision=None):
    key = normalized_scope(pid, selected)
    row = db.execute('SELECT * FROM expression_assets WHERE id=? AND persona_id=? AND scope_key=?', (asset_id, pid, key)).fetchone()
    if not row or row['state'] != 'active':
        raise ExpressionAssetError('expression_asset_unavailable')
    if revision is not None and revision != row['revision']:
        raise ExpressionAssetError('expression_asset_revision_changed')
    version = dict(db.execute('SELECT * FROM expression_asset_versions WHERE asset_id=? AND revision=?', (asset_id, row['revision'])).fetchone())
    refs = supports(db, pid, selected, asset_id, row['revision'])
    return version | {'id': asset_id, 'supports': refs, 'keywords': json.loads(version['keywords_json'])}


class ExpressionAssets:
    def __init__(self, store):
        self.store = store

    def _operation(self, db, pid, operation_id, action, request, apply):
        text(operation_id, 'operation_id', 200)
        digest = fingerprint([pid, action, request])
        prior = db.execute('SELECT * FROM expression_asset_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if prior:
            if prior['persona_id'] != pid or prior['request_hash'] != digest:
                raise ExpressionAssetError('expression_operation_conflict')
            return json.loads(prior['result_json'])
        persona(db, pid)
        value = apply()
        db.execute('INSERT INTO expression_asset_operations VALUES(?,?,?,?,?,?)',
                   (operation_id, pid, action, digest, json.dumps(value, ensure_ascii=False), timestamp()))
        return value

    def policy(self, persona_id):
        with self.store.connection() as db:
            return policy(db, persona_id)

    def configure(self, persona_id, changes, expected_revision, operation_id):
        revision_checked(expected_revision)
        if not isinstance(changes, dict) or set(changes) - (set(DEFAULTS) - {'version'}):
            raise ExpressionAssetError('expression_policy_invalid')
        def update(db):
            current = policy(db, persona_id)
            if current['revision'] != expected_revision:
                raise ExpressionAssetError('expression_policy_revision_conflict')
            data = current['data'] | changes
            for key, value in data.items():
                if key in ('enabled', 'learning_enabled'):
                    if type(value) is not bool:raise ExpressionAssetError('expression_policy_invalid')
                elif key == 'learning_sessions':
                    if not isinstance(value, list) or len(value) > 16 or any(not isinstance(s, str) for s in value) or len(set(value)) != len(value):
                        raise ExpressionAssetError('expression_learning_session_invalid')
                    for sid in value:
                        row = db.execute('SELECT s.id,l.channel FROM sessions s LEFT JOIN session_lifecycle l ON l.session_id=s.id WHERE s.id=? AND s.persona_id=? AND s.user_id=?', (sid, persona_id, LOCAL_USER_ID)).fetchone()
                        if not row or row['channel'] not in (None, 'desktop'):
                            raise ExpressionAssetError('expression_learning_session_scope_invalid')
                elif key != 'version' and (type(value) is not int or not (0 if key in ('learning_hourly_limit','learning_daily_limit') else 1)<=value<=100000):
                    raise ExpressionAssetError('expression_policy_invalid')
            if data['max_candidates'] > 5 or data['max_assets'] > 5000 or data['learning_max_sources'] > 30:
                raise ExpressionAssetError('expression_policy_capacity_invalid')
            revision = current['revision'] + 1
            if {'learning_hourly_limit','learning_daily_limit'} & changes.keys():
                db.execute("INSERT OR IGNORE INTO knowledge_control VALUES(?,?)",('expression.learning.limits.'+persona_id,'1'))
            db.execute('INSERT INTO expression_asset_policies VALUES(?,?,?) ON CONFLICT(persona_id) DO UPDATE SET revision=excluded.revision,data_json=excluded.data_json',
                       (persona_id, revision, json.dumps(data, ensure_ascii=False)))
            db.execute('DELETE FROM expression_asset_vectors WHERE asset_id IN (SELECT id FROM expression_assets WHERE persona_id=?)', (persona_id,))
            return {'revision': revision, 'data': data}
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'policy', [changes, expected_revision], lambda: update(db))

    def register_source(self, persona_id, selected_scope, *, kind, parts=None, event_id=None,
                        permissions=('use',), attribution, operation_id, auto_granted=False):
        key = normalized_scope(persona_id, selected_scope)
        if type(auto_granted) is not bool:raise ExpressionAssetError('expression_auto_grant_invalid')
        if kind not in ('authored_examples', 'public_human', 'user_event'):
            raise ExpressionAssetError('expression_self_or_derived_source_forbidden')
        permissions = permissions_checked(permissions)
        text(attribution, 'attribution', 1000)
        def create(db):
            if kind == 'user_event':
                body, canonical = _event(db, persona_id, selected_scope, event_id)
                ref = {'event_id': event_id, 'canonical_hash': canonical}
                if auto_granted:
                    sid = db.execute('SELECT session_id FROM events WHERE id=?', (event_id,)).fetchone()[0]
                    if sid not in policy(db, persona_id)['data']['learning_sessions']:
                        raise ExpressionAssetError('expression_session_not_authorized')
                    ref['authorized_session'] = sid
            else:
                if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or set(p) != {'kind', 'text'} or p['kind'] != 'text' for p in parts):
                    raise ExpressionAssetError('expression_top_level_text_required')
                body = '\n'.join(text(p['text'], 'source_text', 12000) for p in parts)
                ref = {'attribution': attribution}
            text(body, 'source_text', 12000)
            sid = identifier()
            db.execute('INSERT INTO expression_asset_sources VALUES(?,?,?,?,?,?)',
                       (sid, persona_id, key, json.dumps(selected_scope, ensure_ascii=False), 1, 'active'))
            db.execute('INSERT INTO expression_asset_source_versions VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, 1, kind, json.dumps(ref, ensure_ascii=False), body, fingerprint(body), json.dumps(sorted(set(permissions))), attribution, timestamp()))
            return {'id': sid, 'revision': 1, 'content_hash': fingerprint(body)}
        request = [selected_scope, kind, parts, event_id, sorted(permissions), attribution, auto_granted]
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'source_register', request, lambda: create(db))

    def revise_source(self, persona_id, selected_scope, source_id, *, expected_revision, permissions,
                      operation_id, parts=None, revoke=False):
        normalized_scope(persona_id, selected_scope)
        revision_checked(expected_revision, minimum=1)
        permissions = permissions_checked(permissions)
        if type(revoke) is not bool:raise ExpressionAssetError('expression_revoke_invalid')
        def change(db):
            owner = db.execute('SELECT * FROM expression_asset_sources WHERE id=? AND persona_id=? AND scope_key=?',
                               (source_id, persona_id, normalized_scope(persona_id, selected_scope))).fetchone()
            if not owner or owner['revision'] != expected_revision:
                raise ExpressionAssetError('expression_source_revision_changed')
            current = dict(db.execute('SELECT * FROM expression_asset_source_versions WHERE source_id=? AND revision=?', (source_id, expected_revision)).fetchone())
            revision = expected_revision + 1
            body, ref = current['body'], current['ref_json']
            if parts is not None:
                if current['kind'] == 'user_event' or not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or set(p) != {'kind', 'text'} or p['kind'] != 'text' for p in parts):
                    raise ExpressionAssetError('expression_top_level_text_required')
                body = '\n'.join(text(p['text'], 'source_text', 12000) for p in parts)
                text(body, 'source_text', 12000)
            db.execute('INSERT INTO expression_asset_source_versions VALUES(?,?,?,?,?,?,?,?,?)',
                       (source_id, revision, current['kind'], ref, body, fingerprint(body), json.dumps(sorted(set(permissions))), current['attribution'], timestamp()))
            db.execute('UPDATE expression_asset_sources SET revision=?,state=? WHERE id=?',
                       (revision, 'revoked' if revoke else 'active', source_id))
            return {'id': source_id, 'revision': revision, 'state': 'revoked' if revoke else 'active'}
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'source_revise', [selected_scope, source_id, expected_revision, permissions, parts, revoke], lambda: change(db))

    def _put(self, db, pid, selected, situation, style, keywords, source_refs, asset_id=None, expected_revision=0, restored_from=None):
        revision_checked(expected_revision)
        key = normalized_scope(pid, selected)
        text(situation, 'situation', 200)
        style = normalize_expression_style_for_learning(text(style, 'style', 240))
        text(style, 'style', 240)
        if is_prompt_example_expression_style(style) or style in ('表达方式或语言风格',) or situation == '适用情境':
            raise ExpressionAssetError('expression_prompt_example_contamination')
        if not isinstance(keywords, list) or len(keywords) > 20:
            raise ExpressionAssetError('expression_keywords_invalid')
        for word in keywords:text(word, 'keyword', 80)
        if not isinstance(source_refs, list) or not source_refs:
            raise ExpressionAssetError('expression_asset_without_source')
        for support in source_refs:
            if not isinstance(support, dict) or not isinstance(support.get('source_id'), str):raise ExpressionAssetError('expression_source_reference_invalid')
            revision_checked(support.get('revision'), minimum=1)
        refs = {s['source_id']: s['revision'] for s in source_refs}
        for sid, revision in refs.items():source(db, pid, selected, sid, revision)
        digest = fingerprint([situation, style, keywords])
        if asset_id is None:
            existing = db.execute('''SELECT a.id,a.revision FROM expression_assets a JOIN expression_asset_versions v
                ON v.asset_id=a.id AND v.revision=a.revision WHERE a.persona_id=? AND a.scope_key=?
                AND a.state='active' AND v.content_hash=? AND EXISTS(SELECT 1 FROM expression_asset_supports s
                WHERE s.asset_id=a.id AND s.asset_revision=a.revision)''', (pid, key, digest)).fetchone()
            if existing:
                asset_id, expected_revision = existing['id'], existing['revision']
                for support in db.execute('SELECT * FROM expression_asset_supports WHERE asset_id=? AND asset_revision=?', (asset_id, expected_revision)):
                    try:source(db, pid, selected, support['source_id'], support['source_revision'])
                    except ExpressionAssetError:continue
                    refs.setdefault(support['source_id'], support['source_revision'])
            else:
                maximum = policy(db, pid)['data']['max_assets']
                if db.execute('SELECT count(*) FROM expression_assets WHERE persona_id=? AND scope_key=?', (pid, key)).fetchone()[0] >= maximum:
                    raise ExpressionAssetError('expression_library_capacity_reached')
                asset_id = identifier()
                db.execute('INSERT INTO expression_assets VALUES(?,?,?,?,?)', (asset_id, pid, key, 0, 'active'))
        current = db.execute('SELECT * FROM expression_assets WHERE id=? AND persona_id=? AND scope_key=?', (asset_id, pid, key)).fetchone()
        if not current or current['revision'] != expected_revision:
            raise ExpressionAssetError('expression_asset_revision_conflict')
        if expected_revision and not db.execute('SELECT 1 FROM expression_asset_supports WHERE asset_id=? AND asset_revision=?',
                                                (asset_id, expected_revision)).fetchone():
            raise ExpressionAssetError('expression_asset_requires_versioned_source')
        if current['state'] != 'active' and restored_from is None:
            raise ExpressionAssetError('expression_disabled_asset_requires_restore')
        revision = expected_revision + 1
        db.execute('INSERT INTO expression_asset_versions VALUES(?,?,?,?,?,?,?,?)',
                   (asset_id, revision, situation, style, json.dumps(keywords, ensure_ascii=False), digest, restored_from, timestamp()))
        for sid, rev in sorted(refs.items()):db.execute('INSERT INTO expression_asset_supports VALUES(?,?,?,?)', (asset_id, revision, sid, rev))
        db.execute('UPDATE expression_assets SET revision=?,state=\'active\' WHERE id=?', (revision, asset_id))
        for word in terms(' '.join([situation, style, *keywords])):
            db.execute('INSERT INTO expression_asset_terms VALUES(?,?,?)', (word, asset_id, revision))
        return {'id': asset_id, 'revision': revision, 'content_hash': digest}

    def put(self, persona_id, selected_scope, *, situation, style, source_refs, operation_id,
            keywords=(), asset_id=None, expected_revision=0):
        request = [selected_scope, situation, style, source_refs, list(keywords), asset_id, expected_revision]
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'asset_put', request,
                                   lambda: self._put(db, persona_id, selected_scope, situation, style, list(keywords), source_refs, asset_id, expected_revision))

    def disable(self, persona_id, selected_scope, asset_id, expected_revision, operation_id):
        revision_checked(expected_revision, minimum=1)
        def apply(db):
            owner = db.execute('SELECT revision FROM expression_assets WHERE id=? AND persona_id=? AND scope_key=?',
                               (asset_id, persona_id, normalized_scope(persona_id, selected_scope))).fetchone()
            if not owner or owner['revision'] != expected_revision:raise ExpressionAssetError('expression_asset_revision_conflict')
            db.execute('UPDATE expression_assets SET state=\'disabled\' WHERE id=?', (asset_id,))
            return {'id': asset_id, 'state': 'disabled'}
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'asset_disable', [selected_scope, asset_id, expected_revision], lambda: apply(db))

    def restore(self, persona_id, selected_scope, asset_id, target_revision, expected_revision, operation_id):
        revision_checked(target_revision, minimum=1)
        revision_checked(expected_revision, minimum=1)
        def apply(db):
            key = normalized_scope(persona_id, selected_scope)
            owner = db.execute('SELECT * FROM expression_assets WHERE id=? AND persona_id=? AND scope_key=?', (asset_id, persona_id, key)).fetchone()
            if not owner or owner['revision'] != expected_revision:raise ExpressionAssetError('expression_asset_revision_conflict')
            old = db.execute('SELECT * FROM expression_asset_versions WHERE asset_id=? AND revision=?', (asset_id, target_revision)).fetchone()
            if not old:raise ExpressionAssetError('expression_asset_revision_missing')
            refs = supports(db, persona_id, selected_scope, asset_id, target_revision)
            return self._put(db, persona_id, selected_scope, old['situation'], old['style'], json.loads(old['keywords_json']), refs,
                             asset_id, expected_revision, target_revision)
        with self.store.connection(write=True) as db:
            return self._operation(db, persona_id, operation_id, 'asset_restore', [selected_scope, asset_id, target_revision, expected_revision], lambda: apply(db))

    def show(self, persona_id, selected_scope, asset_id):
        with self.store.connection() as db:
            persona(db, persona_id)
            return asset(db, persona_id, selected_scope, asset_id)

    def status(self, persona_id):
        with self.store.connection() as db:
            current = policy(db, persona_id)
            return {'policy': current,
                    'sources': db.execute('SELECT count(*) FROM expression_asset_sources WHERE persona_id=?', (persona_id,)).fetchone()[0],
                    'assets': db.execute('SELECT count(*) FROM expression_assets WHERE persona_id=?', (persona_id,)).fetchone()[0],
                    'pending_proposals': db.execute('SELECT count(*) FROM expression_asset_proposals WHERE persona_id=? AND state=\'pending\'', (persona_id,)).fetchone()[0],
                    'learning_jobs': {r[0]: r[1] for r in db.execute('SELECT state,count(*) FROM expression_learning_jobs WHERE persona_id=? GROUP BY state', (persona_id,))}}
