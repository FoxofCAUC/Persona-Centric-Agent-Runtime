"""Offline preparation of one prompt-block-order hypothesis.

Only ``prepare`` and read-only ``verify`` exist. Built-in artificial material
uses a non-routable example endpoint; this tool never loads a configured model,
private role, generation client, or deleted comparison package.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.companion_private_reference import split_minimal_request
from persona_runtime.archive import fingerprint
from persona_runtime.companion import GENERATION_PROFILE, MINIMAL_INSTRUCTION, choose
from persona_runtime.config import atomic_json
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME
from persona_runtime.private_personas import safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import expression_snapshot, prompt_contract
from persona_runtime.turn_input import render_input

VERSION = 'companion-block-order-v1'
EVIDENCE_PARENT = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-3-block-order'
CONTROL_ORDER = ('persona_instruction', 'material', 'format_contract')
VARIANT_ORDER = ('format_contract', 'persona_instruction', 'material')
SECRET_FIELDS = frozenset(('encrypted_key', 'api_key', 'admin_token', 'bridge_token',
                          'authorization', 'proxy_authorization', 'password', 'access_token',
                          'refresh_token', 'api-key', 'x-api-key'))
REQUIRED_REQUEST_FIELDS = frozenset(('messages', 'model', 'wire', 'messages_hash', 'wire_hash'))
REQUEST_FIELDS = REQUIRED_REQUEST_FIELDS | {'transport', 'kind', 'model_hash'}
INTERNAL_MODEL_FIELDS = frozenset(('_companion_profile', '_generation_profile', '_reply_mode',
                                   '_expression_snapshot', '_dialogue_profile'))
SOURCE_FILES = (Path(__file__), ROOT / 'persona_runtime/companion.py',
                ROOT / 'persona_runtime/provider_compat.py', ROOT / 'persona_runtime/reply_batch.py',
                ROOT / 'experiments/companion_private_reference.py')


@contextmanager
def offline_guard():
    error = RuntimeErrorBase('block_order_network_disabled')
    with patch('socket.socket.connect', side_effect=error), \
         patch('socket.socket.connect_ex', side_effect=error), \
         patch('socket.create_connection', side_effect=error), \
         patch('socket.getaddrinfo', side_effect=error):
        yield


def _reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeErrorBase('block_order_record_invalid')
            if key.lower() in SECRET_FIELDS:
                raise RuntimeErrorBase('block_order_credentials_forbidden')
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)


def _request(messages, model):
    wire, transport = build_request(model, messages)
    return {'kind': 'artificial_offline', 'messages': deepcopy(messages), 'model': deepcopy(model),
            'wire': deepcopy(wire), 'transport': transport, 'messages_hash': fingerprint(messages),
            'wire_hash': fingerprint(wire), 'model_hash': fingerprint(model)}


def _validated_source(record):
    if (type(record) is not dict or not REQUIRED_REQUEST_FIELDS <= set(record)
            or set(record) - REQUEST_FIELDS):
        raise RuntimeErrorBase('block_order_record_invalid')
    _reject_credentials(record)
    if type(record.get('model')) is not dict:
        raise RuntimeErrorBase('block_order_record_invalid')
    if any(key.startswith('_') and key not in INTERNAL_MODEL_FIELDS for key in record['model']):
        raise RuntimeErrorBase('block_order_unknown_model_profile')
    # Even an apparently public URL cannot smuggle a key into saved evidence.
    endpoint = urlsplit(record['model'].get('base_url', ''))
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise RuntimeErrorBase('block_order_credentials_forbidden')
    parts = split_minimal_request(record)
    expected_wire, expected_transport = build_request(record['model'], record['messages'])
    if record['wire'] != expected_wire:
        raise RuntimeErrorBase('block_order_stored_wire_mismatch')
    if 'transport' in record and record['transport'] != expected_transport:
        raise RuntimeErrorBase('block_order_transport_mismatch')
    if 'model_hash' in record and record['model_hash'] != fingerprint(record['model']):
        raise RuntimeErrorBase('block_order_model_hash_mismatch')
    return parts


def transform_request(record):
    """Reorder exact existing blocks; do not render or rewrite their text."""
    with offline_guard():
        parts = _validated_source(record)
        blocks = {'persona_instruction': MINIMAL_INSTRUCTION,
                  'material': parts['material_text'], 'format_contract': parts['output_contract']}
        control = deepcopy(record)
        variant = deepcopy(record)
        original_system = record['messages'][0]['content']
        if original_system != ''.join(blocks[name] for name in CONTROL_ORDER):
            raise RuntimeErrorBase('block_order_source_layout_mismatch')
        variant['messages'][0]['content'] = ''.join(blocks[name] for name in VARIANT_ORDER)
        variant['wire'], variant_transport = build_request(variant['model'], variant['messages'])
        variant['messages_hash'] = fingerprint(variant['messages'])
        variant['wire_hash'] = fingerprint(variant['wire'])
        changed_system = variant['messages'][0]['content']
        invariants = {
            'control_unchanged': control == record,
            'model_snapshot_unchanged': variant['model'] == control['model'],
            'history_and_current_unchanged': variant['messages'][1:] == control['messages'][1:],
            'message_roles_unchanged': [m['role'] for m in variant['messages']] ==
                                       [m['role'] for m in control['messages']],
            'wire_parameters_unchanged': {k: v for k, v in variant['wire'].items() if k != 'messages'} ==
                                         {k: v for k, v in control['wire'].items() if k != 'messages'},
            'transport_unchanged': variant_transport == build_request(control['model'], control['messages'])[1],
            'character_count_unchanged': len(original_system) == len(changed_system),
            'utf8_byte_count_unchanged': len(original_system.encode('utf-8')) == len(changed_system.encode('utf-8')),
            'character_multiset_unchanged': Counter(original_system) == Counter(changed_system),
        }
        if not all(invariants.values()):
            raise RuntimeErrorBase('block_order_unexplained_difference')
        return {'version': VERSION, 'control': control, 'variant': variant,
                'diagnostic': {'control_order': list(CONTROL_ORDER), 'variant_order': list(VARIANT_ORDER),
                    'blocks': [{'name': name, 'characters': len(blocks[name]),
                                'utf8_bytes': len(blocks[name].encode('utf-8')),
                                'sha256_utf8': hashlib.sha256(blocks[name].encode('utf-8')).hexdigest()}
                               for name in CONTROL_ORDER],
                    'invariants': invariants, 'only_changed_field': 'messages[0].content',
                    'difference': 'Exact format block moved before exact persona instruction and material blocks; no new text.',
                    'real_model_calls': 0, 'naturalness': 'not_evaluated',
                    'viewpoint_and_character_presence': 'not_evaluated'}}


def artificial_cases():
    """New small developer fixtures, unrelated to any private trial text."""
    model = {'base_url': 'https://offline.invalid/v1', 'name': 'grok-4.7',
             'max_tokens': 8192, 'timeout': 240, 'max_input_chars': 24000, 'hourly_limit': 0,
             '_companion_profile': choose('minimal'), '_generation_profile': dict(GENERATION_PROFILE),
             '_reply_mode': 'natural_structured',
             '_expression_snapshot': expression_snapshot('natural_structured', version=3)}
    material = json.dumps({'name': '人工角色林简', 'identity': '普通朋友，资料仅用于离线开发',
                           'expression_style': '习惯先留意具体细节，再说出自己在意的取舍。'}, ensure_ascii=False)
    material += '\n关系说明：刚认识的普通朋友，没有共同事件。'
    material += '\n人物补充资料：\n人工背景：喜欢修理旧收音机。'
    material += '\n人工表达样例：我想先弄清它为什么断断续续，再决定换不换。'
    protocol = prompt_contract('natural_structured', model['_expression_snapshot'])
    system = MINIMAL_INSTRUCTION + material + protocol
    cases = [
        ('O01', 'daily_sharing', [], '刚才看见有人给窗台上的番茄苗打了一把小伞。'),
        ('O02', 'ordered_correction', [], render_input([
            {'id': 'artificial-member-01', 'text': '帮我想几种重新布置书桌的办法。'},
            {'id': 'artificial-member-02', 'text': '改口，今天不想动它了，就随便聊聊吧。'},
        ])),
        ('O03', 'continuing_history', [
            {'role': 'user', 'content': '旧收音机能发出声音了，不过有时还会沙沙响。'},
            {'role': 'assistant', 'content': '能响是个进展，我更想知道沙沙声什么时候出现。'},
        ], '只在转旋钮的时候有声音。你会先看哪一处？'),
    ]
    return [{'id': case_id, 'category': category, 'material_source': 'new_artificial_developer_fixture',
             'request': _request([{'role': 'system', 'content': system}] + history +
                                 [{'role': 'user', 'content': current}], model)}
            for case_id, category, history, current in cases]


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare():
    """Create only a new artificial package in the fixed evidence parent."""
    with offline_guard():
        parent = safe(EVIDENCE_PARENT)
        parent.mkdir(parents=True, exist_ok=True)
        root = safe(parent / identifier(), parent)
        root.mkdir()
        rows = []
        for case in artificial_cases():
            output = transform_request(case['request'])
            name = case['id'] + '.json'
            atomic_json(root / name, {'case_id': case['id'], 'category': case['category'],
                        'material_source': case['material_source'], **output})
            rows.append({'id': case['id'], 'file': name, 'sha256': _hash_file(root / name)})
        manifest = {'version': VERSION, 'kind': 'artificial_offline', 'created_at': timestamp(),
                    'case_count': len(rows), 'cases': rows, 'real_model_calls': 0,
                    'network': 'blocked', 'private_data_read': False,
                    'current_provider_config_loaded': False,
                    'scope': 'Only prompt block order; all stored model parameters, materials, messages and protocol text otherwise unchanged.',
                    'real_effect': 'not_evaluated', 'future_real_execution': 'requires_separate_frozen_conditions',
                    'source_hashes': {str(p.relative_to(ROOT)).replace('\\', '/'): _hash_file(p)
                                      for p in SOURCE_FILES}}
        atomic_json(root / 'manifest.json', manifest)
        atomic_json(root / 'freeze.json', {'version': VERSION, 'manifest_hash': fingerprint(manifest)})
        verify(root)
        return root


def verify(package):
    """Read-only verification; old or private packages are not valid inputs."""
    with offline_guard():
        root = safe(package, safe(EVIDENCE_PARENT))
        if not re.fullmatch(r'[0-9a-f-]{36}', root.name):
            raise RuntimeErrorBase('block_order_package_scope_invalid')
        for path in root.rglob('*'):
            safe(path, root)
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        freeze = json.loads((root / 'freeze.json').read_text(encoding='utf-8'))
        if (manifest.get('version') != VERSION or manifest.get('kind') != 'artificial_offline'
                or freeze != {'version': VERSION, 'manifest_hash': fingerprint(manifest)}
                or manifest.get('real_model_calls') != 0 or manifest.get('private_data_read') is not False
                or manifest.get('case_count') != 3 or len(manifest.get('cases', [])) != 3):
            raise RuntimeErrorBase('block_order_package_invalid')
        if (set(manifest.get('source_hashes', {})) != {str(p.relative_to(ROOT)).replace('\\', '/') for p in SOURCE_FILES}
                or {row.get('id') for row in manifest['cases']} != {'O01', 'O02', 'O03'}):
            raise RuntimeErrorBase('block_order_package_invalid')
        fixtures = {case['id']: case for case in artificial_cases()}
        for name, expected in manifest['source_hashes'].items():
            path = safe(ROOT / name, ROOT)
            if _hash_file(path) != expected:
                raise RuntimeErrorBase('block_order_implementation_changed')
        for row in manifest['cases']:
            if row.get('file') != row.get('id') + '.json' or row.get('id') not in ('O01', 'O02', 'O03'):
                raise RuntimeErrorBase('block_order_package_invalid')
            path = safe(root / row['file'], root)
            if _hash_file(path) != row['sha256']:
                raise RuntimeErrorBase('block_order_artifact_changed')
            case = json.loads(path.read_text(encoding='utf-8'))
            if (case.get('control') != fixtures[row['id']]['request'] or case.get('case_id') != row['id']
                    or case.get('category') != fixtures[row['id']]['category']):
                raise RuntimeErrorBase('block_order_not_builtin_artificial_case')
            expected = transform_request(case['control'])
            if case.get('material_source') != 'new_artificial_developer_fixture' or any(
                    case.get(key) != value for key, value in expected.items()):
                raise RuntimeErrorBase('block_order_case_mismatch')
        return {'version': VERSION, 'status': 'offline_preparation_verified', 'cases': 3,
                'real_model_calls': 0, 'private_data_read': False,
                'naturalness': 'not_evaluated', 'viewpoint_and_character_presence': 'not_evaluated'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('prepare')
    p = sub.add_parser('verify')
    p.add_argument('package', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = prepare()
            result = {**verify(root), 'package': str(root)}
        else:
            result = verify(args.package)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(json.dumps({'status': 'offline_preparation_failed', 'real_model_calls': 0}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
