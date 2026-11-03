"""Read-only integrity check for the independently authorized CR3-A.1 continuation.

This entry point imports neither runtime orchestration nor provider clients. It
never repairs evidence, resumes generation, or changes a sealed predecessor.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-continuation')
ORIGIN = OUT.with_name('CR3-A.1-real-consumption-ablation')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def child(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError('continuation_invalid_path')
    value = (root / name).resolve()
    if value == root.resolve() or not value.is_relative_to(root.resolve()):
        raise ValueError('continuation_outside_path')
    return value


def verify(root=OUT, worktree=False):
    root = Path(root).resolve()
    final = (root / 'closure.json').is_file()
    seal = read(root / ('closure.json' if final else 'checkpoint.json'))
    if seal.get('stage') != 'CR3-A.1-continuation':
        raise ValueError('continuation_wrong_stage')
    for name, expected in seal['evidence_hashes'].items():
        if digest(child(root, name).read_bytes()) != expected:
            raise ValueError('continuation_evidence_changed:' + name)
    if final:
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and p != root / 'closure.json'}
        if actual != set(seal['evidence_hashes']):
            raise ValueError('continuation_evidence_inventory_changed')
    with zipfile.ZipFile(child(root, seal['source_archive'])) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(seal['source_hashes']):
            raise ValueError('continuation_source_inventory_changed')
        for name, expected in seal['source_hashes'].items():
            if digest(archive.read(name)) != expected:
                raise ValueError('continuation_archived_source_changed:' + name)
            if worktree and digest(child(ROOT, name).read_bytes()) != expected:
                raise ValueError('continuation_worktree_changed:' + name)
    if digest((ORIGIN / 'closure.json').read_bytes()) != seal['origin_closure_sha256']:
        raise ValueError('continuation_origin_seal_changed')
    # Only file reads: the predecessor's verifier also imports no runtime code.
    from scripts.dialogue_communication_real_audit import verify as verify_origin
    origin = verify_origin(ORIGIN)
    state = read(root / 'real-v1/state.json')
    if state['status'] not in ('completed', 'stopped'):
        raise ValueError('continuation_unsettled_batch')
    if seal.get('stage_complete') and (state['status'] != 'completed' or
                                       seal.get('user_review') != 'completed'):
        raise ValueError('continuation_premature_completion')
    return {'stage': seal['stage'], 'passed': True, 'final_seal': final,
            'stage_complete': seal.get('stage_complete', False),
            'source_files': len(seal['source_hashes']),
            'evidence_files': len(seal['evidence_hashes']),
            'origin_unchanged': origin['passed'], 'batch_status': state['status'],
            'writes': 0, 'generation_calls': 0, 'worktree_checked': worktree}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=OUT)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.worktree), ensure_ascii=False))
