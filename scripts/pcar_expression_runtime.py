"""维护独立表达阶段；不发起模型调用，呈现授权和回执通过运行中的管理接口完成。"""
import argparse
import json
from pathlib import Path

from persona_runtime.expression_runtime import ExpressionRuntime
from persona_runtime.expression_publication import ExpressionPublication
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.recall_rollback import prepare_snapshot


def execute(action, paths, request):
    runtime=ExpressionRuntime(Store(paths));publisher=ExpressionPublication(runtime)
    actions={'policy':runtime.policy,'configure':runtime.configure,'status':runtime.inspect,
             'plan':publisher.plan,'visible':publisher.visible,'fallback':publisher.fallback,'cancel':publisher.cancel}
    if action=='rollback':return prepare_snapshot(paths,target_schema=30)
    return actions[action](**request)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('policy','configure','status','plan','visible','fallback','cancel','rollback'))
    parser.add_argument('--home',type=Path,required=True);parser.add_argument('--request',type=Path)
    args=parser.parse_args();request=json.loads(args.request.read_text(encoding='utf-8-sig')) if args.request else {}
    print(json.dumps(execute(args.action,RuntimePaths(args.home.resolve()),request),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
