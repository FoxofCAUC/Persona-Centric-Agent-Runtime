"""区分未发送与用量未知的执行修订；沿用冻结请求，保留旧回执。"""
import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import re
import time
import uuid

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider
from . import repair
from .repair_protocol import build_messages, model_for
from .runner import REPO, estimate, execution_lock, fingerprint, load, sha, usage_total
from .transport import ExpressionHTTPClient


def not_dispatched(directory, row):
    diagnostic = row.get('diagnostics') or {}
    return (row['state'] == 'failed' and diagnostic.get('request_headers_sent') is False
            and diagnostic.get('response_headers_received') is False
            and not (directory / 'response.json').exists() and not (directory / 'response-receipt.json').exists())


def prepare(root):
    original = load(root / 'plan.json')
    repair.verify(root, original)
    repair.reconcile(root, original)
    eligible = []
    for position in original['positions']:
        directory = root / 'positions' / position['id']
        if (directory / 'seal.json').exists():
            repair.verify_seal(directory)
            row = load(directory / 'result.json')
            if not_dispatched(directory, row):
                eligible.append(position)
            elif row['state'] == 'unknown' or usage_total(row.get('usage')) is None:
                raise ValueError('unconfirmed_call_cannot_be_rescheduled')
        elif (directory / 'dispatch.json').exists():
            raise ValueError('unresolved_dispatch_cannot_be_rescheduled')
    if len(eligible) != 1:
        raise ValueError('one_proven_unsent_failure_required')
    source = eligible[0]
    replacement = source | {'id': source['id'] + '-N1', 'replaces_unsent_position': source['id']}
    target = root / 'positions' / replacement['id']
    target.mkdir(exist_ok=False)
    (target / 'request.json').write_bytes((root / 'positions' / source['id'] / 'request.json').read_bytes())
    source_file = 'experiments/expression_e1/repair_continue.py'
    revision = {'version': 'expression-e1-execution-v3', 'base_plan_sha256': sha((root / 'plan.json').read_bytes()),
                'source_hashes': {source_file: sha((REPO / source_file).read_bytes())},
                'positions': original['positions'] + [replacement], 'max_physical_calls': len(original['positions']),
                'max_local_attempts': len(original['positions']) + 1, 'additional_pre_send_attempts_max': 1,
                'unsent_proof': {'position': source['id'], 'seal_sha256': sha((root / 'positions' / source['id'] / 'seal.json').read_bytes())},
                'reason': '请求头和响应头均未发送的失败属于未派发，不按模型未知用量处理；未知模型调用仍停止。',
                'prior_successful_or_unknown_calls_replayed': False}
    atomic_json(root / 'execution-v3.json', revision)
    atomic_json(root / 'execution-v3-hash.json', {'sha256': sha((root / 'execution-v3.json').read_bytes())})
    return {'positions_including_unsent': len(revision['positions']), 'max_physical_calls': revision['max_physical_calls'], 'new_calls': 0}


def verify(root, plan, revision):
    repair.verify(root, plan)
    if sha((root / 'execution-v3.json').read_bytes()) != load(root / 'execution-v3-hash.json')['sha256']:
        raise ValueError('execution_revision_changed')
    if revision['base_plan_sha256'] != sha((root / 'plan.json').read_bytes()):
        raise ValueError('base_plan_changed')
    for name, digest in revision['source_hashes'].items():
        if sha((REPO / name).read_bytes()) != digest:
            raise ValueError('execution_revision_source_changed')
    proof = revision['unsent_proof']
    directory = root / 'positions' / proof['position']
    repair.verify_seal(directory)
    if sha((directory / 'seal.json').read_bytes()) != proof['seal_sha256'] or not not_dispatched(directory, load(directory / 'result.json')):
        raise ValueError('unsent_dispatch_proof_changed')
    for position in revision['positions']:
        if fingerprint(load(root / 'positions' / position['id'] / 'request.json')) != position['request_sha256']:
            raise ValueError('execution_request_changed')


def report(root, plan, revision):
    rows, pending, unsent = [], [], []
    for position in revision['positions']:
        directory = root / 'positions' / position['id']
        if (directory / 'seal.json').exists():
            repair.verify_seal(directory)
            marker = repair.marker_for(directory, position)
            row = load(directory / 'result.json')
            if row['position'] != position['id'] or row['attempt_id'] != marker['attempt_id'] or row['request_sha256'] != position['request_sha256']:
                raise ValueError('execution_result_identity_changed')
            rows.append(row)
            if not_dispatched(directory, row):
                unsent.append(position['id'])
        elif (directory / 'dispatch.json').exists():
            pending.append(position['id'])
    billable = [row for row in rows if row['position'] not in unsent]
    unknown = sum(usage_total(row.get('usage')) is None for row in billable) + len(pending)
    result = {'version': revision['version'], 'settled_local_attempts': len(rows), 'reserved_local_attempts': len(revision['positions']),
              'usable_candidates': sum(row['candidate_status'] == 'usable' for row in rows), 'not_dispatched_positions': unsent,
              'usage_classification_for_unsent': 'not_applicable; original usage remains null',
              'unresolved_dispatches': pending, 'unknown_call_positions': [row['position'] for row in billable if row['state'] == 'unknown'],
              'unknown_usage_records': unknown, 'known_tokens': sum(v for row in billable if (v := usage_total(row.get('usage'))) is not None),
              'physical_calls_recorded': len(billable) + len(pending),
              'estimated_cost_cny': str(sum((Decimal(row['estimated_cost_cny']) for row in billable), Decimal(0))) if not unknown else None,
              'actual_cost_cny': None, 'user_text_review': 'pending', 'E1_complete': False, 'rows': rows}
    atomic_json(root / 'continuation-report.json', result)
    return result


def stop_reason(current, plan, revision):
    if current['unresolved_dispatches'] or current['unknown_call_positions']:
        return 'unknown_dispatch_no_replay'
    if current['unknown_usage_records']:
        return 'unknown_usage_or_estimate'
    if current['physical_calls_recorded'] >= revision['max_physical_calls']:
        return 'physical_call_plan_complete'
    limits = plan['call_plan']['stop_between_requests']
    if current['known_tokens'] >= limits['known_tokens'] or Decimal(current['estimated_cost_cny']) >= Decimal(limits['conservative_estimated_cny']):
        return 'observed_budget_reached'
    return None


async def run(root, credential_home=None, *, client=None):
    with execution_lock(root):
        plan, revision = load(root / 'plan.json'), load(root / 'execution-v3.json')
        verify(root, plan, revision)
        execution = plan | {'positions': revision['positions']}
        repair.reconcile(root, execution)
        current = report(root, plan, revision)
        reason = stop_reason(current, plan, revision)
        if reason:
            return current | {'stop_reason': reason}
        config, before, key = None, None, ''
        if client is None:
            if credential_home is None:
                raise ValueError('credential_home_required')
            config = ConfigStore(RuntimePaths(credential_home.resolve()))
            model = config.load()['model']
            require_generation_provider(model)
            key = model['encrypted_key']
            if not key:
                raise ValueError('deepseek_key_missing')
            before = sha(config.file.read_bytes())
            if (root / 'credential-source.json').exists() and load(root / 'credential-source.json')['config_sha256'] != before:
                raise ValueError('credential_source_changed')
            client = ExpressionHTTPClient()
        try:
            for position in revision['positions']:
                current = report(root, plan, revision)
                reason = stop_reason(current, plan, revision)
                if reason:
                    return current | {'stop_reason': reason}
                directory = root / 'positions' / position['id']
                if (directory / 'seal.json').exists():
                    continue
                verify(root, plan, revision)
                if config is not None and sha(config.file.read_bytes()) != before:
                    raise ValueError('credential_source_changed')
                item = load(root / 'inputs' / (position['case_id'] + '.json'))
                messages, _ = build_messages(item, plan['fixtures'], position['protocol'], with_assets=position['arm'] == 'C')
                model = model_for(plan, position) | {'encrypted_key': key}
                if fingerprint(build_request(model, messages)[0]) != position['request_sha256']:
                    raise ValueError('execution_assembly_changed')
                marker = {'position': position['id'], 'attempt_id': str(uuid.uuid4()), 'retry_index': 0,
                          'request_sha256': position['request_sha256'], 'input_sha256': position['input_sha256'], 'started_epoch': time.time()}
                repair.exclusive_bytes(directory / 'dispatch.json', (json.dumps(marker) + '\n').encode('utf-8'))

                def save_raw(raw):
                    repair.exclusive_bytes(directory / 'response.json', raw)
                    atomic_json(directory / 'response-receipt.json', {'attempt_id': marker['attempt_id'],
                                'request_sha256': position['request_sha256'], 'dispatch_sha256': sha((directory / 'dispatch.json').read_bytes()),
                                'response_sha256': sha(raw), 'http_complete': True, 'captured_epoch': time.time()})

                try:
                    received = await client.complete(model, messages, save_raw=save_raw)
                    atomic_json(directory / 'transport.json', received['diagnostics'])
                except BaseException as error:
                    if (directory / 'response-receipt.json').exists():
                        if getattr(error, 'diagnostics', None):
                            atomic_json(directory / 'transport.json', error.diagnostics)
                    else:
                        diagnostic = getattr(error, 'diagnostics', {})
                        known = diagnostic.get('request_headers_sent') is False or diagnostic.get('http_status', 0) not in (0, 200)
                        usage = getattr(error, 'usage', None)
                        row = {'position': position['id'], 'case_id': position['case_id'], 'original_position': position['original_position'],
                               'protocol': position['protocol'], 'arm': position['arm'], 'attempt_id': marker['attempt_id'],
                               'request_sha256': position['request_sha256'], 'state': 'failed' if known else 'unknown',
                               'candidate_status': 'unusable', 'candidate': None, 'usage': usage, 'estimated_cost_cny': estimate(usage, plan['call_plan']),
                               'actual_cost_cny': None, 'diagnostics': diagnostic,
                               'error_code': str(error) if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', str(error)) else 'expression_execution_interrupted'}
                        atomic_json(directory / 'result.json', row)
                        repair.seal(directory)
                if not (directory / 'seal.json').exists():
                    atomic_json(directory / 'result.json', repair.result_from_receipt(root, plan, position))
                    repair.seal(directory)
                row = load(directory / 'result.json')
                print(json.dumps({'position': position['id'], 'state': row['state'], 'candidate': row['candidate_status'], 'error': row['error_code']}), flush=True)
            return report(root, plan, revision)
        finally:
            if config is not None:
                atomic_json(root / 'credential-source-after-v3.json', {'config_unchanged': sha(config.file.read_bytes()) == before,
                                                                     'credential_persisted': False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'report'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--credential-home', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.root)
    elif args.action == 'run':
        result = asyncio.run(run(args.root, args.credential_home))
    else:
        plan, revision = load(args.root / 'plan.json'), load(args.root / 'execution-v3.json')
        verify(args.root, plan, revision)
        result = report(args.root, plan, revision)
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
