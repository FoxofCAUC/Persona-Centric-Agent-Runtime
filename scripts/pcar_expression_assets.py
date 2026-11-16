"""Agent维护表达资产；实例和请求文件均显式指定，只有learning-run/step调用模型。"""
import argparse
import asyncio
import json
from pathlib import Path

from persona_runtime.config import ConfigStore
from persona_runtime.expression_assets import ExpressionAssets
from persona_runtime.expression_candidates import ExpressionCandidates
from persona_runtime.expression_learning import ExpressionLearning
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.recall_rollback import prepare_snapshot


def execute(action, paths, request, *, credential_home=None):
    store = Store(paths)
    assets, candidates, learning = ExpressionAssets(store), ExpressionCandidates(store), ExpressionLearning(store)
    calls = {'policy': assets.policy, 'status': assets.status, 'configure': assets.configure,
             'source-register': assets.register_source, 'source-revise': assets.revise_source,
             'put': assets.put, 'disable': assets.disable, 'restore': assets.restore, 'show': assets.show,
             'select': candidates.select, 'validate': candidates.validate, 'publish': learning.publish}
    if action in calls:return calls[action](**request)
    if action == 'rollback':return prepare_snapshot(paths, target_schema=29)
    if action == 'job':
        value = learning.get(request['job_id'])
        return {key: value[key] for key in ('id', 'state', 'attempt_id', 'usage_json', 'result_json', 'error_code')}
    if action in ('learning-prepare', 'learning-run', 'learning-step'):
        model = ConfigStore(RuntimePaths((credential_home or paths.home).resolve())).load()['model']
        if action == 'learning-prepare':return learning.prepare(model=model, **request)
        if action == 'learning-run':return asyncio.run(learning.run(model=model, **request))
        return asyncio.run(learning.step(model))
    raise ValueError('expression_action_invalid')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('policy', 'status', 'configure', 'source-register', 'source-revise',
        'put', 'disable', 'restore', 'show', 'select', 'validate', 'publish', 'job',
        'learning-prepare', 'learning-run', 'learning-step', 'rollback'))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--credential-home', type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8-sig')) if args.request else {}
    value = execute(args.action, RuntimePaths(args.home.resolve()), request, credential_home=args.credential_home)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':main()
