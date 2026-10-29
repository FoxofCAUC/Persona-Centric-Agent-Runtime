"""Disposable character vectors, explicitly prepared; never a canon authority."""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing

from .character import CharacterService, validate_character
from .character_embedding import (SemanticFailure, CHUNKING, DIMENSION, digest,
                                  safe_path, private_directory, numpy_module, checked_vectors)
from .instance import InstanceLock
from .models import LOCAL_USER_ID, RuntimeErrorBase

TEMPLATE_VERSION = 'character-source-v1'
CACHE_SCHEMA = 1


def sources(version):
    data = validate_character(version['data'])
    result = []
    for kind, rows in (('lore', data['lorebook']), ('example', data['examples'])):
        for row in sorted(rows, key=lambda r: r['id']):
            if not row['enabled'] or (kind == 'lore' and row['visibility'] != 'conversation'):
                continue
            if not row['text'].strip():
                continue
            if kind == 'lore':
                text = f"设定标题：{row['title']}\n类别：{row['category']}\n关键词：{'、'.join(row['keywords'])}\n正文：{row['text']}"
            else:
                text = f"表达场景：{row['scenario']}\n关键词：{'、'.join(row['keywords'])}\n示例：{row['text']}"
            result.append({'kind': kind, 'item_id': row['id'], 'source_hash': digest(row), 'text': text})
    return result


class CharacterSemanticIndex:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.root = store.paths.home / 'cache/character-semantic/v1'

    def _snapshot(self, persona_id, version_id):
        if not persona_id or not version_id:
            raise SemanticFailure('character_version_required')
        version = CharacterService(self.store).show(persona_id, version_id)
        if version['user_id'] != LOCAL_USER_ID:
            raise SemanticFailure('character_scope_mismatch')
        return version

    def _spec(self, version):
        return {'schema': CACHE_SCHEMA, 'user_id': version['user_id'],
                'persona_id': version['persona_id'], 'version_id': version['id'],
                'source_hash': digest(version['data']),
                'model': self.provider.identity()['fingerprint'],
                'template': TEMPLATE_VERSION, 'chunking': CHUNKING, 'dimension': DIMENSION}

    def _path(self, spec):
        safe_path(self.store.paths.home, self.root, self.store.path)
        return safe_path(self.store.paths.home, self.root / (digest(spec) + '.sqlite3'), self.store.path)

    def _read(self, spec, expected, *, path=None):
        np = numpy_module()
        path = self._path(spec) if path is None else safe_path(self.store.paths.home, path, self.store.path)
        if not path.is_file():
            raise SemanticFailure('character_cache_missing')
        if path.stat().st_size > 16 * 1024 * 1024:
            raise SemanticFailure('character_cache_invalid')
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
                meta = dict(conn.execute('SELECT key,value FROM meta'))
                if json.loads(meta['spec']) != spec:
                    raise ValueError()
                rows = conn.execute('SELECT metadata,vector FROM chunks ORDER BY ordinal').fetchall()
                checksum = hashlib.sha256()
                entries, vectors = [], []
                allowed = {(s['kind'], s['item_id']): s for s in expected}
                for raw, vector in rows:
                    entry = json.loads(raw)
                    source = allowed.get((entry['kind'], entry['item_id']))
                    if source is None or entry['source_hash'] != source['source_hash']:
                        raise ValueError()
                    if not 0 <= entry['start'] < entry['end'] <= len(source['text']):
                        raise ValueError()
                    value = np.frombuffer(vector, dtype='<f4')
                    if (value.shape != (DIMENSION,) or not np.isfinite(value).all()
                            or abs(float(np.linalg.norm(value)) - 1) > 1e-4):
                        raise ValueError()
                    checksum.update(raw.encode('utf-8')); checksum.update(vector)
                    entries.append(entry); vectors.append(value)
                if checksum.hexdigest() != meta['checksum'] or int(meta['count']) != len(rows):
                    raise ValueError()
                # A complete item cannot silently lose its final (or middle) chunk.
                for key, source in allowed.items():
                    spans = sorted((e['start'], e['end']) for e in entries if (e['kind'], e['item_id']) == key)
                    covered = 0
                    for start, end in spans:
                        if start > covered:
                            raise ValueError()
                        covered = max(covered, end)
                    if covered != len(source['text']):
                        raise ValueError()
                return entries, np.asarray(vectors, dtype=np.float32).reshape((-1, DIMENSION))
        except SemanticFailure:
            raise
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError):
            raise SemanticFailure('character_cache_invalid') from None

    def build(self, persona_id, version_id, *, rebuild=False):
        version = self._snapshot(persona_id, version_id)
        spec = self._spec(version)
        expected = sources(version)
        path = self._path(spec)
        private_directory(self.store.paths.home, self.root)
        lock_path = safe_path(self.store.paths.home, self.root / (digest(spec) + '.lock'), self.store.path)
        try:
            lock = InstanceLock(lock_path)
        except RuntimeErrorBase:
            raise SemanticFailure('character_cache_busy') from None
        temporary = self.root / (digest(spec) + '-' + uuid.uuid4().hex + '.tmp')
        try:
            if not rebuild and path.exists():
                entries, _ = self._read(spec, expected)
                return {'status': 'hit', 'chunks': len(entries), 'fingerprint': digest(spec)}
            entries, texts = [], []
            for source in expected:
                for index, chunk in enumerate(self.provider.chunks(source['text'])):
                    entries.append({k: source[k] for k in ('kind', 'item_id', 'source_hash')} |
                                   {k: chunk[k] for k in ('start', 'end', 'token_start', 'token_end')} |
                                   {'chunk_index': index})
                    texts.append(chunk['text'])
            vectors = self.provider.embed_documents(texts)
            if entries:
                vectors = checked_vectors(vectors, len(entries))
            # Re-read authority after expensive work, before publishing disposable data.
            if self._spec(self._snapshot(persona_id, version_id)) != spec:
                raise SemanticFailure('character_source_changed')
            self._publish(temporary, spec, entries, vectors)
            self._read(spec, expected, path=temporary)
            safe_path(self.store.paths.home, path, self.store.path)
            os.replace(temporary, path)
            self._read(spec, expected)
            return {'status': 'built', 'chunks': len(entries), 'fingerprint': digest(spec)}
        finally:
            try:
                if temporary.exists():
                    safe_path(self.store.paths.home, temporary, self.store.path).unlink()
            finally:
                lock.close()

    def _publish(self, path, spec, entries, vectors):
        safe_path(self.store.paths.home, path, self.store.path)
        checksum = hashlib.sha256()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            conn.execute('CREATE TABLE chunks(ordinal INTEGER PRIMARY KEY,metadata TEXT NOT NULL,vector BLOB NOT NULL)')
            for index, (entry, value) in enumerate(zip(entries, vectors, strict=True)):
                raw = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                vector = value.astype('<f4').tobytes()
                checksum.update(raw.encode('utf-8')); checksum.update(vector)
                conn.execute('INSERT INTO chunks VALUES (?,?,?)', (index, raw, vector))
            conn.executemany('INSERT INTO meta VALUES (?,?)', [
                ('spec', json.dumps(spec, sort_keys=True)), ('count', str(len(entries))),
                ('checksum', checksum.hexdigest())])

    def invalidate(self, persona_id, version_id):
        spec = self._spec(self._snapshot(persona_id, version_id))
        path = self._path(spec)
        if not path.exists():
            return {'status': 'missing'}
        try:
            lock = InstanceLock(safe_path(self.store.paths.home, self.root / (digest(spec) + '.lock'), self.store.path))
        except RuntimeErrorBase:
            raise SemanticFailure('character_cache_busy') from None
        try:
            safe_path(self.store.paths.home, path, self.store.path).unlink(missing_ok=True)
        finally:
            lock.close()
        return {'status': 'invalidated'}

    def query(self, persona_id, version_id, members, *, limit=5, per_kind_limit=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SemanticFailure('character_invalid_limit')
        if per_kind_limit is not None and (type(per_kind_limit) is not int or not 1 <= per_kind_limit <= 10):
            raise SemanticFailure('character_invalid_limit')
        version = self._snapshot(persona_id, version_id)
        spec = self._spec(version)
        entries, vectors = self._read(spec, sources(version))
        query_parts, query_vectors = self.provider.embed_query(members)
        # No latest-head lookup: legal old frozen versions remain valid.
        if self._spec(self._snapshot(persona_id, version_id)) != spec:
            raise SemanticFailure('character_source_changed')
        scores = query_vectors @ vectors.T
        best = {}
        for index, entry in enumerate(entries):
            query_index = int(scores[:, index].argmax())
            row = {k: entry[k] for k in ('kind', 'item_id', 'chunk_index', 'source_hash')}
            row.update(score=float(scores[query_index, index]), member_index=query_parts[query_index]['member_index'])
            key = (entry['kind'], entry['item_id'])
            if key not in best or row['score'] > best[key]['score']:
                best[key] = row
        ordered = sorted(best.values(), key=lambda r: (-r['score'], r['kind'], r['item_id']))
        result = {'candidates': ordered[:limit],
                'model_fingerprint': spec['model'], 'index_fingerprint': digest(spec),
                'version_id': version_id, 'origin_type': 'creator_setting',
                'query_chunks': [{k: c[k] for k in ('member_index', 'start', 'end')} for c in query_parts],
                'activation_authorized': False}
        if per_kind_limit is not None:
            result['by_kind'] = {kind: [r for r in ordered if r['kind'] == kind][:per_kind_limit]
                                 for kind in ('lore', 'example')}
        return result
