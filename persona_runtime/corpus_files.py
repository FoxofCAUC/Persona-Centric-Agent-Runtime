"""Inspect explicitly supplied files without interpreting chat or creating model input."""
import codecs
import hashlib
from pathlib import Path


FILE_ENVELOPE_VERSION = 'corpus-file-envelope-v1'
MAX_FILE_BYTES = 64 * 1024 * 1024
SUFFIXES = {'.txt': 'text', '.md': 'markdown', '.json': 'json', '.jsonl': 'jsonl'}
ENCODINGS = ('utf-8', 'utf-8-sig', 'utf-16-le', 'utf-16-be', 'gb18030')


class CorpusError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_supplied(path):
    path = Path(path)
    if not path.is_absolute():
        raise CorpusError('corpus_source_absolute_path_required')
    if path.suffix.lower() not in SUFFIXES:
        raise CorpusError('corpus_file_type_unsupported')
    try:
        with path.open('rb') as stream:
            import stat
            import os
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CorpusError('corpus_source_regular_file_required')
            if os.fstat(stream.fileno()).st_size > MAX_FILE_BYTES:
                raise CorpusError('corpus_file_capacity')
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise CorpusError('corpus_source_unavailable') from None
    if len(raw) > MAX_FILE_BYTES:
        raise CorpusError('corpus_file_capacity')
    if not raw:
        raise CorpusError('corpus_file_empty')
    return raw


def inspect_bytes(raw, suffix, encoding=None):
    if suffix.lower() not in SUFFIXES:
        raise CorpusError('corpus_file_type_unsupported')
    if len(raw) > MAX_FILE_BYTES:
        raise CorpusError('corpus_file_capacity')
    if not raw:
        raise CorpusError('corpus_file_empty')
    if encoding is not None and encoding not in ENCODINGS:
        raise CorpusError('corpus_encoding_unsupported')
    bom = None
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise CorpusError('corpus_encoding_unsupported')
    if raw.startswith(codecs.BOM_UTF8):
        bom = 'utf-8-sig'
    elif raw.startswith(codecs.BOM_UTF16_LE):
        bom = 'utf-16-le'
    elif raw.startswith(codecs.BOM_UTF16_BE):
        bom = 'utf-16-be'
    if bom and encoding not in (None, bom, 'utf-8' if bom == 'utf-8-sig' else bom):
        raise CorpusError('corpus_encoding_bom_conflict')
    selected = bom or encoding or 'utf-8'
    try:
        text = raw.decode('utf-16' if bom and bom.startswith('utf-16') else selected, errors='strict')
    except UnicodeError:
        raise CorpusError('corpus_encoding_confirmation_required') from None
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise CorpusError('corpus_binary_or_control_content')
    return {
        'version': FILE_ENVELOPE_VERSION,
        'sha256': digest(raw), 'bytes': len(raw), 'encoding': selected,
        'text_sha256': digest(text.encode('utf-8')), 'characters': len(text),
        'lines': len(text.splitlines()), 'crlf': text.count('\r\n'),
        'lf': text.count('\n') - text.count('\r\n'),
        'cr': text.count('\r') - text.count('\r\n'),
        'format_hint': SUFFIXES[suffix.lower()],
        'parser': None, 'message_count': None,
        'unparsed_character_span': [0, len(text)],
        'state': 'awaiting_export_format', 'learning_eligible': False,
    }
