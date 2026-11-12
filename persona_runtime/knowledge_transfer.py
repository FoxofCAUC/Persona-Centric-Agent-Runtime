"""Portable public evidence and additive recovery under current authorization."""

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from .archive import fingerprint
from .config import atomic_json
from .knowledge_service import REGISTRY, read
from .models import RuntimeErrorBase


TABLES = ('knowledge_pages', 'knowledge_documents', 'knowledge_targets',
          'knowledge_batches', 'knowledge_claims', 'knowledge_evidence')


def snapshot(db, persona_id=None):
    """Execution leases, model snapshots and live grants are excluded."""
    available = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pages = None
    if persona_id:
        pages = {r[0] for r in db.execute('SELECT d.page_id FROM knowledge_batches b '
            'JOIN knowledge_documents d ON d.id=b.document_id WHERE b.owner_persona=?', (persona_id,))}
    data = {}
    for table in TABLES:
        rows = [dict(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] if table in available else []
        if table == 'knowledge_targets':
            rows = [r for r in rows if r['page_id']]
        if pages is not None:
            if table == 'knowledge_pages':
                rows = [r for r in rows if r['id'] in pages]
            elif table in ('knowledge_documents', 'knowledge_targets', 'knowledge_claims'):
                rows = [r for r in rows if r['page_id'] in pages]
            elif table == 'knowledge_batches':
                rows = [r for r in rows if r['owner_persona'] == persona_id]
            else:
                claim_ids = {r['id'] for r in data['knowledge_claims']}
                rows = [r for r in rows if r['claim_id'] in claim_ids]
        if table == 'knowledge_claims' and persona_id:
            batch_ids = {r['id'] for r in data['knowledge_batches']}
            rows = [r for r in rows if r['batch_id'] in batch_ids]
        if table == 'knowledge_batches':
            for row in rows:
                row['extract_task_id'] = row['review_task_id'] = None
        data[table] = rows
    registry = read(db, 'registry', REGISTRY)['data'] if 'knowledge_control' in available else REGISTRY
    web={}
    if 'knowledge_control' in available:
        from .knowledge_web import snapshot as web_snapshot
        web=web_snapshot(db,persona_id)
    return {**({'web':web} if web.get('documents') else {}),'version': 1, 'tables': data,
            'required_packages': [{'sha256': key, 'manifest': record['manifest']}
                for key, record in registry['packages'].items()],
            'invalidations': read(db, 'invalidations', {})['data'] if 'knowledge_control' in available else {}}


def validate(data):
    if isinstance(data,dict) and 'web' in data:
        from .knowledge_web import validate_snapshot
        validate_snapshot(data['web'])
    try:
        if data['version'] != 1 or set(data['tables']) != set(TABLES):
            raise ValueError()
        tables = data['tables']
        for table in TABLES:
            if not isinstance(tables[table], list):
                raise ValueError()
        pages = {r['id']: r for r in tables['knowledge_pages']}
        documents = {r['id']: r for r in tables['knowledge_documents']}
        batches = {r['id']: r for r in tables['knowledge_batches']}
        claims = {r['id']: r for r in tables['knowledge_claims']}
        for table, rows in ((name, tables[name]) for name in TABLES):
            identity = 'claim_id' if table == 'knowledge_evidence' else 'id'
            if len({r[identity] for r in rows}) != len(rows):
                raise ValueError()
        from .knowledge_fetch import public_identifier
        from .knowledge_output import date
        for page in pages.values():
            if (page['source_id'] not in ('wikipedia_zh', 'wikidata')
                    or page['id'] != fingerprint([page['source_id'], page['upstream_id']])
                    or page['state'] not in ('active', 'withdrawn')
                    or type(page['latest_revision']) is not int or page['latest_revision'] <= 0):
                raise ValueError()
            head = documents.get(page['head_document_id'])
            if head and (head['page_id'] != page['id'] or head['upstream_revision'] != page['latest_revision']):
                raise ValueError()
            if page['head_document_id'] and not head:
                raise ValueError()
        for doc in documents.values():
            if (doc['page_id'] not in pages or type(doc['upstream_revision']) is not int
                    or doc['upstream_revision'] <= 0 or not isinstance(doc['body'], str)
                    or hashlib.sha256(doc['body'].encode()).hexdigest() != doc['sha256']):
                raise ValueError()
            json.loads(doc['raw_json'])
            json.loads(doc['metadata_json'])
        for target in tables['knowledge_targets']:
            if (target['page_id'] not in pages
                    or target['source_id'] != pages[target['page_id']]['source_id']
                    or target['id'] != fingerprint([target['source_id'], target['public_id']])):
                raise ValueError()
            public_identifier(target['source_id'], target['public_id'])
            bindings = json.loads(target['bindings_json'])
            if not isinstance(bindings, list):
                raise ValueError()
            for binding in bindings:
                digest = binding['package_sha256']
                if (set(binding) != {'package_sha256', 'article_id'} or not isinstance(digest, str)
                        or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                        or type(binding['article_id']) is not int or binding['article_id'] <= 0):
                    raise ValueError()
        for batch in batches.values():
            if batch['document_id'] not in documents:
                raise ValueError()
            json.loads(batch['data_json'])
        evidence = {r['claim_id']: r for r in tables['knowledge_evidence']}
        from .knowledge_store import key, instant
        for claim in claims.values():
            doc = documents[claim['document_id']]
            item = evidence[claim['id']]
            if (claim['page_id'] != doc['page_id']
                    or batches[claim['batch_id']]['document_id'] != doc['id']
                    or item['document_id'] != doc['id']
                    or claim['kind'] not in ('fact', 'source_claim', 'commentary', 'unresolved')
                    or claim['state'] not in ('active', 'quarantined')
                    or claim['claim_key'] != key(claim['subject'], claim['predicate'])
                    or type(item['start_character']) is not int or type(item['end_character']) is not int
                    or not 0 <= item['start_character'] < item['end_character'] <= len(doc['body'])
                    or doc['body'][item['start_character']:item['end_character']] != item['quote']):
                raise ValueError()
            for name in ('valid_from', 'valid_until'):
                if claim[name] is not None:
                    date(claim[name])
            if claim['valid_from'] and claim['valid_until'] and instant(claim['valid_from']) >= instant(claim['valid_until']):
                raise ValueError()
        if set(evidence) != set(claims):
            raise ValueError()
        for identity, entries in data['invalidations'].items():
            if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                raise ValueError()
            for article, record in entries.items():
                if (not article.isdigit() or record['state'] not in ('withdrawn', 'corrected', 'expired', 'conflict')
                        or not isinstance(record['reason'], str)):
                    raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeErrorBase('knowledge_transfer_invalid') from None


def merge_snapshot(db, data, *, imported_persona=None):
    """Existing evidence, policies and execution records win over recovered data."""
    validate(data)
    if 'web' in data:
        from .knowledge_web import merge_snapshot as merge_web
        merge_web(db,data['web'],imported_persona)
    tables = data['tables']
    mapping = {}
    def insert(table, row):
        columns = [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')]
        db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(columns) + ') VALUES('
                   + ','.join('?' for _ in columns) + ')', [row[name] for name in columns])
    db.execute('PRAGMA defer_foreign_keys=ON')
    for page in tables['knowledge_pages']:
        insert('knowledge_pages', page | {'head_document_id': None})
    for doc in tables['knowledge_documents']:
        prior = db.execute('SELECT * FROM knowledge_documents WHERE page_id=? AND upstream_revision=?',
                           (doc['page_id'], doc['upstream_revision'])).fetchone()
        if prior and prior['sha256'] != doc['sha256']:
            raise RuntimeErrorBase('knowledge_revision_content_mismatch')
        mapped = prior['id'] if prior else doc['id']
        collision = db.execute('SELECT * FROM knowledge_documents WHERE id=?', (mapped,)).fetchone()
        if collision and (collision['page_id'], collision['upstream_revision'], collision['sha256']) != (doc['page_id'], doc['upstream_revision'], doc['sha256']):
            raise RuntimeErrorBase('knowledge_transfer_identity_conflict')
        mapping[doc['id']] = mapped
        insert('knowledge_documents', doc | {'id': mapped})
    batch_ids = {}
    for batch in tables['knowledge_batches']:
        owner = imported_persona or batch['owner_persona']
        if not db.execute('SELECT 1 FROM personas WHERE id=?', (owner,)).fetchone():
            continue
        batch_id = fingerprint(['knowledge-import', owner, batch['id']]) if imported_persona else batch['id']
        work_key = fingerprint(['knowledge-import', owner, batch['work_key']]) if imported_persona else batch['work_key']
        prior = db.execute('SELECT id FROM knowledge_batches WHERE work_key=?', (work_key,)).fetchone()
        batch_id = prior['id'] if prior else batch_id
        batch_ids[batch['id']] = batch_id
        insert('knowledge_batches', batch | {'id': batch_id, 'owner_persona': owner,
            'document_id': mapping[batch['document_id']], 'work_key': work_key,
            'state': ('import_review_required' if imported_persona else 'restored_complete'
                      if batch['state'] in ('committed', 'restored_complete') else 'restore_review_required'),
            'extract_task_id': None, 'review_task_id': None})
    claim_ids = {r['id']: fingerprint(['knowledge-import', imported_persona, r['id']])
                 if imported_persona else r['id'] for r in tables['knowledge_claims'] if r['batch_id'] in batch_ids}
    for claim in tables['knowledge_claims']:
        if claim['id'] not in claim_ids:
            continue
        insert('knowledge_claims', claim | {'id': claim_ids[claim['id']],
            'document_id': mapping[claim['document_id']], 'batch_id': batch_ids[claim['batch_id']],
            'previous_id': claim_ids.get(claim['previous_id']),
            'state': 'quarantined' if imported_persona else claim['state']})
    for item in tables['knowledge_evidence']:
        if item['claim_id'] in claim_ids:
            insert('knowledge_evidence', item | {'claim_id': claim_ids[item['claim_id']],
                'document_id': mapping[item['document_id']]})
    for page in tables['knowledge_pages']:
        current = db.execute('SELECT * FROM knowledge_pages WHERE id=?', (page['id'],)).fetchone()
        if current['state']!='withdrawn' and ((not current['head_document_id'] and page['latest_revision']>=current['latest_revision'])
                or not imported_persona and page['latest_revision']>current['latest_revision']):
            db.execute('UPDATE knowledge_pages SET title=?,url=?,head_document_id=?,latest_revision=?,state=?,last_checked=? WHERE id=?',
                (page['title'], page['url'], mapping.get(page['head_document_id']), page['latest_revision'], page['state'], page['last_checked'], page['id']))
    for target in tables['knowledge_targets']:
        insert('knowledge_targets', target | {'last_checked': None,
            'bindings_json': '[]' if imported_persona else target['bindings_json']})
    invalid = read(db, 'invalidations', {})
    merged = copy.deepcopy(data['invalidations'])
    for identity, records in invalid['data'].items():
        merged.setdefault(identity, {}).update(records)
    if merged != invalid['data']:
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('knowledge.invalidations', json.dumps({'revision': invalid['revision'] + 1, 'data': merged})))
    return {'documents_seen': len(mapping), 'claims_seen': len(claim_ids),
            'review_required': bool(imported_persona), 'permissions_imported': False, 'execution_restored': False}


def backup_manifest(path):
    """The large base package is represented by immutable references."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_control'").fetchone():
            return
        registry = read(db, 'registry', REGISTRY)['data']
        packages = [{'sha256': key, 'data_revision': row['manifest'].get('data_revision')}
                    for key, row in registry['packages'].items()]
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    atomic_json(path.with_suffix(path.suffix + '.knowledge.json'),
                {'version': 1, 'database_sha256': digest, 'required_packages': packages})


def restore(service, name, expected_revision, operation_id):
    paths = service.paths
    if not isinstance(name, str) or ':' in name or not name.endswith('.sqlite3'):
        raise RuntimeErrorBase('knowledge_backup_name_invalid')
    relative = Path(name)
    if (relative.is_absolute() or '..' in relative.parts or len(relative.parts) not in (1, 2)
            or len(relative.parts) == 2 and relative.parts[0] != 'workbench-automatic'):
        raise RuntimeErrorBase('knowledge_backup_name_invalid')
    from .character_embedding import safe_path
    root = paths.home / 'backups'
    path = safe_path(root, root / name)
    try:
        metadata_path = path.with_suffix(path.suffix + '.knowledge.json')
        metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else None
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if metadata is not None and (digest != metadata['database_sha256'] or metadata['version'] != 1):
            raise ValueError()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
            source.row_factory = sqlite3.Row
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError()
            if source.execute('PRAGMA user_version').fetchone()[0] > 26:
                raise ValueError()
            data = snapshot(source)
        validate(data)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError()
    except (OSError, ValueError, KeyError, sqlite3.Error):
        raise RuntimeErrorBase('knowledge_backup_invalid') from None
    result = {'backup': name, 'database_sha256': digest,
              'manifest_state': 'verified' if metadata is not None else 'legacy_observed',
              'required_packages': data['required_packages']}
    def apply(db):
        result.update(merge_snapshot(db, data))
    return service._update('restore', result, expected_revision, operation_id,
                           request=[name, digest], apply=apply)
