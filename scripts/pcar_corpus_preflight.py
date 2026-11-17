"""核对语料前置工程追加边界；正式实例只读，不打开模型或聊天内容。"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.pcar_expression_handoff_preflight import accepted_files
from scripts.pcar_expression_preflight import check_core, source_digest


MANIFEST = 'docs/expression/E.L0-L1-语料基础扩展清单.json'
PARENT = 'docs/expression/E阶段归档-README发布保护整合清单.json'


def inspect(repo, home=None):
    repo = repo.resolve()
    baseline, files = accepted_files(repo)
    ledger = json.loads((repo / MANIFEST).read_text(encoding='utf-8'))
    if (ledger['version'] != 'corpus-foundation-extension-v1'
            or ledger['parent_manifest'] != PARENT
            or ledger['parent_manifest_sha256'] != hashlib.sha256((repo / PARENT).read_bytes()).hexdigest()
            or ledger['baseline_core_files'] != len(files) or ledger['declared_changes']):
        raise ValueError('corpus_extension_baseline_invalid')
    added = ledger['new_core_files']
    for item in added:
        if item['path'] in files:
            raise ValueError('corpus_extension_duplicate')
        files[item['path']] = {'path': item['path'], 'sha256': item['sha256']}
    core = check_core(repo, {'commit': baseline['commit'], 'files': list(files.values()),
                            'allowed_new_core_files_e0': []})
    test_errors = []
    for item in ledger['test_files']:
        path = (repo / item['path']).resolve()
        if (not path.is_relative_to(repo) or not path.is_file()
                or source_digest(path.read_bytes()) != item['sha256']):
            test_errors.append({'code': 'corpus_test_file_changed', 'path': item['path']})
    result = {'version': 'corpus-foundation-preflight-v1', 'passed': core['passed'] and not test_errors,
              'core': core, 'unchanged_runtime_files': len(files) - len(added),
              'new_local_corpus_files': len(added), 'test_errors': test_errors,
              'corpus_schema': 1, 'candidate_schema': 32, 'parser_ready': False,
              'waiting_for': 'author_wechat_export', 'model_calls': 0, 'instance_modified': False}
    if home is not None:
        path = (home / 'data/database/runtime.sqlite3').resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                result['instance'] = {'schema': db.execute('PRAGMA user_version').fetchone()[0], 'read_only': True}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.output and args.output.resolve().is_relative_to(repo):
        parser.error('corpus_preflight_output_must_be_outside_source')
    try:
        result = inspect(repo, args.home.resolve() if args.home else None)
    except (OSError, ValueError, KeyError, TypeError):
        result = {'version': 'corpus-foundation-preflight-v1', 'passed': False,
                  'error': 'corpus_preflight_inputs_invalid', 'model_calls': 0, 'instance_modified': False}
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
