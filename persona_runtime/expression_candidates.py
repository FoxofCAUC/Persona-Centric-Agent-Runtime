"""小表达库的本地选材与可复核快照；索引和向量不拥有来源权威。"""
import json
import math

from .archive import fingerprint
from .chat import terms
from .expression_assets import ExpressionAssetError, asset, authority, normalized_scope, policy, persona
from .expression_contract import ScopedText, text_hash
from .models import identifier, timestamp
from .character_retrieval import literal_match


QUERY_STOP = frozenset(('对方','现在','时候','当前','这次','本次','这个','那个','一下','刚才','今天',
                        '我们','你们','自己','怎么','什么','需要','可以','知道','事情','回应','表达','使用'))


def matching_terms(value):
    return terms(value) - QUERY_STOP


def rendered(item):
    return '情境：' + item['situation'] + '\n表达：' + item['style']


def vector(value, dimension):
    result = [float(v) for v in value]
    if len(result) != dimension or not result or not all(math.isfinite(v) for v in result):
        raise ExpressionAssetError('expression_vector_invalid')
    norm = math.sqrt(sum(v * v for v in result))
    if norm <= 0:raise ExpressionAssetError('expression_vector_invalid')
    return [v / norm for v in result]


def cosine(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))


class ExpressionCandidates:
    def __init__(self, store, embedding=None):
        self.store, self.embedding = store, embedding

    def _semantic(self, query, rows):
        if self.embedding is None or not rows:
            return {}, {}, None
        info = self.embedding.identity()
        fp, dimension = info['fingerprint'], info['dimension']
        cached, missing = {}, []
        with self.store.connection() as db:
            for item in rows:
                row = db.execute('SELECT * FROM expression_asset_vectors WHERE asset_id=? AND revision=? AND model_fingerprint=?',
                                 (item['id'], item['revision'], fp)).fetchone()
                try:
                    if not row or row['content_hash'] != item['content_hash']:raise ValueError()
                    cached[item['id']] = vector(json.loads(row['vector_json']), dimension)
                except (ValueError, TypeError, ExpressionAssetError):
                    missing.append(item)
        if missing:
            supplied = self.embedding.embed_documents([rendered(item) for item in missing])
            if len(supplied) != len(missing):raise ExpressionAssetError('expression_vector_invalid')
            for item, value in zip(missing, supplied):cached[item['id']] = vector(value, dimension)
        _, queries = self.embedding.embed_query([query])
        queries = [vector(value, dimension) for value in queries]
        if not queries:raise ExpressionAssetError('expression_vector_invalid')
        with self.store.connection(write=True) as db:
            for item in missing:
                # 撤回发生在本地推理期间也不能重新写入旧的派生向量。
                asset(db, item['persona_id'], item['scope'], item['id'], item['revision'])
                db.execute('INSERT OR REPLACE INTO expression_asset_vectors VALUES(?,?,?,?,?)',
                           (item['id'], item['revision'], fp, item['content_hash'], json.dumps(cached[item['id']])))
        scores = {key: max(cosine(v, query_vector) for query_vector in queries) for key, v in cached.items()}
        return scores, cached, fp

    def select(self, persona_id, selected_scope, query):
        if not isinstance(query, str) or len(query) > 12000:
            raise ExpressionAssetError('expression_query_invalid')
        scope_key = normalized_scope(persona_id, selected_scope)
        with self.store.connection() as db:
            current_policy = policy(db, persona_id)
            current_authority = authority(db, persona_id)
            rows = []
            if current_policy['data']['enabled'] and query.strip():
                for (ident,) in db.execute('''SELECT a.id FROM expression_assets a WHERE a.persona_id=? AND a.scope_key=?
                        AND a.state='active' AND EXISTS(SELECT 1 FROM expression_asset_supports s
                        WHERE s.asset_id=a.id AND s.asset_revision=a.revision) ORDER BY a.id''', (persona_id, scope_key)):
                    try:item = asset(db, persona_id, selected_scope, ident)
                    except ExpressionAssetError:continue
                    rows.append(item | {'persona_id': persona_id, 'scope': selected_scope})
        query_terms = matching_terms(query)
        explicit = {item['id']: {word for word in item['keywords'] if word not in QUERY_STOP and literal_match(word, query)} for item in rows}
        tokens = {item['id']: matching_terms(' '.join([item['situation'], item['style'], *item['keywords']])) | set(item['keywords']) - QUERY_STOP for item in rows}
        # 首版以情境／关键词命中形成小库候选，Embedding仅做排序，不用未校准分数激活资产。
        rows = [item for item in rows if query_terms & tokens[item['id']] or explicit[item['id']]]
        lexical = {item['id']: (len(query_terms & tokens[item['id']]) + len(explicit[item['id']])) / max(1, len(query_terms) + len(explicit[item['id']])) for item in rows}
        fallback = None
        try:semantic, vectors, model = self._semantic(query, rows)
        except (RuntimeError, ValueError, TypeError, KeyError, OSError) as error:
            semantic, vectors, model = {}, {}, None
            fallback = str(error) if isinstance(error, ExpressionAssetError) else 'local_embedding_unavailable'
        remaining = {item['id']: item for item in rows}
        chosen, total = [], 0
        while remaining and len(chosen) < current_policy['data']['max_candidates']:
            def score(ident):
                relevance = .7 * lexical[ident] + .3 * max(0, semantic.get(ident, 0))
                novelty = 0
                for previous in chosen:
                    old = previous['id']
                    similar = (max(0, cosine(vectors[ident], vectors[old])) if vectors else
                               len(tokens[ident] & tokens[old]) / max(1, len(tokens[ident] | tokens[old])))
                    novelty = max(novelty, similar)
                return .7 * relevance - .3 * novelty
            ident = sorted(remaining, key=lambda key: (-score(key), key))[0]
            item = remaining.pop(ident)
            size = len(rendered(item))
            if total + size > current_policy['data']['max_context_chars']:
                continue
            chosen.append(item)
            total += size
        items = [{key: item[key] for key in ('id', 'revision', 'situation', 'style', 'content_hash', 'supports')} for item in chosen]
        snapshot = {'version': 'expression-candidates-v1', 'id': identifier(), 'persona_id': persona_id,
                    'scope': selected_scope, 'policy_revision': current_policy['revision'],
                    'policy_hash': fingerprint(current_policy['data']), 'authority': current_authority,
                    'query_hash': fingerprint(query), 'items': items, 'characters': total,
                    'retrieval': 'lexical-local-mmr-v2', 'embedding_model': model, 'embedding_fallback': fallback}
        digest = fingerprint(snapshot)
        with self.store.connection(write=True) as db:
            self._validate(db, snapshot)
            db.execute('INSERT INTO expression_candidate_snapshots VALUES(?,?,?,?,?,?)',
                       (snapshot['id'], persona_id, scope_key, json.dumps(snapshot, ensure_ascii=False), digest, timestamp()))
        return snapshot | {'data_hash': digest}

    def _validate(self, db, snapshot):
        pid, selected = snapshot['persona_id'], snapshot['scope']
        current = policy(db, pid)
        if (current['revision'] != snapshot['policy_revision'] or fingerprint(current['data']) != snapshot['policy_hash']
                or authority(db, pid) != snapshot['authority']):
            raise ExpressionAssetError('expression_candidate_policy_changed')
        canonical = []
        for item in snapshot['items']:
            if not current['data']['enabled']:raise ExpressionAssetError('expression_assets_disabled')
            fresh = asset(db, pid, selected, item['id'], item['revision'])
            if fresh['content_hash'] != item['content_hash'] or fresh['supports'] != item['supports']:
                raise ExpressionAssetError('expression_candidate_source_changed')
            canonical.append(fresh)
        return canonical

    def validate(self, persona_id, selected_scope, snapshot_id, expected_hash=None):
        scope_key = normalized_scope(persona_id, selected_scope)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM expression_candidate_snapshots WHERE id=? AND persona_id=? AND scope_key=?',
                             (snapshot_id, persona_id, scope_key)).fetchone()
            if not row:raise ExpressionAssetError('expression_candidate_snapshot_missing')
            data = json.loads(row['data_json'])
            if data.get('id') != snapshot_id or data.get('persona_id') != persona_id or data.get('scope') != selected_scope:
                raise ExpressionAssetError('expression_candidate_snapshot_scope_changed')
            if fingerprint(data) != row['data_hash'] or (expected_hash is not None and expected_hash != row['data_hash']):
                raise ExpressionAssetError('expression_candidate_snapshot_changed')
            return self._validate(db, data)

    def scoped_texts(self, persona_id, selected_scope, snapshot_id, expected_hash=None):
        rows = self.validate(persona_id, selected_scope, snapshot_id, expected_hash)
        scope_key = normalized_scope(persona_id, selected_scope)
        return tuple(ScopedText(item['id'], str(item['revision']) + ':' + item['content_hash'], persona_id, scope_key,
                                'expression-snapshot:' + snapshot_id, rendered(item), text_hash(rendered(item))) for item in rows)
