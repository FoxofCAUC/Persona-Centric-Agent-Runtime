"""E1有限隔离对照：固定32个表达位置，首次结果与未知状态保留。"""
import argparse
import asyncio
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider
from scripts.pcar_expression_preflight import inspect
from .core import (PROMPT_VERSION, TIMING_VERSION, WIRE_VERSION, ExpressionPayloadError,
                   build_messages, decode_output, local_baseline_plan)
from .transport import ExpressionHTTPClient, ExpressionCallFailure


VERSION = 'expression-e1-run-v1'
REPO = Path(__file__).resolve().parents[2]


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def public_model(plan, position):
    cfg = plan['call_plan']
    return {'base_url': 'https://api.deepseek.com', 'name': cfg['model'], 'encrypted_key': '',
            'timeout': 120, 'max_tokens': cfg['max_tokens'], '_json_output': True,
            '_dialogue_profile': {'version': 'natural-dialogue-v8', 'seed': position['id'],
                                  'temperature': cfg['temperature']}}


def prepare(root, materials_root, upstream, *, repo=REPO):
    root = root.resolve()
    if not root.is_absolute() or root.is_relative_to(repo.resolve()) or root.exists():
        raise ValueError('fresh_external_run_root_required')
    checked = inspect(repo, materials_root=materials_root, upstream=upstream)
    if not checked['passed'] or not checked['full_e0_inputs_checked']:
        raise ValueError('E0_inputs_not_verified')
    frozen = repo / 'experiments/expression_e0'
    manifest = load(frozen / 'materials_manifest.json')
    fixtures = load(frozen / 'public_fixtures.json')
    cfg = load(frozen / 'call_plan.json')
    source_names = sorted(p.relative_to(repo).as_posix() for p in (repo / 'experiments/expression_e1').glob('*.py') if p.name != 'review.py')
    source_names += ['persona_runtime/expression_contract.py', 'scripts/pcar_expression_preflight.py',
                     'experiments/expression_e0/call_plan.json', 'experiments/expression_e0/materials_manifest.json',
                     'experiments/expression_e0/public_fixtures.json', 'docs/expression/E0-core-baseline.json',
                     'docs/licenses/MaiBot-GPL-3.0.txt']
    plan = {'version': VERSION, 'created_at_epoch': time.time(), 'execution_mode': 'real',
            'E0_commit': '6a3b3d55151f2bd5f034cb996323badd66006304',
            'core_baseline_commit': checked['checks']['core']['baseline_commit'],
            'wire_version': WIRE_VERSION, 'prompt_version': PROMPT_VERSION, 'timing_version': TIMING_VERSION,
            'call_plan': cfg, 'fixtures': fixtures, 'cases': manifest['cases'],
            'diagnostic_case_ids': manifest['diagnostic_case_ids'], 'positions': [],
            'source_hashes': {name: sha((repo / name).read_bytes()) for name in source_names},
            'wire_adaptation': '模型返回正文与字符串片段；MaiBot原文映射后由程序计算E0字符区间。',
            'review_renderer_separate_from_generation': True,
            'main_calls_replayed': False, 'formal_database_writes': False}
    inputs = {}
    for case in manifest['cases']:
        data = (materials_root / case['file']).read_bytes()
        if sha(data) != case['sha256']:
            raise ValueError('material_changed_during_prepare')
        inputs[case['id']] = data
    requests = {}
    for position in manifest['positions']:
        material = json.loads(inputs[position['case_id']])
        messages, _ = build_messages(material, fixtures, with_assets=position['arm'] == 'C')
        payload, _ = build_request(public_model(plan, position), messages)
        if len(json.dumps(messages, ensure_ascii=False)) > cfg['max_input_characters']:
            raise ValueError('expression_input_over_capacity')
        plan['positions'].append(position | {'input_sha256': sha(inputs[position['case_id']]),
                                             'request_sha256': fingerprint(payload)})
        requests[position['id']] = payload
    root.mkdir(parents=True, exist_ok=False)
    (root / 'inputs').mkdir()
    for ident, data in inputs.items():
        (root / 'inputs' / (ident + '.json')).write_bytes(data)
    for position in plan['positions']:
        directory = root / 'positions' / position['id']
        directory.mkdir(parents=True)
        atomic_json(directory / 'request.json', requests[position['id']])
    atomic_json(root / 'preflight.json', checked)
    atomic_json(root / 'plan.json', plan)
    atomic_json(root / 'plan-hash.json', {'sha256': sha((root / 'plan.json').read_bytes())})
    for case in plan['cases']:
        material = json.loads(inputs[case['id']])
        atomic_json(root / 'baselines' / (case['id'] + '.json'),
                    {'A': material['draft'], 'B': local_baseline_plan(material), 'source_kind': case['source_kind']})
    return {'prepared': True, 'positions': len(plan['positions']), 'paid_calls': 0}


@contextmanager
def execution_lock(root):
    stream = (root / '.execution.lock').open('a+b')
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        yield
    finally:
        stream.close()


def verify_frozen(root, plan, *, repo=REPO):
    if sha((root / 'plan.json').read_bytes()) != load(root / 'plan-hash.json')['sha256']:
        raise ValueError('expression_plan_changed')
    for name, expected in plan['source_hashes'].items():
        if sha((repo / name).read_bytes()) != expected:
            raise ValueError('expression_source_changed')
    for position in plan['positions']:
        if sha((root / 'inputs' / (position['case_id'] + '.json')).read_bytes()) != position['input_sha256']:
            raise ValueError('expression_material_changed')
        if fingerprint(load(root / 'positions' / position['id'] / 'request.json')) != position['request_sha256']:
            raise ValueError('expression_request_changed')


def usage_total(usage):
    if not isinstance(usage, dict):
        return None
    values = [usage.get('prompt_tokens'), usage.get('completion_tokens')]
    return sum(values) if all(type(v) is int and v >= 0 for v in values) else None


def estimate(usage, cfg):
    if usage_total(usage) is None:
        return None
    price = cfg['price_snapshot']
    amount = (Decimal(usage['prompt_tokens']) * Decimal(price['input_cache_miss_peak']) +
              Decimal(usage['completion_tokens']) * Decimal(price['output_peak'])) / Decimal(1000000)
    return str(amount)


def report(root, plan):
    rows = []
    unresolved = []
    for position in plan['positions']:
        directory = root / 'positions' / position['id']
        if (directory / 'result.json').exists():
            value = load(directory / 'result.json')
            if value['position'] != position['id'] or value['request_sha256'] != position['request_sha256']:
                raise ValueError('expression_result_identity_changed')
            if (directory / 'response.json').exists() and value.get('response_file_sha256') != sha((directory / 'response.json').read_bytes()):
                raise ValueError('expression_response_receipt_changed')
            rows.append(value)
        elif (directory / 'dispatch.json').exists():
            unresolved.append(position['id'])
    totals = [usage_total(row.get('usage')) for row in rows]
    unknown = sum(v is None for v in totals)
    estimates = [row.get('estimated_cost_cny') for row in rows]
    usable = sum(row.get('candidate_status') == 'usable' for row in rows)
    result = {'version': VERSION, 'planned_positions': len(plan['positions']), 'settled_positions': len(rows),
              'usable_candidates': usable, 'unusable_or_failed_candidates': len(rows) - usable,
              'unresolved_dispatches': unresolved, 'unknown_call_positions': [r['position'] for r in rows if r['state'] == 'unknown'],
              'known_tokens': sum(v for v in totals if v is not None), 'unknown_usage_records': unknown,
              'estimated_cost_cny': str(sum((Decimal(v) for v in estimates), Decimal(0))) if all(v is not None for v in estimates) else None,
              'actual_cost_cny': None, 'physical_dispatches_recorded': len(rows) + len(unresolved),
              'known_headers_sent': sum(r.get('diagnostics', {}).get('request_headers_sent') is True for r in rows),
              'split_fallbacks': sum(bool((r.get('candidate') or {}).get('plan_fallback')) for r in rows),
              'user_text_review': 'pending', 'E1_quality_passed': False, 'rows': rows}
    atomic_json(root / 'report.json', result)
    return result


def stop_reason(current, plan):
    if current['unresolved_dispatches'] or current['unknown_call_positions']:
        return 'unknown_dispatch_no_replay'
    if current['unknown_usage_records'] or current['estimated_cost_cny'] is None:
        return 'unknown_usage_or_estimate'
    limits = plan['call_plan']['stop_between_requests']
    if current['known_tokens'] >= limits['known_tokens']:
        return 'known_token_budget_reached'
    if Decimal(current['estimated_cost_cny']) >= Decimal(limits['conservative_estimated_cny']):
        return 'estimated_budget_reached'
    tail = current['rows'][-2:]
    if len(tail) == 2 and all(row['state'] == 'failed' for row in tail):
        return 'consecutive_request_failures'
    return None


async def run(root, credential_home=None, *, client=None, test_key='', repo=REPO):
    root = root.resolve()
    with execution_lock(root):
        plan = load(root / 'plan.json')
        if plan['version'] != VERSION or len(plan['positions']) != 32:
            raise ValueError('expression_run_plan_invalid')
        verify_frozen(root, plan, repo=repo)
        if client is None:
            if credential_home is None:
                raise ValueError('credential_home_required')
            config = ConfigStore(RuntimePaths(credential_home.resolve()))
            configured = config.load()['model']
            require_generation_provider(configured)
            key = configured['encrypted_key']
            if not key:
                raise ValueError('deepseek_key_missing')
            credential_hash = sha(config.file.read_bytes())
            prior = root / 'credential-source.json'
            if prior.exists() and load(prior)['config_sha256'] != credential_hash:
                raise ValueError('credential_source_changed')
            atomic_json(prior, {'config_sha256': credential_hash, 'credential_persisted': False})
            client = ExpressionHTTPClient()
        else:
            config = None
            key = test_key
        for position in plan['positions']:
            current = report(root, plan)
            reason = stop_reason(current, plan)
            if reason:
                atomic_json(root / 'stop.json', {'reason': reason, 'next_position': position['id']})
                return current | {'stop_reason': reason}
            directory = root / 'positions' / position['id']
            if (directory / 'result.json').exists():
                continue
            verify_frozen(root, plan, repo=repo)
            if config is not None and sha(config.file.read_bytes()) != credential_hash:
                raise ValueError('credential_source_changed')
            material = load(root / 'inputs' / (position['case_id'] + '.json'))
            messages, candidates = build_messages(material, plan['fixtures'], with_assets=position['arm'] == 'C')
            model = public_model(plan, position) | {'encrypted_key': key}
            payload, _ = build_request(model, messages)
            if fingerprint(payload) != position['request_sha256']:
                raise ValueError('expression_assembly_changed')
            attempt = str(uuid.uuid4())
            marker = {'position': position['id'], 'attempt_id': attempt, 'purpose': 'expression',
                      'request_sha256': position['request_sha256'], 'started_epoch': time.time(), 'retry_index': 0}
            with (directory / 'dispatch.json').open('x', encoding='utf-8') as stream:
                json.dump(marker, stream)
                stream.flush()
                os.fsync(stream.fileno())
            result = {'position': position['id'], 'case_id': position['case_id'], 'arm': position['arm'],
                      'request_sha256': position['request_sha256'], 'attempt_id': attempt,
                      'state': 'unknown', 'candidate_status': 'unusable', 'usage': None, 'actual_cost_cny': None,
                      'estimated_cost_cny': None, 'candidate': None, 'error_code': None, 'diagnostics': {}}
            started = time.monotonic()

            def save_raw(data):
                with (directory / 'response.json').open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())

            try:
                received = await client.complete(model, messages, save_raw=save_raw)
                result.update(state='received', usage=received['usage'], diagnostics=received['diagnostics'],
                              raw_content=received['content'])
                if received['diagnostics'].get('finish_reason') != 'stop' or received['diagnostics'].get('tool_calls_present'):
                    raise ExpressionPayloadError('expression_completion_unusable')
                candidate = decode_output(received['content'], material, candidates)
                result.update(candidate_status='usable', candidate=candidate,
                              body_available_ms=round((time.monotonic() - started) * 1000, 3))
            except ExpressionPayloadError as error:
                result['error_code'] = str(error)
            except BaseException as error:
                diagnostic = getattr(error, 'diagnostics', {})
                known = diagnostic.get('request_headers_sent') is False or diagnostic.get('http_status', 0) not in (0, 200)
                result.update(state='failed' if known else 'unknown', usage=getattr(error, 'usage', None),
                              diagnostics=diagnostic, error_code=str(error) if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', str(error)) else 'expression_execution_interrupted')
            if (directory / 'response.json').exists():
                result['response_file_sha256'] = sha((directory / 'response.json').read_bytes())
            result['estimated_cost_cny'] = estimate(result['usage'], plan['call_plan'])
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
            atomic_json(directory / 'result.json', result)
            print(json.dumps({'position': position['id'], 'state': result['state'],
                              'candidate': result['candidate_status'], 'error': result['error_code'],
                              'known_tokens': usage_total(result['usage'])}), flush=True)
        current = report(root, plan)
        reason = stop_reason(current, plan)
        if reason:
            atomic_json(root / 'stop.json', {'reason': reason, 'next_position': None})
        if config is not None:
            atomic_json(root / 'credential-source-after.json',
                        {'config_unchanged': sha(config.file.read_bytes()) == credential_hash, 'credential_persisted': False})
        return current | ({'stop_reason': reason} if reason else {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'report'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--materials-root', type=Path)
    parser.add_argument('--upstream', type=Path)
    parser.add_argument('--credential-home', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.root, args.materials_root, args.upstream)
    elif args.action == 'run':
        result = asyncio.run(run(args.root, args.credential_home))
    else:
        result = report(args.root, load(args.root / 'plan.json'))
    print(json.dumps({key: value for key, value in result.items() if key != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
