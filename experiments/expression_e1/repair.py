"""E1协议修复的有限执行器；完整回执本地恢复，旧32个首次位置保持。"""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time
import uuid

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request, response_metadata, usage_fields
from persona_runtime.provider_policy import require_generation_provider
from scripts.pcar_expression_preflight import inspect
from .core import ExpressionPayloadError
from .repair_protocol import PROTOCOLS, JSON_PROTOCOL, TEXT_PROTOCOL, build_messages, decode_output_for, model_for
from .runner import REPO, estimate, execution_lock, fingerprint, load, sha, usage_total, verify_frozen as verify_original
from .transport import ExpressionHTTPClient


VERSION = 'expression-e1-repair-v2'
TARGETS = ('E0-06-C', 'E0-07-C', 'E0-08-C', 'E0-15-C', 'E0-07-C0', 'E0-08-C0', 'E0-17-C', 'E0-24-C')


def exclusive_bytes(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(root, original_root, upstream, *, repo=REPO):
    root, original_root = root.resolve(), original_root.resolve()
    if root.exists() or root.is_relative_to(repo.resolve()) or root.is_relative_to(original_root) or original_root.is_relative_to(root):
        raise ValueError('fresh_external_repair_root_required')
    original = load(original_root / 'plan.json')
    verify_original(original_root, original, repo=repo)
    checks = inspect(repo, materials_root=original_root / 'inputs', upstream=upstream)
    if not checks['passed']:
        raise ValueError('repair_baseline_invalid')
    frozen_files = {str(p.relative_to(original_root)): sha(p.read_bytes()) for p in original_root.rglob('*')
                    if p.is_file() and (p.name in ('plan.json', 'plan-hash.json', 'request.json', 'dispatch.json', 'response.json', 'result.json') or p.parent.name == 'inputs')}
    cfg = json.loads(json.dumps(original['call_plan']))
    cfg.update(version='expression-e1-repair-finite-plan-v2', execution_phase='E1 protocol repair',
               positions={'J3': 8, 'T1': 8, 'total_physical_calls_max': 16, 'new_primary': 0,
                          'paid_selector': 0, 'paid_splitter': 0})
    cfg.pop('arms', None)
    cfg.pop('wire_contract', None)
    cfg['stop_between_requests'].update(known_tokens=100000, conservative_estimated_cny='0.50')
    dependencies = list(original['source_hashes']) + ['experiments/expression_e1/repair.py', 'experiments/expression_e1/repair_protocol.py']
    plan = {'version': VERSION, 'created_at_epoch': time.time(), 'execution_kind': 'real',
            'authorization': '作者要求修复E1暴露问题；独立冻结协议工程补验',
            'original_root': str(original_root), 'original_files': frozen_files,
            'source_hashes': {p: sha((repo / p).read_bytes()) for p in sorted(set(dependencies))},
            'call_plan': cfg, 'max_physical_calls': 16, 'automatic_retries': 0,
            'fixtures': original['fixtures'], 'positions': [], 'target_original_positions': list(TARGETS),
            'scope': '6个旧空白位置与2个代码/短确认对照，每种协议各8次；协议不同，旧结果不覆盖',
            'primary_calls': 0, 'formal_writes': False, 'candidate_protocols': list(PROTOCOLS)}
    source_positions = {p['id']: p for p in original['positions']}
    requests, inputs = {}, {}
    for index, ident in enumerate(TARGETS):
        old = source_positions[ident]
        data = (original_root / 'inputs' / (old['case_id'] + '.json')).read_bytes()
        if sha(data) != old['input_sha256']:
            raise ValueError('repair_input_differs_from_original')
        inputs[old['case_id']] = data
        item = json.loads(data)
        variants = PROTOCOLS if index % 2 == 0 else tuple(reversed(PROTOCOLS))
        for protocol in variants:
            short = 'J3' if protocol == JSON_PROTOCOL else 'T1'
            position = {'id': f'R2-{index + 1:02d}-{short}', 'case_id': old['case_id'],
                        'original_position': ident, 'arm': old['arm'], 'protocol': protocol,
                        'input_sha256': sha(data), 'prior_request_sha256': old['request_sha256']}
            messages, _ = build_messages(item, plan['fixtures'], protocol, with_assets=old['arm'] == 'C')
            payload, _ = build_request(model_for(plan, position), messages)
            if len(json.dumps(messages, ensure_ascii=False)) > cfg['max_input_characters']:
                raise ValueError('repair_input_over_capacity')
            position['request_sha256'] = fingerprint(payload)
            if position['request_sha256'] == old['request_sha256']:
                raise ValueError('repair_request_must_change_protocol')
            plan['positions'].append(position)
            requests[position['id']] = payload
    root.mkdir(parents=True, exist_ok=False)
    (root / 'inputs').mkdir()
    for ident, data in inputs.items():
        (root / 'inputs' / (ident + '.json')).write_bytes(data)
    for position in plan['positions']:
        directory = root / 'positions' / position['id']
        directory.mkdir(parents=True)
        atomic_json(directory / 'request.json', requests[position['id']])
    atomic_json(root / 'preflight.json', checks)
    atomic_json(root / 'plan.json', plan)
    atomic_json(root / 'plan-hash.json', {'sha256': sha((root / 'plan.json').read_bytes())})
    return {'prepared_positions': len(plan['positions']), 'paid_calls': 0}


def verify(root, plan, *, repo=REPO):
    if plan['version'] != VERSION or not 0 < len(plan['positions']) <= plan['max_physical_calls'] <= 16:
        raise ValueError('repair_plan_invalid')
    if sha((root / 'plan.json').read_bytes()) != load(root / 'plan-hash.json')['sha256']:
        raise ValueError('repair_plan_changed')
    for name, digest in plan['source_hashes'].items():
        if sha((repo / name).read_bytes()) != digest:
            raise ValueError('repair_source_changed')
    original_root = Path(plan['original_root'])
    for name, digest in plan['original_files'].items():
        if sha((original_root / name).read_bytes()) != digest:
            raise ValueError('original_batch_changed')
    ids = [p['id'] for p in plan['positions']]
    if len(set(ids)) != len(ids):
        raise ValueError('repair_position_identity_invalid')
    for p in plan['positions']:
        if sha((root / 'inputs' / (p['case_id'] + '.json')).read_bytes()) != p['input_sha256']:
            raise ValueError('repair_input_changed')
        if fingerprint(load(root / 'positions' / p['id'] / 'request.json')) != p['request_sha256']:
            raise ValueError('repair_request_changed')


def marker_for(directory, position):
    marker = load(directory / 'dispatch.json')
    if (marker['position'], marker['request_sha256'], marker['input_sha256'], marker['retry_index']) != (
            position['id'], position['request_sha256'], position['input_sha256'], 0):
        raise ValueError('repair_dispatch_identity_changed')
    return marker


def raw_receipt(directory, position):
    marker = marker_for(directory, position)
    receipt = load(directory / 'response-receipt.json')
    if (receipt['attempt_id'] != marker['attempt_id'] or receipt['request_sha256'] != position['request_sha256']
            or receipt['dispatch_sha256'] != sha((directory / 'dispatch.json').read_bytes()) or receipt['http_complete'] is not True
            or receipt['response_sha256'] != sha((directory / 'response.json').read_bytes())):
        raise ValueError('repair_response_receipt_changed')
    return marker, receipt


def result_from_receipt(root, plan, position):
    directory = root / 'positions' / position['id']
    marker, receipt = raw_receipt(directory, position)
    raw = (directory / 'response.json').read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        data = {}
    usage = usage_fields(data.get('usage'), deepseek=True) if isinstance(data, dict) else None
    diagnostics = (load(directory / 'transport.json') if (directory / 'transport.json').exists() else
                   {'request_headers_sent': True, 'response_headers_received': True, 'http_status': 200,
                    'elapsed_ms': None, 'receipt_recovered_locally': True})
    result = {'position': position['id'], 'case_id': position['case_id'], 'original_position': position['original_position'],
              'protocol': position['protocol'], 'arm': position['arm'], 'attempt_id': marker['attempt_id'],
              'request_sha256': position['request_sha256'], 'state': 'received', 'usage': usage,
              'estimated_cost_cny': estimate(usage, plan['call_plan']), 'actual_cost_cny': None,
              'candidate_status': 'unusable', 'candidate': None, 'error_code': None, 'raw_content': None,
              'response_file_sha256': receipt['response_sha256'], 'diagnostics': diagnostics}
    try:
        choice = data['choices'][0]
        content = choice['message']['content']
        result['raw_content'] = content
        result['diagnostics'] = diagnostics | response_metadata(data) | {'finish_reason': choice.get('finish_reason')}
        if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls'):
            raise ExpressionPayloadError('expression_completion_unusable')
        material = load(root / 'inputs' / (position['case_id'] + '.json'))
        _, candidates = build_messages(material, plan['fixtures'], position['protocol'], with_assets=position['arm'] == 'C')
        decoded = decode_output_for(position['protocol'], content, material, candidates)
        result.update(candidate_status='usable', candidate=decoded)
    except ExpressionPayloadError as error:
        result['error_code'] = str(error)
    except (KeyError, IndexError, TypeError):
        result['error_code'] = 'provider_response_invalid'
    return result


SEALED_FILES = ('dispatch.json', 'response.json', 'response-receipt.json', 'transport.json', 'result.json')


def seal(directory):
    atomic_json(directory / 'seal.json', {'files': {name: sha((directory / name).read_bytes())
                                                  for name in SEALED_FILES if (directory / name).exists()}})


def verify_seal(directory):
    frozen = load(directory / 'seal.json')['files']
    if set(frozen) != {name for name in SEALED_FILES if (directory / name).exists()}:
        raise ValueError('sealed_receipt_file_missing_or_added')
    if any(sha((directory / name).read_bytes()) != value for name, value in frozen.items()):
        raise ValueError('sealed_receipt_changed')


def reconcile(root, plan):
    """只用已经完整落盘的HTTP回执恢复派生结果，不进行网络操作。"""
    recovered = []
    for position in plan['positions']:
        directory = root / 'positions' / position['id']
        if (directory / 'seal.json').exists():
            verify_seal(directory)
            continue
        if not (directory / 'response-receipt.json').exists():
            continue
        derived = result_from_receipt(root, plan, position)
        path = directory / 'result.json'
        if path.exists() and load(path) != derived:
            raise ValueError('unsealed_result_differs_from_raw_receipt')
        if not path.exists():
            atomic_json(path, derived)
        seal(directory)
        atomic_json(directory / 'recovery.json', {'local_only': True, 'new_physical_calls': 0,
                                                 'response_sha256': derived['response_file_sha256']})
        recovered.append(position['id'])
    return recovered


def report(root, plan):
    rows, unresolved = [], []
    for position in plan['positions']:
        directory = root / 'positions' / position['id']
        if (directory / 'seal.json').exists():
            verify_seal(directory)
            marker = marker_for(directory, position)
            row = load(directory / 'result.json')
            if row['attempt_id'] != marker['attempt_id'] or row['request_sha256'] != position['request_sha256'] or row['position'] != position['id']:
                raise ValueError('sealed_result_identity_changed')
            rows.append(row)
        elif (directory / 'dispatch.json').exists():
            marker_for(directory, position)
            unresolved.append(position['id'])
    unknown_usage = sum(usage_total(r['usage']) is None for r in rows) + len(unresolved)
    estimates = [r['estimated_cost_cny'] for r in rows]
    result = {'version': VERSION, 'planned_positions': len(plan['positions']), 'settled_positions': len(rows),
              'usable_candidates': sum(r['candidate_status'] == 'usable' for r in rows),
              'unknown_usage_records': unknown_usage, 'unresolved_dispatches': unresolved,
              'unknown_call_positions': [r['position'] for r in rows if r['state'] == 'unknown'],
              'known_tokens': sum(v for r in rows if (v := usage_total(r['usage'])) is not None),
              'estimated_cost_cny': str(sum((Decimal(v) for v in estimates), Decimal(0))) if not unknown_usage and all(v is not None for v in estimates) else None,
              'actual_cost_cny': None, 'physical_dispatches_recorded': len(rows) + len(unresolved),
              'user_text_review': 'pending', 'E1_complete': False,
              'protocols': {protocol: {'received': sum(r['protocol'] == protocol for r in rows),
                                      'usable': sum(r['protocol'] == protocol and r['candidate_status'] == 'usable' for r in rows)} for protocol in PROTOCOLS},
              'rows': rows}
    atomic_json(root / 'report.json', result)
    return result


def stop_reason(current, plan):
    if current['unresolved_dispatches'] or current['unknown_call_positions']:
        return 'unknown_dispatch_no_replay'
    if current['unknown_usage_records']:
        return 'unknown_usage_or_estimate'
    limits = plan['call_plan']['stop_between_requests']
    if current['known_tokens'] >= limits['known_tokens']:
        return 'known_token_budget_reached'
    if current['estimated_cost_cny'] is None or Decimal(current['estimated_cost_cny']) >= Decimal(limits['conservative_estimated_cny']):
        return 'estimated_budget_reached'
    if len(current['rows']) >= 2 and all(r['state'] == 'failed' for r in current['rows'][-2:]):
        return 'consecutive_request_failures'
    return None


async def run(root, credential_home=None, *, client=None, repo=REPO):
    root = root.resolve()
    with execution_lock(root):
        plan = load(root / 'plan.json')
        verify(root, plan, repo=repo)
        reconcile(root, plan)
        initial = report(root, plan)
        initial_stop = stop_reason(initial, plan)
        if initial_stop or initial['settled_positions'] == len(plan['positions']):
            return initial | ({'stop_reason': initial_stop} if initial_stop else {})
        config, credential_hash, key = None, None, ''
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
            source = root / 'credential-source.json'
            if source.exists() and load(source)['config_sha256'] != credential_hash:
                raise ValueError('credential_source_changed')
            atomic_json(source, {'config_sha256': credential_hash, 'credential_persisted': False})
            client = ExpressionHTTPClient()
        try:
            reconcile(root, plan)
            for position in plan['positions']:
                current = report(root, plan)
                reason = stop_reason(current, plan)
                if reason:
                    atomic_json(root / 'stop.json', {'reason': reason, 'next_position': position['id']})
                    return current | {'stop_reason': reason}
                directory = root / 'positions' / position['id']
                if (directory / 'seal.json').exists():
                    continue
                verify(root, plan, repo=repo)
                if config is not None and sha(config.file.read_bytes()) != credential_hash:
                    raise ValueError('credential_source_changed')
                item = load(root / 'inputs' / (position['case_id'] + '.json'))
                messages, _ = build_messages(item, plan['fixtures'], position['protocol'], with_assets=position['arm'] == 'C')
                model = model_for(plan, position) | {'encrypted_key': key}
                if fingerprint(build_request(model, messages)[0]) != position['request_sha256']:
                    raise ValueError('repair_assembly_changed')
                marker = {'position': position['id'], 'attempt_id': str(uuid.uuid4()),
                          'request_sha256': position['request_sha256'], 'input_sha256': position['input_sha256'],
                          'started_epoch': time.time(), 'retry_index': 0}
                exclusive_bytes(directory / 'dispatch.json', (json.dumps(marker) + '\n').encode('utf-8'))

                def save_raw(raw):
                    exclusive_bytes(directory / 'response.json', raw)
                    atomic_json(directory / 'response-receipt.json',
                                {'attempt_id': marker['attempt_id'], 'request_sha256': position['request_sha256'],
                                 'dispatch_sha256': sha((directory / 'dispatch.json').read_bytes()),
                                 'response_sha256': sha(raw), 'http_complete': True, 'captured_epoch': time.time()})

                try:
                    received = await client.complete(model, messages, save_raw=save_raw)
                    atomic_json(directory / 'transport.json', received['diagnostics'])
                except BaseException as error:
                    if (directory / 'response-receipt.json').exists():
                        if getattr(error, 'diagnostics', None):
                            atomic_json(directory / 'transport.json', error.diagnostics)
                    else:
                        diag = getattr(error, 'diagnostics', {})
                        known = diag.get('request_headers_sent') is False or diag.get('http_status', 0) not in (0, 200)
                        usage = getattr(error, 'usage', None)
                        failed = {'position': position['id'], 'case_id': position['case_id'], 'original_position': position['original_position'],
                                  'arm': position['arm'], 'protocol': position['protocol'], 'attempt_id': marker['attempt_id'],
                                  'request_sha256': position['request_sha256'], 'state': 'failed' if known else 'unknown',
                                  'candidate_status': 'unusable', 'candidate': None, 'usage': usage,
                                  'estimated_cost_cny': estimate(usage, plan['call_plan']), 'actual_cost_cny': None,
                                  'error_code': str(error) if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', str(error)) else 'expression_execution_interrupted',
                                  'diagnostics': diag}
                        atomic_json(directory / 'result.json', failed)
                        seal(directory)
                if not (directory / 'seal.json').exists():
                    result = result_from_receipt(root, plan, position)
                    atomic_json(directory / 'result.json', result)
                    seal(directory)
                result = load(directory / 'result.json')
                print(json.dumps({'position': position['id'], 'state': result['state'],
                                  'candidate': result['candidate_status'], 'error': result['error_code'],
                                  'known_tokens': usage_total(result['usage'])}), flush=True)
            current = report(root, plan)
            reason = stop_reason(current, plan)
            if reason:
                atomic_json(root / 'stop.json', {'reason': reason, 'next_position': None})
            return current | ({'stop_reason': reason} if reason else {})
        finally:
            if config is not None:
                atomic_json(root / 'credential-source-after.json',
                            {'config_unchanged': sha(config.file.read_bytes()) == credential_hash, 'credential_persisted': False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'report'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--original-root', type=Path)
    parser.add_argument('--upstream', type=Path)
    parser.add_argument('--credential-home', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.root, args.original_root, args.upstream)
    elif args.action == 'run':
        result = asyncio.run(run(args.root, args.credential_home))
    else:
        plan = load(args.root / 'plan.json')
        verify(args.root, plan)
        reconcile(args.root, plan)
        result = report(args.root, plan)
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
