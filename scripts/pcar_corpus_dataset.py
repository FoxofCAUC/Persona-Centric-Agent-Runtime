"""本地E.L2数据视图与分区；没有模型调用或提示写作入口。"""
import argparse
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_dataset_export import export_dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True)
    subs = parser.add_subparsers(dest='action',required=True)
    subs.add_parser('init')
    subs.add_parser('inspect')
    build = subs.add_parser('build')
    build.add_argument('--parse-id',required=True)
    build.add_argument('--operation-id',required=True)
    build.add_argument('--recipe',type=Path)
    for action in ('status','verify'):
        item = subs.add_parser(action)
        item.add_argument('--view-id',required=True)
    export = subs.add_parser('export')
    export.add_argument('--view-id',required=True)
    export.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv)
    try:
        store = DatasetCorpusStore(args.corpus)
        if args.action=='init':result = store.initialize()
        elif args.action=='inspect':result = store.inspect()
        elif args.action=='build':
            changes = json.loads(args.recipe.read_text(encoding='utf-8-sig')) if args.recipe else None
            result = store.build_version(args.parse_id,args.operation_id,changes)
        elif args.action=='status':result = store.view_status(args.view_id)
        elif args.action=='verify':result = store.verify_version(args.view_id)
        else:result = export_dataset(store,args.view_id,args.output)
    except CorpusError as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False))
        return 2
    except (OSError,sqlite3.Error,ValueError,TypeError):
        print(json.dumps({'status':'failed','error':'corpus_dataset_local_operation_failed'},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result.get('passed') is False else 0


if __name__=='__main__':
    raise SystemExit(main())
