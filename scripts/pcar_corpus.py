"""本地微信语料收存维护；只处理显式文件，不调用模型。"""
import argparse
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import CorpusError, ENCODINGS
from persona_runtime.corpus_store import CorpusStore


def execute(action, corpus, request):
    store = CorpusStore(corpus)
    actions = {'init': store.initialize, 'dataset-create': store.create_dataset,
               'inspect': store.inspect, 'import-preview': store.preview,
               'import-commit': store.retain_original, 'dataset-revoke': store.revoke_dataset,
               'verify-originals': store.verify_originals}
    if action not in actions:
        raise CorpusError('corpus_action_invalid')
    return actions[action](**request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    subs = parser.add_subparsers(dest='action', required=True)
    subs.add_parser('init')
    create = subs.add_parser('dataset-create')
    create.add_argument('--label', required=True)
    create.add_argument('--operation-id', required=True)
    show = subs.add_parser('inspect')
    show.add_argument('--dataset-id')
    preview = subs.add_parser('import-preview')
    preview.add_argument('--dataset-id', required=True)
    preview.add_argument('--source', type=Path, required=True)
    preview.add_argument('--encoding', choices=ENCODINGS)
    preview.add_argument('--operation-id', required=True)
    commit = subs.add_parser('import-commit')
    commit.add_argument('--preview-id', required=True)
    commit.add_argument('--expected-hash', required=True)
    commit.add_argument('--operation-id', required=True)
    revoke = subs.add_parser('dataset-revoke')
    revoke.add_argument('--dataset-id', required=True)
    revoke.add_argument('--expected-revision', type=int, required=True)
    revoke.add_argument('--operation-id', required=True)
    subs.add_parser('verify-originals')
    args = vars(parser.parse_args(argv))
    corpus, action = args.pop('corpus'), args.pop('action')
    try:
        result = execute(action, corpus, args)
    except CorpusError as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2
    except (OSError, sqlite3.Error):
        print(json.dumps({'status': 'failed', 'error': 'corpus_local_storage_failed'}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('passed') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
