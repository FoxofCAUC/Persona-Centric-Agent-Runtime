"""Optional local character embeddings. No network or authoritative writes."""
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import threading

from .models import RuntimeErrorBase

MODEL_ID = 'BAAI/bge-small-zh-v1.5'
DIMENSION = 512
ENCODING = 'plain-no-prefix-l2-f32-v1'
CHUNKING = 'offset-384-overlap32-v1'
_gate = threading.Lock()
_loaded = None


class SemanticFailure(RuntimeErrorBase):
    """Stable diagnostic codes only; never include source text or credentials."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(home, path, authority=None):
    """Reject reparse points at every ancestor, escapes and authority hard links."""
    home, path = Path(home).absolute(), Path(path).absolute()
    if not path.is_relative_to(home) or '..' in path.parts:
        raise SemanticFailure('unsafe_semantic_path')
    for node in (path, *path.parents):
        if node.exists() or node.is_symlink():
            info = node.lstat()
            if node.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise SemanticFailure('unsafe_semantic_path')
    if authority and path.exists() and Path(authority).exists() and path.samefile(authority):
        raise SemanticFailure('unsafe_semantic_path')
    return path


def private_directory(home, path):
    path = safe_path(home, path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import subprocess
        account = os.environ['USERDOMAIN'] + '\\' + os.environ['USERNAME']
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                        account + ':(OI)(CI)F', 'SYSTEM:(OI)(CI)F'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000)
    else:
        path.chmod(0o700)
    return path


def checked_vectors(values, count):
    # NumPy is deliberately imported only in the optional path.
    np = numpy_module()
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.shape != (count, DIMENSION) or not np.isfinite(vectors).all():
        raise SemanticFailure('embedding_invalid_vector')
    norms = np.linalg.norm(vectors, axis=1)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise SemanticFailure('embedding_invalid_vector')
    return vectors / norms[:, None]


def numpy_module():
    try:
        import numpy
        return numpy
    except ImportError:
        raise SemanticFailure('embedding_dependency_missing') from None


class CharacterEmbeddingProvider:
    def __init__(self, paths, model_directory):
        self.paths = paths
        self.directory = safe_path(paths.home, Path(model_directory))
        root = paths.home / 'models/character-embedding'
        if not self.directory.is_relative_to(root.absolute()):
            raise SemanticFailure('unsafe_semantic_path')
        self._identity = None

    def identity(self):
        manifest_path = safe_path(self.paths.home, self.directory / 'manifest.json')
        if not manifest_path.is_file():
            raise SemanticFailure('embedding_unavailable')
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            fingerprint = manifest.pop('fingerprint')
            if digest(manifest) != fingerprint or self.directory.name != fingerprint:
                raise ValueError()
            if (manifest['model_id'] != MODEL_ID or manifest['dimension'] != DIMENSION
                    or manifest['encoding'] != ENCODING or len(manifest['revision']) != 40):
                raise ValueError()
            required = {'model_optimized.onnx', 'tokenizer.json', 'config.json',
                        'special_tokens_map.json', 'tokenizer_config.json'}
            if not required.issubset(manifest['files']):
                raise ValueError()
            stamps = []
            for name, expected in sorted(manifest['files'].items()):
                if Path(name).name != name:
                    raise ValueError()
                path = safe_path(self.paths.home, self.directory / name)
                info = path.stat()
                stamps.append((name, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
            # Verify on every process load and on any file metadata change, not every query.
            stamp = (fingerprint, tuple(stamps))
            if self._identity is None or self._identity[0] != stamp:
                for name, expected in manifest['files'].items():
                    if file_hash(self.directory / name) != expected:
                        raise ValueError()
                self._identity = (stamp, manifest | {'fingerprint': fingerprint})
            return dict(self._identity[1])
        except SemanticFailure:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise SemanticFailure('embedding_model_invalid') from None

    @contextmanager
    def _model(self):
        global _loaded
        if not _gate.acquire(blocking=False):
            raise SemanticFailure('embedding_busy')
        try:
            identity = self.identity()
            try:
                from fastembed import TextEmbedding
                from tokenizers import Tokenizer
                for name, version in identity['packages'].items():
                    if importlib.metadata.version(name) != version:
                        raise SemanticFailure('embedding_dependency_mismatch')
            except (ImportError, importlib.metadata.PackageNotFoundError):
                raise SemanticFailure('embedding_dependency_missing') from None
            key = (str(self.directory), identity['fingerprint'])
            if _loaded is None or _loaded[0] != key:
                _loaded = None  # A changed model never shares the old vector space.
                # specific_model_path bypasses download_model's hub branches entirely.
                model = TextEmbedding(MODEL_ID, specific_model_path=str(self.directory),
                                      local_files_only=True, cache_dir=str(self.directory),
                                      threads=2, providers=['CPUExecutionProvider'])
                tokenizer = Tokenizer.from_file(str(self.directory / 'tokenizer.json'))
                tokenizer.no_truncation()
                tokenizer.no_padding()
                _loaded = (key, model, tokenizer)
            yield _loaded[1], _loaded[2]
        except SemanticFailure:
            raise
        except Exception:
            raise SemanticFailure('embedding_inference_failed') from None
        finally:
            _gate.release()

    def chunks(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 24000:
            raise SemanticFailure('embedding_invalid_text')
        with self._model() as (_, tokenizer):
            encoding = tokenizer.encode(text, add_special_tokens=False)
            offsets = encoding.offsets
            if not offsets:
                raise SemanticFailure('embedding_invalid_text')
            chunks = []
            start = 0
            while start < len(offsets):
                end = min(start + 384, len(offsets))
                left = 0 if start == 0 else offsets[start][0]
                right = len(text) if end == len(offsets) else offsets[end][0]
                part = text[left:right]
                # Re-tokenization can change boundary segmentation. Never accept truncation.
                if len(tokenizer.encode(part).ids) > 512:
                    raise SemanticFailure('embedding_chunk_too_large')
                chunks.append({'start': left, 'end': right, 'token_start': start,
                               'token_end': end, 'text': part})
                if end == len(offsets):
                    break
                start = end - 32
            return chunks

    def embed_documents(self, texts):
        texts = list(texts)
        if not texts:
            return []
        with self._model() as (model, tokenizer):
            if any(not isinstance(t, str) or not t.strip() or
                   len(tokenizer.encode(t).ids) > 512 for t in texts):
                raise SemanticFailure('embedding_chunk_too_large')
            return checked_vectors(list(model.embed(texts, batch_size=16, parallel=None)), len(texts))

    def embed_query(self, members):
        if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 20:
            raise SemanticFailure('embedding_invalid_members')
        parts = []
        for index, text in enumerate(members):
            parts.extend(c | {'member_index': index} for c in self.chunks(text))
        return parts, self.embed_documents([c['text'] for c in parts])

    def diagnose(self):
        try:
            with self._model():
                pass
            return {'status': 'ready', 'model': self.identity()}
        except SemanticFailure as exc:
            return {'status': 'unavailable', 'reason': str(exc)}
