"""受控首批语料学习；只有显式run动作读取凭据并调用模型。"""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_learning import CorpusLearning
from persona_runtime.corpus_learning_review import export_learning_review


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True);parser.add_argument('--ledger',type=Path,required=True)
    subs=parser.add_subparsers(dest='action',required=True)
    for name in ('init','inspect','recover'):subs.add_parser(name)
    approve=subs.add_parser('register-approval');approve.add_argument('--approval',type=Path,required=True);approve.add_argument('--preview',type=Path,required=True)
    prepare=subs.add_parser('prepare');prepare.add_argument('--approval-id',required=True);prepare.add_argument('--position',type=int,required=True);prepare.add_argument('--operation-id',required=True)
    for name in ('status','finalize','cancel'):
        command=subs.add_parser(name);command.add_argument('--job-id',required=True)
    revoke=subs.add_parser('revoke');revoke.add_argument('--approval-id',required=True)
    run=subs.add_parser('run');run.add_argument('--job-id',required=True);run.add_argument('--credential-home',type=Path,required=True)
    report=subs.add_parser('report');report.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        learning=CorpusLearning(args.ledger,DatasetCorpusStore(args.corpus))
        action=args.action
        if action=='init':result=learning.initialize()
        elif action=='inspect':result=learning.inspect()
        elif action=='recover':result=learning.recover()
        elif action=='register-approval':result=learning.register_approval(args.approval,args.preview)
        elif action=='prepare':result=learning.prepare(args.approval_id,args.position,args.operation_id)
        elif action=='status':result=learning.status(args.job_id)
        elif action=='finalize':result=learning.finalize(args.job_id)
        elif action=='cancel':result=learning.cancel(args.job_id)
        elif action=='revoke':result=learning.revoke(args.approval_id)
        elif action=='report':result=export_learning_review(learning,args.output)
        else:
            from persona_runtime.config import ConfigStore
            from persona_runtime.paths import RuntimePaths
            credentials=ConfigStore(RuntimePaths(args.credential_home.resolve())).load()['model']
            result=asyncio.run(learning.run(args.job_id,credentials))
    except CorpusError as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False));return 2
    except (OSError,sqlite3.Error,ValueError,KeyError):
        print(json.dumps({'status':'failed','error':'corpus_learning_operation_failed'},ensure_ascii=False));return 2
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
