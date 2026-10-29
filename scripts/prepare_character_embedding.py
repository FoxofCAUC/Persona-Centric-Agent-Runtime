"""Explicit online preparation only. Runtime never imports this downloader."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.character_embedding import (MODEL_ID, ENCODING, DIMENSION, digest,
    file_hash, safe_path, private_directory)
from persona_runtime.paths import RuntimePaths

REPOSITORY = 'Qdrant/bge-small-zh-v1.5'
FILES = ('config.json', 'model_optimized.onnx', 'ort_config.json', 'special_tokens_map.json',
         'tokenizer.json', 'tokenizer_config.json', 'vocab.txt', 'README.md')


def download(url, path):
    with urllib.request.urlopen(url, timeout=120) as response, path.open('wb') as target:
        shutil.copyfileobj(response, target)


def prepare(paths, revision):
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('A full immutable Hugging Face commit is required')
    started = time.perf_counter()
    root = private_directory(paths.home, paths.home / 'models/character-embedding')
    staging = safe_path(paths.home, root / ('preparing-' + uuid.uuid4().hex))
    staging.mkdir()
    # Incomplete preparations remain visibly separate, never eligible for runtime loading.
    for name in FILES:
        download(f'https://huggingface.co/{REPOSITORY}/resolve/{revision}/{name}', staging / name)
    manifest = {
        'schema_version': 1, 'model_id': MODEL_ID, 'artifact_repository': REPOSITORY,
        'revision': revision, 'dimension': DIMENSION, 'encoding': ENCODING,
        'packages': {n: importlib.metadata.version(n) for n in
                     ('fastembed', 'onnxruntime', 'numpy', 'tokenizers')},
        'files': {name: file_hash(staging / name) for name in FILES},
        'license': 'MIT', 'upstream': 'https://huggingface.co/BAAI/bge-small-zh-v1.5',
        'artifact_source': f'https://huggingface.co/{REPOSITORY}/tree/{revision}',
    }
    fingerprint = digest(manifest)
    manifest['fingerprint'] = fingerprint
    (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    destination = safe_path(paths.home, root / fingerprint)
    if destination.exists():
        # Never overwrite a pinned model in use. Keep failed/duplicate staging for explicit cleanup.
        raise ValueError('Pinned destination already exists; verify it instead of downloading again')
    os.replace(staging, destination)
    return {'model_directory': str(destination), 'preparation_seconds': time.perf_counter() - started,
            'manifest': manifest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    paths = RuntimePaths.from_environment()
    safe_path(paths.home, args.report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result = prepare(paths, args.revision)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'model_directory': result['model_directory'],
                      'preparation_seconds': result['preparation_seconds']}))
