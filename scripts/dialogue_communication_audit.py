"""Read-only CR3-A closure verification. Imports no runtime or provider code."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A-communication-consumption')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def child(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError('cr3_seal_path_invalid')
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError('cr3_seal_path_outside_package')
    return path


def verify(root=OUT, *, worktree=False):
    root = Path(root).resolve()
    seal = read(root / 'closure.json')
    if seal.get('stage') != 'CR3-A' or seal.get('stage_complete') is not True:
        raise ValueError('cr3_seal_stage_incomplete')
    inventory = {p.relative_to(root).as_posix() for p in root.rglob('*')
                 if p.is_file() and p != root / 'closure.json'}
    if inventory != set(seal['evidence_hashes']):
        raise ValueError('cr3_sealed_evidence_inventory_changed')
    for name, expected in seal['evidence_hashes'].items():
        if digest(child(root, name).read_bytes()) != expected:
            raise ValueError('cr3_sealed_evidence_changed:' + name)
    with zipfile.ZipFile(child(root, seal['source_archive'])) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(seal['source_hashes']):
            raise ValueError('cr3_sealed_source_inventory_changed')
        for name, expected in seal['source_hashes'].items():
            local = child(ROOT, name)
            if digest(archive.read(name)) != expected:
                raise ValueError('cr3_archived_source_changed:' + name)
            if worktree and digest(local.read_bytes()) != expected:
                raise ValueError('cr3_worktree_changed:' + name)
    normal = child(root, seal.get('normal_pack', 'offline-v1'))
    check = read(normal / 'verification.json')
    if (check.get('passed') is not True or check.get('main_requests') != 48
            or check.get('real_calls') != 0 or check.get('mock_generations') != 72
            or seal.get('real_model_calls') != 0
            or seal.get('main_requests') != 48
            or seal.get('corrected_user_verdicts') != {'win': 5, 'tie': 1, 'loss': 6}):
        raise ValueError('cr3_sealed_required_work_incomplete')
    frozen = read(normal / 'freeze.json')
    for name, expected in frozen['sources'].items():
        if seal['source_hashes'].get(name) != expected:
            raise ValueError('cr3_execution_and_archived_source_differ:' + name)
    return {'stage': 'CR3-A', 'passed': True,
            'source_files': len(seal['source_hashes']),
            'evidence_files': len(seal['evidence_hashes']),
            'writes': 0, 'generation_calls': 0,
            'real_accuracy_and_naturalness': 'unverified',
            'worktree_checked': worktree}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(worktree=args.worktree), ensure_ascii=False))
