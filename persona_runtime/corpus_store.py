"""Local-only corpus originals, frozen previews and idempotent file retention."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .corpus_files import CorpusError, digest, inspect_bytes, read_supplied
from .schema_corpus import APPLICATION_ID, SCHEMA, SCHEMA_VERSION, TABLES


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return digest(encoded(value).encode('utf-8'))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def field(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise CorpusError('corpus_invalid_' + name)
    return value


def outside_repository(path):
    path = Path(path)
    if not path.is_absolute():
        raise CorpusError('corpus_absolute_path_required')
    path = path.resolve()
    source = Path(__file__).resolve().parents[1]
    if path == Path(path.anchor) or path.is_relative_to(source):
        raise CorpusError('corpus_storage_outside_source_required')
    if any((parent / '.git').exists() for parent in (path, *path.parents)):
        raise CorpusError('corpus_storage_outside_source_required')
    return path


class CorpusStore:
    def __init__(self, root):
        self.root = outside_repository(root)
        self.path = self.root / 'corpus.sqlite3'

    def _inside(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root):
            raise CorpusError('corpus_storage_path_changed')
        return path

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return self.inspect()
        if any(self.root.iterdir()):
            raise CorpusError('corpus_empty_target_required')
        try:
            with self.path.open('xb'):
                pass
        except FileExistsError:
            raise CorpusError('corpus_initialization_in_progress') from None
        db = sqlite3.connect(self.path)
        complete = False
        try:
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
            db.execute('INSERT INTO corpus_meta VALUES(1,?,?,?)',
                       (str(uuid4()), digest(SCHEMA.encode('utf-8')), now()))
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            db.commit()
            complete = True
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
            if not complete:
                self.path.unlink(missing_ok=True)
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
            meta = db.execute('SELECT * FROM corpus_meta WHERE singleton=1').fetchone()
            if not meta or meta['schema_hash'] != digest(SCHEMA.encode('utf-8')):
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

    @staticmethod
    def _dataset(db, dataset_id, active=True):
        row = db.execute('SELECT * FROM corpus_datasets WHERE id=?', (dataset_id,)).fetchone()
        if not row:
            raise CorpusError('corpus_dataset_missing')
        if active and row['state'] != 'active':
            raise CorpusError('corpus_dataset_revoked')
        return dict(row)

    @staticmethod
    def _prior(db, operation_id, action, request):
        field(operation_id, 'operation_id')
        row = db.execute('SELECT * FROM corpus_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if row:
            if row['action'] != action or row['request_hash'] != fingerprint(request):
                raise CorpusError('corpus_operation_conflict')
            return json.loads(row['result_json'])

    @staticmethod
    def _save(db, operation_id, action, request, result):
        db.execute('INSERT INTO corpus_operations VALUES(?,?,?,?,?)',
                   (operation_id, action, fingerprint(request), encoded(result), now()))
        return result

    def create_dataset(self, label, operation_id):
        field(label, 'label')
        request = {'label': label, 'platform': 'wechat'}
        with self.connection(write=True) as db:
            previous = self._prior(db, operation_id, 'dataset_create', request)
            if previous is not None:
                return previous
            ident = str(uuid4())
            db.execute('INSERT INTO corpus_datasets VALUES(?,?,?,\'active\',1,1,0,0,?)',
                       (ident, 'wechat', label, now()))
            return self._save(db, operation_id, 'dataset_create', request, self._dataset(db, ident))

    def preview(self, dataset_id, source, operation_id, encoding=None):
        source = Path(source)
        if not source.is_absolute():
            raise CorpusError('corpus_source_absolute_path_required')
        # Keep the supplied suffix: a symlink must not change the reviewed file type.
        source = source.absolute()
        request = {'dataset_id': dataset_id, 'source': str(source), 'encoding': encoding}
        with self.connection() as db:
            previous = self._prior(db, operation_id, 'import_preview', request)
            if previous is not None:
                return previous
            dataset = self._dataset(db, dataset_id)
        raw = read_supplied(source)
        manifest = inspect_bytes(raw, source.suffix, encoding)
        manifest.update(dataset_id=dataset_id, dataset_revision=dataset['revision'], filename=source.name)
        with self.connection(write=True) as db:
            previous = self._prior(db, operation_id, 'import_preview', request)
            if previous is not None:
                return previous
            fresh = self._dataset(db, dataset_id)
            if fresh['revision'] != dataset['revision']:
                raise CorpusError('corpus_dataset_changed')
            ident = str(uuid4())
            preview_hash = fingerprint(manifest)
            db.execute('INSERT INTO corpus_previews VALUES(?,?,?,?,?,?,?)',
                       (ident, dataset_id, dataset['revision'], str(source), encoded(manifest), preview_hash, now()))
            result = {'id': ident, 'manifest_hash': preview_hash, 'manifest': manifest}
            return self._save(db, operation_id, 'import_preview', request, result)

    def _blob(self, sha256):
        if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
            raise CorpusError('corpus_file_hash_invalid')
        return self._inside('originals', sha256[:2], sha256 + '.bin')

    def _verify_blob(self, sha256, size):
        path = self._blob(sha256)
        hasher = hashlib.sha256()
        length = 0
        try:
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    length += len(block)
                    hasher.update(block)
        except OSError:
            raise CorpusError('corpus_original_unavailable') from None
        if length != size or hasher.hexdigest() != sha256:
            raise CorpusError('corpus_original_changed')

    def retain_original(self, preview_id, expected_hash, operation_id):
        request = {'preview_id': preview_id, 'expected_hash': expected_hash}
        with self.connection() as db:
            previous = self._prior(db, operation_id, 'import_commit', request)
            if previous is not None:
                return previous
            found = db.execute('SELECT * FROM corpus_previews WHERE id=?', (preview_id,)).fetchone()
            if not found:
                raise CorpusError('corpus_preview_missing')
            preview = dict(found)
            self._dataset(db, preview['dataset_id'])
        manifest = json.loads(preview['manifest_json'])
        if fingerprint(manifest) != preview['manifest_hash'] or expected_hash != preview['manifest_hash']:
            raise CorpusError('corpus_preview_changed')
        raw = read_supplied(Path(preview['source_path']))
        if digest(raw) != manifest['sha256'] or len(raw) != manifest['bytes']:
            raise CorpusError('corpus_source_changed')
        temporary = self._inside(str(uuid4()) + '.pending')
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            with self.connection(write=True) as db:
                previous = self._prior(db, operation_id, 'import_commit', request)
                if previous is not None:
                    return previous
                dataset = self._dataset(db, preview['dataset_id'])
                if dataset['revision'] != preview['dataset_revision']:
                    raise CorpusError('corpus_dataset_changed')
                # Serialize final placement with the import ledger. Replacing an
                # original another Windows reader has open can fail or race.
                target = self._blob(manifest['sha256'])
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.replace(temporary, target)
                self._verify_blob(manifest['sha256'], manifest['bytes'])
                db.execute('INSERT OR IGNORE INTO corpus_files VALUES(?,?,?)',
                           (manifest['sha256'], manifest['bytes'], now()))
                existing = db.execute('SELECT * FROM corpus_imports WHERE dataset_id=? AND file_hash=?',
                                      (preview['dataset_id'], manifest['sha256'])).fetchone()
                ident = existing['id'] if existing else str(uuid4())
                if not existing:
                    db.execute('INSERT INTO corpus_imports VALUES(?,?,?,\'awaiting_export_format\',?)',
                               (ident, preview['dataset_id'], manifest['sha256'], now()))
                db.execute('INSERT OR IGNORE INTO corpus_source_locations VALUES(?,?)', (preview_id, ident))
                result = {'id': ident, 'dataset_id': preview['dataset_id'], 'file_hash': manifest['sha256'],
                          'duplicate_original': existing is not None, 'state': 'awaiting_export_format',
                          'message_count': None, 'learning_eligible': False}
                return self._save(db, operation_id, 'import_commit', request, result)
        finally:
            temporary.unlink(missing_ok=True)

    def revoke_dataset(self, dataset_id, expected_revision, operation_id):
        if type(expected_revision) is not int or expected_revision < 1:
            raise CorpusError('corpus_revision_invalid')
        request = {'dataset_id': dataset_id, 'expected_revision': expected_revision}
        with self.connection(write=True) as db:
            previous = self._prior(db, operation_id, 'dataset_revoke', request)
            if previous is not None:
                return previous
            dataset = self._dataset(db, dataset_id, active=False)
            if dataset['revision'] != expected_revision:
                raise CorpusError('corpus_dataset_changed')
            db.execute('UPDATE corpus_datasets SET state=\'revoked\',revision=revision+1 WHERE id=?', (dataset_id,))
            return self._save(db, operation_id, 'dataset_revoke', request,
                              {'id': dataset_id, 'state': 'revoked', 'revision': expected_revision + 1,
                               'original_bytes_deleted': False})

    def inspect(self, dataset_id=None):
        with self.connection() as db:
            datasets = ([self._dataset(db, dataset_id, active=False)] if dataset_id else
                        [dict(row) for row in db.execute('SELECT * FROM corpus_datasets ORDER BY created_at,id')])
            for dataset in datasets:
                dataset['original_count'] = db.execute('SELECT count(*) FROM corpus_imports WHERE dataset_id=?', (dataset['id'],)).fetchone()[0]
            return {'schema': SCHEMA_VERSION, 'corpus_id': db.execute('SELECT corpus_id FROM corpus_meta').fetchone()[0],
                    'datasets': datasets, 'original_count': db.execute('SELECT count(*) FROM corpus_files').fetchone()[0],
                    'parser_ready': False, 'message_count': None, 'cloud_learning': False, 'expression_use': False}

    def verify_originals(self):
        with self.connection() as db:
            rows = list(db.execute('SELECT sha256,byte_count FROM corpus_files ORDER BY sha256'))
        for row in rows:
            self._verify_blob(row['sha256'], row['byte_count'])
        expected = {self._blob(row['sha256']) for row in rows}
        folder = self._inside('originals')
        files = {path for path in folder.rglob('*') if path.is_file()} if folder.exists() else set()
        files.update(path for path in self.root.glob('*.pending') if path.is_file())
        pending = {path for path in files if path.suffix == '.pending'}
        unindexed = files - expected - pending
        return {'originals_checked': len(rows), 'passed': not pending and not unindexed,
                'pending_files': len(pending), 'unindexed_files': len(unindexed), 'read_only': True}
