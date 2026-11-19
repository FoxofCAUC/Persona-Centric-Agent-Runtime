"""按授权来源学习表达提案；一个任务一次派发，发布与人物演化分开。"""
from .call_limits import resume_on_limit, gate, CallLimitPending, granted, flow, workflow
import asyncio
import json
import time

from .archive import fingerprint
from .config import atomic_json
from .expression_assets import ExpressionAssets, ExpressionAssetError, authority, normalized_scope, policy, source, persona
from .models import RuntimeErrorBase, identifier, timestamp
from .provider_compat import build_request, usage_fields
from .provider_policy import require_generation_provider
from .vendor.maibot_expression_learning_prompt import PROMPT
from .vendor.maibot_expression_style import normalize_expression_style_for_learning, is_prompt_example_expression_style


def learning_messages(sources):
    # MaiBot模板允许无来源候选；PCAR维护提案必须有有效的冻结来源。
    optional = 'source_id 为可选的原文片段整数 id，程序会提取该片段作为参考依据；无法确定具体片段时可省略或填 null，仍可输出候选供用户确认。'
    if PROMPT.count(optional) != 1:
        raise ExpressionAssetError('expression_learning_prompt_boundary_changed')
    template = PROMPT.replace(optional, 'source_id 必须是本次原文片段的有效整数 id；没有对应原文依据的条目不输出。', 1)
    template = template.replace('{{', '{').replace('}}', '}')
    items = [{'id': index, 'text': value['body']} for index, value in enumerate(sources, 1)]
    return [{'role': 'system', 'content': '输出一个严格JSON对象。\n' + template},
            {'role': 'user', 'content': json.dumps(items, ensure_ascii=False)},
            {'role': 'user', 'content': '依据以上原文提取表达方式，返回包含candidates的JSON对象。'}]


def filter_candidates(raw, sources, banned_names=()):
    """适配MaiBot _filter_expressions：来源编号、SELF、黑名单及示例污染。"""
    from .reply_batch import _unique_object
    try:value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):raise ExpressionAssetError('expression_learning_invalid_json') from None
    if not isinstance(value, dict) or set(value) != {'candidates'} or not isinstance(value['candidates'], list) or len(value['candidates']) > 30:
        raise ExpressionAssetError('expression_learning_invalid_envelope')
    accepted, rejected, seen = [], [], set()
    names = {name.casefold() for name in banned_names if isinstance(name, str) and name.strip()}
    for index, item in enumerate(value['candidates']):
        reason = None
        if not isinstance(item, dict) or set(item) != {'title', 'description', 'source_id'}:
            rejected.append({'index': index, 'reason': 'candidate_shape'});continue
        sid = str(item['source_id'])
        if not sid.isdigit() or not 1 <= int(sid) <= len(sources):
            rejected.append({'index': index, 'reason': 'invalid_source_id'});continue
        original = sources[int(sid) - 1]
        situation, style = item['title'], item['description']
        if not isinstance(situation, str) or not isinstance(style, str):
            rejected.append({'index': index, 'reason': 'candidate_shape'});continue
        style = normalize_expression_style_for_learning(style)
        context = original['body']
        if original.get('speaker') == 'SELF' or original.get('blacklisted'):
            reason = 'self_or_blacklisted_source'
        elif not situation.strip() or not style.strip() or len(situation) > 200 or len(style) > 240:
            reason = 'candidate_capacity'
        elif any(token in situation or token in style or token in context for token in ('SELF', '[表情包', '[图片')):
            reason = 'non_top_level_or_self_content'
        elif style.casefold() in names or is_prompt_example_expression_style(style) or style == '表达方式或语言风格' or situation == '适用情境':
            reason = 'prompt_example_or_persona_name'
        digest = fingerprint([situation, style])
        if digest in seen:reason = 'duplicate_in_batch'
        if reason:
            rejected.append({'index': index, 'reason': reason});continue
        seen.add(digest)
        accepted.append({'situation': situation, 'style': style, 'source_refs': [{'source_id': original['source_id'], 'revision': original['revision']}],
                         'source_quote': context, 'content_hash': digest})
    return accepted, rejected


def checked_manifest(db, pid, manifest):
    current = policy(db, pid)
    if (current['revision'] != manifest['policy_revision'] or fingerprint(current['data']) != manifest['policy_hash']
            or not current['data']['learning_enabled'] or authority(db, pid) != manifest['authority']):
        raise ExpressionAssetError('expression_learning_policy_changed')
    values = []
    for item in manifest['sources']:
        fresh = source(db, pid, manifest['scope'], item['source_id'], item['revision'], permission='learn')
        if fresh['content_hash'] != item['content_hash']:
            raise ExpressionAssetError('expression_learning_source_changed')
        values.append(fresh)
    return values


class ExpressionLearning:
    def __init__(self, store, client=None):
        self.store = store
        if client is None:
            from .expression_transport import ExpressionHTTPClient
            client = ExpressionHTTPClient()
        self.client = client
        self.assets = ExpressionAssets(store)

    def prepare(self, persona_id, selected_scope, source_ids, model, operation_id):
        require_generation_provider(model)
        key = normalized_scope(persona_id, selected_scope)
        if not isinstance(source_ids, list) or not source_ids or len(set(source_ids)) != len(source_ids):
            raise ExpressionAssetError('expression_learning_source_list_invalid')
        def apply(db):
            configured = policy(db, persona_id)
            limits = configured['data']
            if not limits['learning_enabled']:raise ExpressionAssetError('expression_learning_disabled')
            if len(source_ids) > limits['learning_max_sources']:raise ExpressionAssetError('expression_learning_capacity')
            sources = [source(db, persona_id, selected_scope, sid, permission='learn') for sid in source_ids]
            messages = learning_messages(sources)
            if len(json.dumps(messages, ensure_ascii=False)) > limits['learning_max_input_chars']:
                raise ExpressionAssetError('expression_learning_capacity')
            job = identifier()
            public = {'base_url': model['base_url'], 'name': model['name'], 'timeout': 120,
                      'max_tokens': limits['learning_max_tokens'], '_json_output': True,
                      '_dialogue_profile': {'version': 'natural-dialogue-v8', 'seed': job, 'temperature': .2}}
            request = build_request(public, messages)[0]
            manifest = {'scope': selected_scope, 'policy_revision': configured['revision'],
                        'policy_hash': fingerprint(configured['data']), 'authority': authority(db, persona_id),
                        'sources': [{k: value[k] for k in ('source_id', 'revision', 'kind', 'body', 'content_hash')} for value in sources]}
            db.execute('''INSERT INTO expression_learning_jobs
             (id,persona_id,scope_key,state,source_manifest_json,request_json,request_hash,model_json,created_at)
             VALUES(?,?,?,'prepared',?,?,?,?,?)''',
                       (job, persona_id, key, json.dumps(manifest, ensure_ascii=False), json.dumps(request, ensure_ascii=False),
                        fingerprint(request), json.dumps(public), timestamp()))
            return {'id': job, 'state': 'prepared', 'request_hash': fingerprint(request)}
        with self.store.connection(write=True) as db:
            return self.assets._operation(db, persona_id, operation_id, 'learning_prepare', [selected_scope, source_ids, model['base_url'], model['name']], lambda: apply(db))

    def get(self, job_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM expression_learning_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:raise ExpressionAssetError('expression_learning_job_missing')
            value = dict(row)
            value.pop('raw_response', None)
            return value

    def finalize(self, job_id):
        with self.store.connection(write=True) as db:
            row = dict(db.execute('SELECT * FROM expression_learning_jobs WHERE id=?', (job_id,)).fetchone())
            import hashlib
            raw = bytes(row['raw_response']) if row['raw_response'] is not None else None
            if raw is not None and hashlib.sha256(raw).hexdigest() != row['raw_response_hash']:
                raise ExpressionAssetError('expression_learning_receipt_changed')
            if row['state'] in ('completed', 'stale', 'cancelled'):
                return {'id': job_id, 'state': row['state'], 'result': json.loads(row['result_json'] or '{}')}
            if raw is None:
                raise ExpressionAssetError('expression_learning_receipt_unavailable')
            try:data = json.loads(raw)
            except (ValueError, TypeError):data = {}
            usage = usage_fields(data.get('usage') if isinstance(data, dict) else None, deepseek=True)
            db.execute('UPDATE expression_learning_jobs SET usage_json=? WHERE id=?', (json.dumps(usage) if usage is not None else None, job_id))
            manifest = json.loads(row['source_manifest_json'])
            try:sources = checked_manifest(db, row['persona_id'], manifest)
            except RuntimeErrorBase as error:
                result = {'status': 'source_or_policy_changed', 'proposals': [], 'error': str(error)}
                db.execute('UPDATE expression_learning_jobs SET state=\'stale\',result_json=?,error_code=? WHERE id=?', (json.dumps(result), str(error), job_id))
                return {'id': job_id, 'state': 'stale', 'result': result}
            try:
                choice = data['choices'][0]
                if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls'):
                    raise ExpressionAssetError('expression_learning_incomplete_output')
                owner = persona(db, row['persona_id'])
                accepted, rejected = filter_candidates(choice['message']['content'], sources, [owner['name']])
                proposals = []
                for candidate in accepted:
                    ident = identifier()
                    db.execute('INSERT INTO expression_asset_proposals VALUES(?,?,?,?,?,?)',
                               (ident, job_id, row['persona_id'], row['scope_key'], json.dumps(candidate, ensure_ascii=False, sort_keys=True), 'pending'))
                    proposals.append(ident)
                result = {'status': 'accepted', 'proposals': proposals, 'rejected': rejected, 'assets_automatically_published': False}
            except (KeyError, IndexError, TypeError, ValueError, ExpressionAssetError) as error:
                result = {'status': 'invalid_output', 'proposals': [],
                          'error': str(error) if isinstance(error, ExpressionAssetError) else 'expression_learning_invalid_response'}
            db.execute('UPDATE expression_learning_jobs SET state=\'completed\',result_json=? WHERE id=?', (json.dumps(result, ensure_ascii=False), job_id))
            return {'id': job_id, 'state': 'completed', 'result': result}

    @workflow
    @resume_on_limit
    async def run(self, job_id, model):
        require_generation_provider(model)
        with self.store.connection() as db:
            found = db.execute('SELECT * FROM expression_learning_jobs WHERE id=?', (job_id,)).fetchone()
            if not found:raise ExpressionAssetError('expression_learning_job_missing')
            row = dict(found)
        if row['raw_response'] is not None:
            return self.finalize(job_id)
        if row['state'] != 'prepared' or row['attempt_id']:
            return {'id': job_id, 'state': row['state'], 'new_call': False}
        public = json.loads(row['model_json'])
        if (model['base_url'], model['name']) != (public['base_url'], public['name']):
            raise ExpressionAssetError('expression_learning_model_changed')
        live_model = public | {'encrypted_key': model['encrypted_key']}
        from .private_personas import guard_cloud_dispatch
        guard_cloud_dispatch(self.store, row['persona_id'], 'expression_learning', live_model)
        request = json.loads(row['request_json'])
        if fingerprint(request) != row['request_hash'] or build_request(live_model, request['messages'])[0] != request:
            raise ExpressionAssetError('expression_learning_request_changed')
        now = time.time()
        with self.store.connection(write=True) as db:
            fresh = dict(db.execute('SELECT * FROM expression_learning_jobs WHERE id=?', (job_id,)).fetchone())
            if fresh['state'] != 'prepared' or fresh['attempt_id']:
                return {'id': job_id, 'state': fresh['state'], 'new_call': False}
            try:checked_manifest(db, row['persona_id'], json.loads(row['source_manifest_json']))
            except RuntimeErrorBase as error:
                db.execute('UPDATE expression_learning_jobs SET state=\'stale\',error_code=?,result_json=? WHERE id=?',
                           (str(error), json.dumps({'status': 'source_or_policy_changed', 'proposals': []}), job_id))
                return {'id': job_id, 'state': 'stale', 'new_call': False}
            configured = policy(db, row['persona_id'])['data']
            from .config import ConfigStore
            settings=ConfigStore(self.store.paths).load()
            limit_settings=settings['call_limits']
            unchecked=('saved_automatic' in limit_settings and not limit_settings['enabled']
                       and not any(settings[g][k] for g,k in (('model','hourly_limit'),('organization','hourly_limit'),
                           ('organization','daily_limit'),('character_selector','hourly_limit'),('character_selector','daily_limit'))))
            for period, field in ((3600, 'learning_hourly_limit'), (86400, 'learning_daily_limit')):
                used = db.execute('SELECT count(*) FROM expression_learning_jobs WHERE persona_id=? AND started_epoch>?', (row['persona_id'], now - period)).fetchone()[0]
                if not unchecked and configured[field] and used >= configured[field] and not granted(db,flow.get() or 'learning/'+job_id):
                    raise CallLimitPending(flow.get() or 'learning/'+job_id,'learning',now+1)
            gate(db,self.store,'learning','learning/'+job_id)
            attempt = identifier()
            db.execute('UPDATE expression_learning_jobs SET state=\'dispatched\',attempt_id=?,started_epoch=? WHERE id=?', (attempt, now, job_id))

        def save_raw(raw):
            import hashlib
            with self.store.connection(write=True) as db:
                changed = db.execute('UPDATE expression_learning_jobs SET raw_response=?,raw_response_hash=? WHERE id=? AND attempt_id=? AND raw_response IS NULL',
                                     (raw, hashlib.sha256(raw).hexdigest(), job_id, attempt)).rowcount
                if changed != 1:raise ExpressionAssetError('expression_learning_receipt_conflict')
        try:
            received = await self.client.complete(live_model, request['messages'], save_raw=save_raw)
            with self.store.connection(write=True) as db:
                db.execute('UPDATE expression_learning_jobs SET transport_json=? WHERE id=?', (json.dumps(received['diagnostics']), job_id))
        except BaseException as error:
            failure = None
            with self.store.connection(write=True) as db:
                saved = db.execute('SELECT raw_response FROM expression_learning_jobs WHERE id=?', (job_id,)).fetchone()[0]
                if saved is None:
                    diagnostic = getattr(error, 'diagnostics', {})
                    unsent = diagnostic.get('request_headers_sent') is False and diagnostic.get('response_headers_received') is False
                    rejected = diagnostic.get('response_headers_received') is True and diagnostic.get('http_status', 200) != 200
                    state = 'failed' if unsent or rejected else 'unknown'
                    result = {'status': 'not_dispatched' if unsent else 'request_failed' if rejected else 'unknown', 'proposals': [], 'usage_class': 'not_applicable' if unsent else 'unknown'}
                    db.execute('UPDATE expression_learning_jobs SET state=?,transport_json=?,result_json=?,error_code=? WHERE id=?',
                               (state, json.dumps(diagnostic), json.dumps(result), 'expression_learning_transport_failed', job_id))
                    failure = {'id': job_id, 'state': state, 'new_call': False if unsent else None,
                               'dispatch_attempted': True, 'result': result}
            if isinstance(error, asyncio.CancelledError):
                if saved is not None:self.finalize(job_id)
                raise
            if failure is not None:return failure
        return self.finalize(job_id)

    def publish(self, persona_id, selected_scope, proposal_id, operation_id):
        key = normalized_scope(persona_id, selected_scope)
        def apply(db):
            proposal = db.execute('SELECT * FROM expression_asset_proposals WHERE id=? AND persona_id=? AND scope_key=?', (proposal_id, persona_id, key)).fetchone()
            if not proposal or proposal['state'] != 'pending':raise ExpressionAssetError('expression_proposal_unavailable')
            job = db.execute('SELECT * FROM expression_learning_jobs WHERE id=?', (proposal['job_id'],)).fetchone()
            checked_manifest(db, persona_id, json.loads(job['source_manifest_json']))
            data = json.loads(proposal['data_json'])
            result = self.assets._put(db, persona_id, selected_scope, data['situation'], data['style'], [], data['source_refs'])
            db.execute('UPDATE expression_asset_proposals SET state=\'accepted\' WHERE id=?', (proposal_id,))
            db.execute('INSERT OR IGNORE INTO expression_asset_observations VALUES(?,?,?)', (result['id'], proposal['job_id'], timestamp()))
            return result
        with self.store.connection(write=True) as db:
            return self.assets._operation(db, persona_id, operation_id, 'proposal_publish', [selected_scope, proposal_id], lambda: apply(db))

    async def step(self, model):
        """供既有宿主调用的一次有界工作，不创建线程、服务或自主学习来源。"""
        with self.store.connection() as db:
            row = db.execute("SELECT id FROM expression_learning_jobs WHERE state='prepared' ORDER BY created_at,id LIMIT 1").fetchone()
        if row:return await self.run(row[0], model)
        with self.store.connection() as db:
            people = [r[0] for r in db.execute('SELECT persona_id FROM expression_asset_policies WHERE json_extract(data_json,\'$.learning_enabled\')=1 ORDER BY persona_id')]
        for pid in people:
            queued = self.collect_authorized(pid, model)
            if queued:return await self.run(queued['id'], model)
        return None

    def collect_authorized(self, persona_id, model):
        """一次授权范围内增量登记顶层用户原话；游标重入不会创建第二个付费位置。"""
        with self.store.connection() as db:
            configured = policy(db, persona_id)
            limits = configured['data']
            if not limits['learning_enabled']:return None
            sessions = list(limits['learning_sessions'])
        from .archive import scope as make_scope, find_turn
        from .ingress import manual_pending
        selected = make_scope(persona_id, 'desktop')
        for sid in sessions:
            with self.store.connection() as db:
                cursor = db.execute('SELECT last_seq FROM expression_learning_scan WHERE persona_id=? AND session_id=?', (persona_id, sid)).fetchone()
                after = cursor[0] if cursor else 0
                held = manual_pending(db, persona_id, sid)
                events = [dict(r) for r in db.execute("""SELECT * FROM events WHERE persona_id=? AND session_id=?
                    AND role='user' AND seq>? AND seq<coalesce(
                        (SELECT min(e.seq) FROM input_batch_members m JOIN events e ON e.id=m.event_id
                         WHERE m.batch_id=?),9223372036854775807) ORDER BY seq LIMIT ?""",
                    (persona_id, sid, after, held['id'] if held else None, limits['learning_max_sources']))]
            sources, last = [], after
            for event in events:
                with self.store.connection() as db:
                    turn = find_turn(db, 'event', event['id'])
                if turn and turn['status'] in ('queued', 'running'):break
                try:
                    record = self.assets.register_source(persona_id, selected, kind='user_event', event_id=event['id'],
                              permissions=('use', 'learn'), attribution='authorized session ' + sid,
                              operation_id='expression-event:' + fingerprint([persona_id, sid, event['id'], configured['revision']]), auto_granted=True)
                    sources.append(record['id'])
                except ExpressionAssetError:
                    pass
                last = event['seq']
            if not sources:
                if last > after:
                    with self.store.connection(write=True) as db:
                        db.execute('INSERT INTO expression_learning_scan VALUES(?,?,?) ON CONFLICT(persona_id,session_id) DO UPDATE SET last_seq=max(last_seq,excluded.last_seq)', (persona_id, sid, last))
                continue
            queued = self.prepare(persona_id, selected, sources, model,
                                  'expression-session-batch:' + fingerprint([persona_id, sid, configured['revision'], sources]))
            with self.store.connection(write=True) as db:
                db.execute('INSERT INTO expression_learning_scan VALUES(?,?,?) ON CONFLICT(persona_id,session_id) DO UPDATE SET last_seq=max(last_seq,excluded.last_seq)', (persona_id, sid, last))
            return queued
        return None
