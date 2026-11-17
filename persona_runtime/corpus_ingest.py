"""Local private-chat import; data permission never enables an LLM dispatch."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .corpus_files import CorpusError, MAX_FILE_BYTES, digest
from .corpus_store import CorpusStore, encoded, fingerprint, now
from .corpus_moect import PARSER_VERSION, parse_private_export
from .schema_corpus import APPLICATION_ID
from .schema_corpus_moect import EXTENSION, SCHEMA_HASH_INPUT, SCHEMA_VERSION, TABLES


class MoectCorpusStore(CorpusStore):
    def initialize(self):
        base = CorpusStore(self.root)
        if not self.path.exists():
            base.initialize()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 1:
            with base.connection():
                pass
            key_path = self._inside('identity.key')
            if not key_path.exists():
                try:
                    with key_path.open('xb') as stream:
                        stream.write(os.urandom(32))
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    pass
            key = key_path.read_bytes()
            if len(key) != 32:
                raise CorpusError('corpus_identity_key_invalid')
            with base.connection(write=True) as db:
                for statement in EXTENSION.split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute('INSERT INTO corpus_ingest_meta VALUES(1,?)', (digest(key),))
                db.execute('UPDATE corpus_meta SET schema_hash=? WHERE singleton=1', (digest(SCHEMA_HASH_INPUT.encode('utf-8')),))
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        elif version != SCHEMA_VERSION:
            raise CorpusError('corpus_database_incompatible')
        self._identity_key()
        return self.inspect()

    @contextmanager
    def connection(self, write=False):
        path = self._inside('corpus.sqlite3')
        if not path.is_file():
            raise CorpusError('corpus_not_initialized')
        db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('BEGIN IMMEDIATE')
            if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise CorpusError('corpus_database_incompatible')
            present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES) <= present:
                raise CorpusError('corpus_schema_incomplete')
            meta = db.execute('SELECT schema_hash FROM corpus_meta WHERE singleton=1').fetchone()
            if not meta or meta[0] != digest(SCHEMA_HASH_INPUT.encode('utf-8')):
                raise CorpusError('corpus_schema_incompatible')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _identity_key(self):
        try:
            key = self._inside('identity.key').read_bytes()
        except OSError:
            raise CorpusError('corpus_identity_key_unavailable') from None
        with self.connection() as db:
            expected = db.execute('SELECT identity_key_hash FROM corpus_ingest_meta WHERE singleton=1').fetchone()
        if len(key) != 32 or not expected or expected[0] != digest(key):
            raise CorpusError('corpus_identity_key_changed')
        return key

    @contextmanager
    def _identity_cleanup(self):
        created = []
        try:
            yield created
        finally:
            # This runs after the enclosing database commit/rollback. Delete
            # only maps created here and proven absent from the parse ledger.
            if created:
                with self.connection() as db:
                    for ident, path in created:
                        if not db.execute('SELECT 1 FROM corpus_parses WHERE id=?', (ident,)).fetchone():
                            path.unlink(missing_ok=True)

    def inspect(self, dataset_id=None):
        value = super().inspect(dataset_id)
        with self.connection() as db:
            selected = ' WHERE dataset_id=?' if dataset_id else ''
            args = (dataset_id,) if dataset_id else ()
            parses = db.execute('SELECT count(*) FROM corpus_parses' + selected, args).fetchone()[0]
            total = db.execute('SELECT count(*) FROM corpus_messages WHERE parse_id IN (SELECT id FROM corpus_parses' + selected + ')', args).fetchone()[0]
        value.update(schema=SCHEMA_VERSION, parser_ready=True, parser=PARSER_VERSION,
                     parsed_batches=parses, message_count=total, model_input_approved=False)
        return value

    def _import_bytes(self, db, dataset_id, import_id, format_hint):
        row = db.execute('SELECT * FROM corpus_imports WHERE id=? AND dataset_id=?', (import_id, dataset_id)).fetchone()
        if not row:
            raise CorpusError('corpus_import_scope_invalid')
        manifests = [json.loads(r[0]) for r in db.execute('''SELECT p.manifest_json FROM corpus_previews p
            JOIN corpus_source_locations s ON p.id=s.preview_id WHERE s.import_id=?''', (import_id,))]
        if not any(m.get('format_hint') == format_hint for m in manifests):
            raise CorpusError('corpus_import_format_invalid')
        path = self._blob(row['file_hash'])
        try:
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise CorpusError('corpus_original_unavailable') from None
        if len(raw) > MAX_FILE_BYTES or digest(raw) != row['file_hash']:
            raise CorpusError('corpus_original_changed')
        return raw

    @staticmethod
    def _grant(raw, json_hash, md_hash):
        try:
            grant = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise CorpusError('corpus_author_receipt_invalid') from None
        if (not isinstance(grant, dict) or grant.get('version') != 'author-wechat-material-grant-v1'
                or grant.get('conversation_kind') != 'private'
                or grant.get('training_input_authorized') is not True
                or grant.get('speaker_scope') != 'both'
                or grant.get('speaker_confirmation') != 'author_confirmed_both_as_human_expression_sources'
                or grant.get('model_request_version_approved') is not False):
            raise CorpusError('corpus_author_scope_unconfirmed')
        sources = grant.get('sources')
        if not isinstance(sources, list) or len(sources) != 2 or any(not isinstance(v, dict) for v in sources):
            raise CorpusError('corpus_author_files_unconfirmed')
        if {(v.get('kind'), v.get('sha256')) for v in sources} != {('json', json_hash), ('md', md_hash)}:
            raise CorpusError('corpus_author_files_unconfirmed')
        return grant

    def parse_pair(self, dataset_id, json_import_id, md_import_id, grant_path, operation_id):
        grant_path = Path(grant_path)
        if not grant_path.is_absolute():
            raise CorpusError('corpus_author_receipt_path_required')
        try:
            with grant_path.open('rb') as stream:
                receipt_raw = stream.read(65537)
        except OSError:
            raise CorpusError('corpus_author_receipt_unavailable') from None
        if len(receipt_raw) > 65536:
            raise CorpusError('corpus_author_receipt_capacity')
        from . import corpus_moect
        code_hash = digest(Path(corpus_moect.__file__).read_bytes().replace(b'\r\n', b'\n'))
        request = {'dataset_id': dataset_id, 'json_import_id': json_import_id, 'md_import_id': md_import_id,
                   'receipt_hash': digest(receipt_raw), 'parser': PARSER_VERSION, 'implementation_hash': code_hash}
        with self.connection() as db:
            previous = self._prior(db, operation_id, 'moect_parse', request)
            if previous is not None:
                return self._parse_status(db, previous['id'])
            dataset = self._dataset(db, dataset_id)
            json_raw = self._import_bytes(db, dataset_id, json_import_id, 'json')
            md_raw = self._import_bytes(db, dataset_id, md_import_id, 'markdown')
        grant = self._grant(receipt_raw, digest(json_raw), digest(md_raw))
        key = self._identity_key()
        parsed = parse_private_export(json_raw, md_raw, identity_key=key, dataset_id=dataset_id)
        grant_id = fingerprint([dataset_id, digest(receipt_raw)])
        rows_hash = fingerprint(parsed['rows'])
        with self._identity_cleanup() as identity_files, self.connection(write=True) as db:
            previous = self._prior(db, operation_id, 'moect_parse', request)
            if previous is not None:
                return self._parse_status(db, previous['id'])
            fresh = self._dataset(db, dataset_id)
            if fresh['revision'] != dataset['revision']:
                raise CorpusError('corpus_dataset_changed')
            if db.execute('SELECT identity_key_hash FROM corpus_ingest_meta').fetchone()[0] != digest(key):
                raise CorpusError('corpus_identity_key_changed')
            db.execute('INSERT OR IGNORE INTO corpus_material_grants VALUES(?,?,?,?,\'both\',1,0,?)',
                       (grant_id, dataset_id, digest(receipt_raw), encoded(grant), now()))
            existing = db.execute('''SELECT id FROM corpus_parses WHERE dataset_id=? AND json_import_id=?
                AND md_import_id=? AND grant_id=? AND parser_version=? AND implementation_hash=?''',
                (dataset_id, json_import_id, md_import_id, grant_id, PARSER_VERSION, code_hash)).fetchone()
            if existing:
                result = self._parse_status(db, existing[0])
                return self._save(db, operation_id, 'moect_parse', request, result)
            ident = str(uuid4())
            identity_raw = encoded(parsed['identity']).encode('utf-8')
            identity_path = self._inside(ident + '.identities.json')
            with identity_path.open('xb') as stream:
                identity_files.append((ident, identity_path))
                stream.write(identity_raw)
                stream.flush()
                os.fsync(stream.fileno())
            db.execute('INSERT INTO corpus_parses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, dataset_id, dataset['revision'], json_import_id, md_import_id, grant_id,
                        PARSER_VERSION, code_hash, encoded(parsed['summary']), fingerprint(parsed['summary']),
                        rows_hash, fingerprint(sorted(parsed['relations'], key=lambda r: (r['source_id'], r['kind']))),
                        digest(identity_raw), now()))
            fields = ('id','ordinal','platform_message_id','source_locator','local_id','shard','speaker_token',
                      'role','kind','created_epoch','time_str','text','text_hash','record_hash','target_candidate',
                      'exclusion_code','md_text_start','md_text_end')
            db.executemany('INSERT INTO corpus_messages VALUES(' + ','.join('?' for _ in range(19)) + ')',
                           ((ident, *(row[name] for name in fields)) for row in parsed['rows']))
            db.executemany('INSERT INTO corpus_relations VALUES(?,?,?,?,?,?,?)',
                           ((ident, row['source_id'], row['kind'], row['target_id'], row['external_message_id'],
                             row['state'], row['evidence_locator']) for row in parsed['relations']))
            result = self._parse_status(db, ident)
            return self._save(db, operation_id, 'moect_parse', request, result)

    def _parse_status(self, db, parse_id):
        row = db.execute('SELECT * FROM corpus_parses WHERE id=?', (parse_id,)).fetchone()
        if not row:
            raise CorpusError('corpus_parse_missing')
        summary = json.loads(row['summary_json'])
        if fingerprint(summary) != row['summary_hash']:
            raise CorpusError('corpus_parse_summary_changed')
        dataset = self._dataset(db, row['dataset_id'], active=False)
        return {'id': parse_id, 'dataset_id': row['dataset_id'], 'dataset_state': dataset['state'],
                'training_input_authorized': dataset['state'] == 'active' and dataset['revision'] == row['dataset_revision'],
                'speaker_scope': 'both',
                'model_input_approved': False, 'learning_eligible': False, 'summary': summary}

    def parse_status(self, parse_id):
        with self.connection() as db:
            return self._parse_status(db, parse_id)

    def verify_parse(self, parse_id):
        with self.connection() as db:
            record = db.execute('SELECT * FROM corpus_parses WHERE id=?', (parse_id,)).fetchone()
            if not record:
                raise CorpusError('corpus_parse_missing')
            self._parse_status(db, parse_id)
            dataset = self._dataset(db, record['dataset_id'])
            rows = [dict(r) for r in db.execute('SELECT * FROM corpus_messages WHERE parse_id=? ORDER BY ordinal', (parse_id,))]
            for row in rows:
                row.pop('parse_id')
                row['target_candidate'] = bool(row['target_candidate'])
            if any(row['ordinal'] != n or digest(row['text'].encode('utf-8')) != row['text_hash'] for n, row in enumerate(rows)):
                raise CorpusError('corpus_message_evidence_changed')
            if fingerprint(rows) != record['rows_hash']:
                raise CorpusError('corpus_message_evidence_changed')
            relations = [dict(r) for r in db.execute('SELECT * FROM corpus_relations WHERE parse_id=? ORDER BY source_id,kind', (parse_id,))]
            for relation in relations:
                relation.pop('parse_id')
            if fingerprint(relations) != record['relations_hash']:
                raise CorpusError('corpus_relation_evidence_changed')
            self._import_bytes(db, record['dataset_id'], record['json_import_id'], 'json')
            self._import_bytes(db, record['dataset_id'], record['md_import_id'], 'markdown')
            try:
                identity = self._inside(parse_id + '.identities.json').read_bytes()
            except OSError:
                raise CorpusError('corpus_identity_map_unavailable') from None
            if digest(identity) != record['identity_map_hash']:
                raise CorpusError('corpus_identity_map_changed')
            if dataset['revision'] != record['dataset_revision']:
                raise CorpusError('corpus_dataset_changed')
        self._identity_key()
        return {'passed': True, 'records_checked': len(rows), 'learning_eligible': False, 'read_only': True}
