"""Read-only CR2.1 seal inspection. No runtime, provider or reporting entry is imported."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR2.1-real-diagnosis')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def child(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError('cr21_seal_path_invalid')
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError('cr21_seal_path_outside_package')
    return path


def verify(root=OUT, *, worktree=False):
    root = Path(root).resolve()
    seal = json.loads((root / 'closure.json').read_text('utf-8'))
    if seal.get('stage') != 'CR2.1' or seal.get('stage_complete') is not True:
        raise ValueError('cr21_seal_stage_incomplete')
    inventory = {p.relative_to(root).as_posix() for p in root.rglob('*')
                 if p.is_file() and p != root / 'closure.json'}
    if inventory != set(seal['evidence_hashes']):
        raise ValueError('cr21_sealed_evidence_inventory_changed')
    for name, expected in seal['evidence_hashes'].items():
        if digest(child(root, name).read_bytes()) != expected:
            raise ValueError('cr21_sealed_evidence_changed:' + name)
    with zipfile.ZipFile(child(root, seal['source_archive'])) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(seal['source_hashes']):
            raise ValueError('cr21_sealed_source_inventory_changed')
        for name, expected in seal['source_hashes'].items():
            local = child(ROOT, name)
            if digest(archive.read(name)) != expected:
                raise ValueError('cr21_archived_source_changed:' + name)
            if worktree and digest(local.read_bytes()) != expected:
                raise ValueError('cr21_worktree_changed:' + name)
    state = json.loads((root / 'real-v1/state.json').read_text('utf-8'))
    summary = json.loads((root / 'real-v1/analysis/summary.json').read_text('utf-8'))
    if (state.get('status') != 'completed' or state.get('real') is not True
            or state.get('main_completed') != 128
            or summary.get('real') is not True
            or summary.get('main_completed') != 128
            or summary.get('complete_pairs') != 64
            or summary.get('machine_reading_pending') != 0
            or summary.get('frame_outputs') != 32
            or summary.get('frame_machine_read') != 32
            or summary.get('user_review_received') != 12
            or summary.get('receipts', {}).get('reserved_attempts') != state.get('physical_calls')
            or summary.get('user_review_complete') is not True):
        raise ValueError('cr21_sealed_required_work_incomplete')
    frozen = json.loads((root / 'real-v1/freeze.json').read_text('utf-8'))
    for name, expected in frozen['sources'].items():
        if seal['source_hashes'].get(name) != expected:
            raise ValueError('cr21_execution_and_archived_source_differ:' + name)
    return {'stage': 'CR2.1', 'passed': True,
            'source_files': len(seal['source_hashes']),
            'evidence_files': len(seal['evidence_hashes']),
            'writes': 0, 'generation_calls': 0,
            'effect': seal.get('effect_conclusion'), 'worktree_checked': worktree}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(worktree=args.worktree), ensure_ascii=False))
