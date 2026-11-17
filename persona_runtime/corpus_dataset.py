"""Persist local dataset drafts; no prompt construction or cloud dispatch."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .corpus_files import CorpusError, digest
from .corpus_store import encoded, fingerprint, now
from .corpus_ingest import MoectCorpusStore
from .corpus_views import build_dataset, recipe_checked, validate_dataset
from .schema_corpus import APPLICATION_ID
from .schema_corpus_views import EXTENSION, SCHEMA_HASH_INPUT, SCHEMA_VERSION, TABLES


def execute_statements(db, script):
    pending = ''
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                db.execute(pending)
            pending = ''
    if pending.strip():
        raise CorpusError('corpus_view_migration_incomplete')


class DatasetCorpusStore(MoectCorpusStore):
    def initialize(self):
        prior = MoectCorpusStore(self.root)
        if not self.path.exists():
            prior.initialize()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 1:
            prior.initialize()
            version = 2
        if version == 2:
            with prior.connection():
                pass
            prior._identity_key()
            backup_dir = self._inside('backups')
            backup_dir.mkdir(exist_ok=True)
            backup = self._inside('backups', 'before-views-v3-' + str(uuid4()) + '.sqlite3')
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
            with prior.connection(write=True) as db:
                execute_statements(db, EXTENSION)
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

    def inspect(self, dataset_id=None):
        result = super().inspect(dataset_id)
        with self.connection() as db:
            count = db.execute('SELECT count(*) FROM corpus_dataset_versions').fetchone()[0]
        result.update(schema=SCHEMA_VERSION, dataset_versions=count, model_input_approved=False)
        return result

    def _source_proof(self, db, parse_id):
        parse = db.execute('SELECT * FROM corpus_parses WHERE id=?', (parse_id,)).fetchone()
        if not parse:
            raise CorpusError('corpus_parse_missing')
        dataset = self._dataset(db, parse['dataset_id'])
        if dataset['revision'] != parse['dataset_revision']:
            raise CorpusError('corpus_dataset_changed')
        grant = db.execute('SELECT * FROM corpus_material_grants WHERE id=?', (parse['grant_id'],)).fetchone()
        if (not grant or not grant['training_input_authorized'] or grant['speaker_scope'] != 'both'
                or grant['dataset_id'] != dataset['id']):
            raise CorpusError('corpus_material_permission_changed')
        receipt = json.loads(grant['receipt_json'])
        source_hashes = [db.execute('SELECT file_hash FROM corpus_imports WHERE id=?', (parse[name],)).fetchone()[0]
                         for name in ('json_import_id','md_import_id')]
        self._grant(encoded(receipt).encode('utf-8'), *source_hashes)
        if fingerprint([dataset['id'], grant['receipt_hash']]) != grant['id']:
            raise CorpusError('corpus_material_permission_changed')
        return {k: parse[k] for k in ('id','dataset_id','dataset_revision','grant_id','rows_hash',
                                      'relations_hash','identity_map_hash','implementation_hash')} | {
                   'grant_receipt_hash': grant['receipt_hash'],
                   'grant_payload_hash': fingerprint(receipt),
                   'identity_key_hash': db.execute('SELECT identity_key_hash FROM corpus_ingest_meta').fetchone()[0]}

    def _source_rows(self, db, parse_id):
        rows = [dict(r) for r in db.execute('SELECT * FROM corpus_messages WHERE parse_id=? ORDER BY ordinal', (parse_id,))]
        edges = [dict(r) for r in db.execute('SELECT * FROM corpus_relations WHERE parse_id=? ORDER BY source_id,kind', (parse_id,))]
        return rows, edges

    def build_version(self, parse_id, operation_id, changes=None):
        recipe = recipe_checked(changes)
        self.verify_parse(parse_id)
        from . import corpus_views
        implementation = digest(Path(corpus_views.__file__).read_bytes().replace(b'\r\n', b'\n'))
        with self.connection() as db:
            proof = self._source_proof(db, parse_id)
            request = {'parse_id': parse_id, 'source_proof_hash': fingerprint(proof),
                       'recipe_hash': fingerprint(recipe), 'implementation_hash': implementation}
            previous = self._prior(db, operation_id, 'dataset_view_build', request)
            if previous is not None:
                return self._view_status(db, previous['id'])
            rows, relations = self._source_rows(db, parse_id)
        try:
            identities = json.loads(self._inside(parse_id + '.identities.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise CorpusError('corpus_identity_map_unavailable') from None
        result = build_dataset(rows, relations, identities, key=self._identity_key(),
                               namespace=proof['dataset_id'], parse_id=parse_id, changes=recipe)
        with self.connection(write=True) as db:
            if self._source_proof(db, parse_id) != proof:
                raise CorpusError('corpus_view_source_changed')
            previous = self._prior(db, operation_id, 'dataset_view_build', request)
            if previous is not None:
                return self._view_status(db, previous['id'])
            existing = db.execute('SELECT id FROM corpus_dataset_versions WHERE request_hash=?', (fingerprint(request),)).fetchone()
            if existing:
                return self._save(db, operation_id, 'dataset_view_build', request, self._view_status(db, existing[0]))
            ident = str(uuid4())
            db.execute('INSERT INTO corpus_dataset_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)',
                       (ident, parse_id, proof['dataset_revision'], encoded(proof), fingerprint(proof),
                        encoded(recipe), fingerprint(recipe), implementation, fingerprint(request),
                        encoded(result['summary']), fingerprint(result['summary']), fingerprint(result), now()))
            db.executemany('INSERT INTO corpus_view_messages VALUES(?,?,?,?,?,?,?)',
                           ((ident, r['message_id'], r['ordinal'], r['split'], int(r['target_candidate']), r['text_hash'], encoded(r)) for r in result['messages']))
            db.executemany('INSERT INTO corpus_view_fragments VALUES(?,?,?,?,?,?)',
                           ((ident, r['id'], r['ordinal'], r['split'], r['group_id'], encoded(r)) for r in result['fragments']))
            db.executemany('INSERT INTO corpus_view_groups VALUES(?,?,?,?)',
                           ((ident, r['id'], r['split'], encoded(r)) for r in result['groups']))
            db.executemany('INSERT INTO corpus_view_edges VALUES(?,?,?)',
                           ((ident, n, encoded(r)) for n, r in enumerate(result['edges'])))
            return self._save(db, operation_id, 'dataset_view_build', request, self._view_status(db, ident))

    def _view_status(self, db, view_id):
        row = db.execute('SELECT * FROM corpus_dataset_versions WHERE id=?', (view_id,)).fetchone()
        if not row:
            raise CorpusError('corpus_view_missing')
        proof, recipe, summary = (json.loads(row[n]) for n in ('source_proof_json','recipe_json','summary_json'))
        if (fingerprint(proof) != row['source_proof_hash'] or fingerprint(recipe) != row['recipe_hash']
                or fingerprint(summary) != row['summary_hash']):
            raise CorpusError('corpus_view_manifest_changed')
        if self._source_proof(db, row['parse_id']) != proof:
            raise CorpusError('corpus_view_source_changed')
        return {'id': view_id, 'parse_id': row['parse_id'], 'source_proof_hash': row['source_proof_hash'],
                'payload_hash': row['payload_hash'], 'state': 'draft_pending_author_review',
                'model_input_approved': False, 'summary': summary}

    def view_status(self, view_id):
        with self.connection() as db:
            return self._view_status(db, view_id)

    def load_version(self, view_id):
        with self.connection() as db:
            self._view_status(db, view_id)
            parse_id = db.execute('SELECT parse_id FROM corpus_dataset_versions WHERE id=?', (view_id,)).fetchone()[0]
        self.verify_parse(parse_id)
        with self.connection() as db:
            self._view_status(db, view_id)
            row = db.execute('SELECT * FROM corpus_dataset_versions WHERE id=?', (view_id,)).fetchone()
            result = {'recipe': json.loads(row['recipe_json']), 'summary': json.loads(row['summary_json'])}
            for key, table, order in [('messages','corpus_view_messages','ordinal'), ('fragments','corpus_view_fragments','ordinal'),
                                      ('groups','corpus_view_groups',"json_extract(data_json,'$.first_epoch'),id"),
                                      ('edges','corpus_view_edges','ordinal')]:
                result[key] = [json.loads(r[0]) for r in db.execute(f'SELECT data_json FROM {table} WHERE view_id=? ORDER BY {order}', (view_id,))]
            if fingerprint(result) != row['payload_hash']:
                raise CorpusError('corpus_view_payload_changed')
            return result

    def verify_version(self, view_id):
        with self.connection() as db:
            self._view_status(db, view_id)
            view = db.execute('SELECT parse_id FROM corpus_dataset_versions WHERE id=?', (view_id,)).fetchone()
        result = self.load_version(view_id)
        with self.connection() as db:
            rows, relations = self._source_rows(db, view['parse_id'])
        checked = validate_dataset(result, rows, relations)
        return checked | {'view_id': view_id, 'model_input_approved': False, 'read_only': True}
