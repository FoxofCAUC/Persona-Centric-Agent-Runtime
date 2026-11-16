"""只读核对E2声明扩展与封口核心；不升级实例、读取密钥或调用模型。"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.pcar_expression_preflight import check_core, source_digest


CHANGED = {'persona_runtime/storage.py', 'persona_runtime/__init__.py', 'persona_runtime/migration.py',
           'persona_runtime/recall_rollback.py', 'scripts/pcar_recall_preflight.py',
           'scripts/pcar_upgrade_check.py', 'pyproject.toml', 'uv.lock'}


def inspect(repo, home=None):
    declaration = json.loads((repo / 'docs/expression/E2-核心扩展清单.json').read_text(encoding='utf-8'))
    baseline_path = repo / 'docs/expression/E0-core-baseline.json'
    if hashlib.sha256(baseline_path.read_bytes()).hexdigest() != declaration['baseline_manifest_sha256']:
        raise ValueError('E0_baseline_manifest_changed')
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    updates = {item['path']: item for item in declaration['declared_changes']}
    if set(updates) != CHANGED:raise ValueError('E2_extension_scope_invalid')
    for item in baseline['files']:
        if item['path'] in updates:
            expected = updates[item['path']]
            if expected['baseline_sha256'] != item['sha256']:raise ValueError('E2_baseline_binding_invalid')
            item['sha256'] = expected['accepted_sha256']
    additions = declaration['new_core_files'] + declaration['E0_extensions']
    baseline['allowed_new_core_files_e0'] = [item['path'] for item in additions]
    core = check_core(repo, baseline)
    for item in additions:
        path = repo / item['path']
        if not path.is_file() or source_digest(path.read_bytes()) != item['accepted_sha256']:
            core['errors'].append({'code': 'extension_file_changed', 'path': item['path']})
    core['passed'] = not core['errors']
    result = {'version': 'expression-E2-preflight-v1', 'passed': core['passed'], 'core': core,
              'baseline_core_files': len(baseline['files']), 'unchanged_baseline_files': len(baseline['files']) - len(updates),
              'declared_data_and_compatibility_changes': len(updates), 'new_E2_core_files': len(declaration['new_core_files']),
              'model_calls': 0, 'instance_modified': False}
    if home is not None:
        database = home / 'data/database/runtime.sqlite3'
        if database.is_file():
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                result['instance'] = {'schema': db.execute('PRAGMA user_version').fetchone()[0], 'read_only': True}
        else:result['instance'] = {'exists': False, 'read_only': True}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True);p.add_argument('--home', type=Path);p.add_argument('--output', type=Path)
    args = p.parse_args();repo = args.repo.resolve()
    result = inspect(repo, args.home.resolve() if args.home else None)
    if args.output:
        if args.output.resolve().is_relative_to(repo):raise ValueError('preflight_output_must_be_outside_source')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':raise SystemExit(main())
