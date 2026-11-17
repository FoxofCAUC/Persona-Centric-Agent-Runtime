"""本地Moect私聊解析与原文核对；数据授权不触发任何模型调用。"""
import argparse
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import CorpusError, ENCODINGS
from persona_runtime.corpus_ingest import MoectCorpusStore
from persona_runtime.corpus_review import export_review


def execute(action, corpus, request):
    store = MoectCorpusStore(corpus)
    if action == 'export-review':
        return export_review(store, **request)
    actions = {'init': store.initialize, 'dataset-create': store.create_dataset, 'inspect': store.inspect,
               'import-preview': store.preview, 'import-commit': store.retain_original,
               'dataset-revoke': store.revoke_dataset, 'verify-originals': store.verify_originals,
               'parse-pair': store.parse_pair, 'parse-status': store.parse_status, 'verify-parse': store.verify_parse}
    if action not in actions:
        raise CorpusError('corpus_action_invalid')
    return actions[action](**request)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    create = sub.add_parser('dataset-create')
    create.add_argument('--label', required=True)
    create.add_argument('--operation-id', required=True)
    show = sub.add_parser('inspect')
    show.add_argument('--dataset-id')
    preview = sub.add_parser('import-preview')
    preview.add_argument('--dataset-id', required=True)
    preview.add_argument('--source', type=Path, required=True)
    preview.add_argument('--encoding', choices=ENCODINGS)
    preview.add_argument('--operation-id', required=True)
    commit = sub.add_parser('import-commit')
    commit.add_argument('--preview-id', required=True)
    commit.add_argument('--expected-hash', required=True)
    commit.add_argument('--operation-id', required=True)
    sub.add_parser('verify-originals')
    pair = sub.add_parser('parse-pair')
    for name in ('dataset-id','json-import-id','md-import-id','operation-id'):
        pair.add_argument('--' + name, required=True)
    pair.add_argument('--grant-path', type=Path, required=True)
    for name in ('parse-status', 'verify-parse'):
        command = sub.add_parser(name)
        command.add_argument('--parse-id', required=True)
    review = sub.add_parser('export-review')
    review.add_argument('--parse-id', required=True)
    review.add_argument('--output', type=Path, required=True)
    revoke = sub.add_parser('dataset-revoke')
    revoke.add_argument('--dataset-id', required=True)
    revoke.add_argument('--expected-revision', type=int, required=True)
    revoke.add_argument('--operation-id', required=True)
    args = vars(parser.parse_args(argv))
    action, corpus = args.pop('action'), args.pop('corpus')
    try:
        result = execute(action, corpus, args)
    except CorpusError as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False))
        return 2
    except (OSError, sqlite3.Error, UnicodeError):
        print(json.dumps({'status': 'failed', 'error': 'corpus_local_import_failed'}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('passed') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
