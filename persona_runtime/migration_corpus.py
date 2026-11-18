"""显式选择整个独立语料库，按索引携带原件及本地说话人映射。"""
from contextlib import closing
import hashlib
from pathlib import PurePosixPath
import re
import shutil
import sqlite3
from uuid import UUID

from .corpus_dataset import DatasetCorpusStore
from .models import RuntimeErrorBase
from .private_personas import safe


PREFIX = ('private', 'corpora')


def canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def member_allowed(name):
    parts = PurePosixPath(name).parts
    if len(parts) < 4 or parts[:2] != PREFIX or not canonical_uuid(parts[2]):
        return False
    if len(parts) == 4:
        filename = parts[3]
        return (filename in ('corpus.sqlite3', 'identity.key') or
                filename.endswith('.identities.json') and canonical_uuid(filename.removesuffix('.identities.json')))
    if len(parts) == 6 and parts[3] == 'originals':
        sha = parts[5].removesuffix('.bin')
        return parts[5] == sha + '.bin' and re.fullmatch('[a-f0-9]{64}', sha) and parts[4] == sha[:2]
    return False


def indexed_files(store):
    with store.connection() as db:
        ident = db.execute('SELECT corpus_id FROM corpus_meta WHERE singleton=1').fetchone()[0]
        if not canonical_uuid(ident):
            raise RuntimeErrorBase('migration_corpus_id_invalid')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeErrorBase('migration_corpus_integrity_invalid')
        key_hash = db.execute('SELECT identity_key_hash FROM corpus_ingest_meta WHERE singleton=1').fetchone()[0]
        files = {'identity.key': (key_hash, 32)}
        for parse_id, digest in db.execute('SELECT id,identity_map_hash FROM corpus_parses'):
            if not canonical_uuid(parse_id):
                raise RuntimeErrorBase('migration_corpus_id_invalid')
            files[parse_id + '.identities.json'] = (digest, None)
        for digest, size in db.execute('SELECT sha256,byte_count FROM corpus_files'):
            relative = store._blob(digest).relative_to(store.root).as_posix()
            files[relative] = (digest, size)
    return ident, files


def verify_file(path, expected):
    digest, size = expected
    if not path.is_file() or (size is not None and path.stat().st_size != size):
        raise RuntimeErrorBase('migration_corpus_evidence_invalid')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise RuntimeErrorBase('migration_corpus_evidence_invalid')


def stage_selected(folders, stage_root):
    entries = {}
    for index, folder in enumerate(folders):
        source = DatasetCorpusStore(safe(folder))
        staging = DatasetCorpusStore(stage_root / ('corpus-' + str(index)))
        staging.root.mkdir()
        safe(source.path, source.root)
        with source.connection() as src, closing(sqlite3.connect(staging.path)) as dst:
            src.backup(dst)
        ident, files = indexed_files(staging)
        prefix = '/'.join((*PREFIX, ident)) + '/'
        if prefix + 'corpus.sqlite3' in entries:
            raise RuntimeErrorBase('migration_duplicate_corpus')
        entries[prefix + 'corpus.sqlite3'] = staging.path
        for rel, expected in files.items():
            original = safe(source.root / rel, source.root)
            dest = staging.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, dest)
            verify_file(dest, expected)
            entries[prefix + rel] = dest
    return entries


def corpus_ids(files):
    return sorted({PurePosixPath(name).parts[2] for name in files if member_allowed(name)})


def directories(files):
    return {parent.as_posix() for name in files if member_allowed(name)
            for parent in PurePosixPath(name).parents if len(parent.parts) >= 3}


def check_target(paths, ids):
    for ident in ids:
        directory = safe(paths.home.joinpath(*PREFIX, ident), paths.home)
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise RuntimeErrorBase('migration_corpus_target_occupied')


def verify_staged(root, files):
    for ident in corpus_ids(files):
        prefix = '/'.join((*PREFIX, ident)) + '/'
        store = DatasetCorpusStore(root / prefix)
        actual_id, expected = indexed_files(store)
        names = {n.removeprefix(prefix) for n in files if n.startswith(prefix)}
        if actual_id != ident or names != {'corpus.sqlite3', *expected}:
            raise RuntimeErrorBase('migration_corpus_manifest_invalid')
        for name, proof in expected.items():
            verify_file(store.root / name, proof)
