"""Offline ablation of language-arrangement clauses in the shared bubble contract.

This module has no generation action. Its public-record transform is reused by
the separately admitted private experiment; built-in packages are artificial
developer material only and cannot verify a private or real-call package.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments import companion_block_order as order
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.companion_contract import REMOVALS, structural_contract
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME
from persona_runtime.private_personas import safe
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import bubble_prompt

VERSION = 'companion-contract-language-v1'
EVIDENCE_PARENT = DEFAULT_HOME / 'exports/acceptance/PCAR-companion-rebuild/stage-3-contract-language'
SOURCE_FILES = (Path(__file__), ROOT / 'experiments/companion_block_order.py',
                ROOT / 'experiments/companion_private_reference.py',
                ROOT / 'persona_runtime/companion.py', ROOT / 'persona_runtime/companion_contract.py',
                ROOT / 'persona_runtime/provider_compat.py',
                ROOT / 'persona_runtime/reply_batch.py', ROOT / 'persona_runtime/bubble_contract.py',
                ROOT / 'persona_runtime/archive.py')
offline_guard = order.offline_guard


def reduced_contract():
    """Delete exactly the frozen language fragments, never rewrite the contract."""
    return structural_contract(bubble_prompt())


def transform_request(record):
    """Keep original block order and every non-treatment input byte unchanged."""
    with offline_guard():
        parts = order._validated_source(record)
        if record['model'].get('_dialogue_profile') is not None:
            raise RuntimeErrorBase('contract_language_unsupported_dialogue_profile')
        original = bubble_prompt()
        if parts['output_contract'] != original:
            raise RuntimeErrorBase('contract_language_unsupported_contract')
        reduced = reduced_contract()
        control, variant = deepcopy(record), deepcopy(record)
        source_system = record['messages'][0]['content']
        prefix = source_system[:-len(original)]
        if source_system != order.MINIMAL_INSTRUCTION + parts['material_text'] + original:
            raise RuntimeErrorBase('contract_language_source_layout_mismatch')
        variant['messages'][0]['content'] = prefix + reduced
        variant['wire'], transport = build_request(variant['model'], variant['messages'])
        variant['messages_hash'] = fingerprint(variant['messages'])
        variant['wire_hash'] = fingerprint(variant['wire'])
        removed_text = ''.join(text for _, text in REMOVALS)
        new_system = variant['messages'][0]['content']
        original_parameters = {k: v for k, v in control['wire'].items() if k != 'messages'}
        new_parameters = {k: v for k, v in variant['wire'].items() if k != 'messages'}
        invariants = {
            'control_unchanged': control == record,
            'model_snapshot_unchanged': variant['model'] == control['model'],
            'history_and_current_unchanged': variant['messages'][1:] == control['messages'][1:],
            'message_roles_unchanged': [m['role'] for m in variant['messages']] ==
                                       [m['role'] for m in control['messages']],
            'persona_and_material_unchanged': new_system == prefix + reduced,
            'block_order_unchanged': new_system.startswith(order.MINIMAL_INSTRUCTION + parts['material_text'])
                                     and new_system.endswith(reduced),
            'wire_parameters_unchanged': new_parameters == original_parameters,
            'response_format_unchanged': variant['wire'].get('response_format') == control['wire'].get('response_format'),
            'reasoning_effort_unchanged': variant['wire'].get('reasoning_effort') == control['wire'].get('reasoning_effort'),
            'transport_unchanged': transport == build_request(control['model'], control['messages'])[1],
            'only_registered_text_removed': Counter(source_system) == Counter(new_system) + Counter(removed_text),
            'removed_character_count_exact': len(source_system) - len(new_system) == len(removed_text),
            'removed_utf8_byte_count_exact': len(source_system.encode('utf-8')) - len(new_system.encode('utf-8')) ==
                                             len(removed_text.encode('utf-8')),
        }
        if not all(invariants.values()):
            raise RuntimeErrorBase('contract_language_unexplained_difference')
        return {'version': VERSION, 'control': control, 'variant': variant,
                'diagnostic': {'invariants': invariants,
                    'control_order': list(order.CONTROL_ORDER), 'variant_order': list(order.CONTROL_ORDER),
                    'original_contract_sha256': hashlib.sha256(original.encode('utf-8')).hexdigest(),
                    'retained_contract_sha256': hashlib.sha256(reduced.encode('utf-8')).hexdigest(),
                    'removed_clauses': [{'name': name, 'characters': len(text),
                                         'sha256_utf8': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                                        for name, text in REMOVALS],
                    'removed_characters': len(removed_text), 'budget_refilled': False,
                    'only_changed_field': 'messages[0].content',
                    'difference': 'Delete only registered bubble-contract language guidance; original material and block order retained.',
                    'real_model_calls': 0, 'naturalness': 'not_evaluated',
                    'viewpoint_and_character_presence': 'not_evaluated'}}


def artificial_cases():
    """Reuse only the public developer fixtures; never load a private package."""
    return order.artificial_cases()


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_hashes():
    return {str(p.relative_to(ROOT)).replace('\\', '/'): _hash_file(p) for p in SOURCE_FILES}


def prepare():
    with offline_guard():
        parent = safe(EVIDENCE_PARENT)
        parent.mkdir(parents=True, exist_ok=True)
        root = safe(parent / identifier(), parent)
        root.mkdir()
        rows = []
        for case in artificial_cases():
            value = transform_request(case['request'])
            file = case['id'] + '.json'
            atomic_json(root / file, {'case_id': case['id'], 'category': case['category'],
                        'material_source': 'public_artificial_developer_fixture', **value})
            rows.append({'id': case['id'], 'file': file, 'sha256': _hash_file(root / file)})
        manifest = {'version': VERSION, 'kind': 'artificial_offline', 'created_at': timestamp(),
                    'case_count': 3, 'cases': rows, 'source_hashes': _source_hashes(),
                    'network': 'blocked', 'real_model_calls': 0, 'private_data_read': False,
                    'current_provider_config_loaded': False, 'budget_refilled': False,
                    'scope': 'Language clauses only; schema, validator, wire parameters and original block order unchanged.',
                    'real_effect': 'not_evaluated', 'future_real_execution': 'requires_separate_frozen_conditions'}
        atomic_json(root / 'manifest.json', manifest)
        atomic_json(root / 'freeze.json', {'version': VERSION, 'manifest_hash': fingerprint(manifest)})
        verify(root)
        return root


def verify(package):
    with offline_guard():
        root = safe(package, safe(EVIDENCE_PARENT))
        if not re.fullmatch(r'[0-9a-f-]{36}', root.name):
            raise RuntimeErrorBase('contract_language_package_scope_invalid')
        for path in root.rglob('*'):
            safe(path, root)
        manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
        freeze = json.loads((root / 'freeze.json').read_text('utf-8'))
        if (manifest.get('version') != VERSION or manifest.get('kind') != 'artificial_offline'
                or freeze != {'version': VERSION, 'manifest_hash': fingerprint(manifest)}
                or manifest.get('real_model_calls') != 0 or manifest.get('private_data_read') is not False
                or manifest.get('case_count') != 3 or len(manifest.get('cases', [])) != 3
                or manifest.get('source_hashes') != _source_hashes()
                or {row.get('id') for row in manifest['cases']} != {'O01', 'O02', 'O03'}):
            raise RuntimeErrorBase('contract_language_package_invalid')
        fixtures = {case['id']: case for case in artificial_cases()}
        for row in manifest['cases']:
            if row.get('file') != row.get('id') + '.json':
                raise RuntimeErrorBase('contract_language_package_invalid')
            path = safe(root / row['file'], root)
            if _hash_file(path) != row['sha256']:
                raise RuntimeErrorBase('contract_language_artifact_changed')
            value = json.loads(path.read_text('utf-8'))
            case = fixtures[row['id']]
            if (value.get('case_id') != case['id'] or value.get('category') != case['category']
                    or value.get('material_source') != 'public_artificial_developer_fixture'
                    or value.get('control') != case['request']):
                raise RuntimeErrorBase('contract_language_not_builtin_artificial_case')
            if any(value.get(key) != item for key, item in transform_request(case['request']).items()):
                raise RuntimeErrorBase('contract_language_case_mismatch')
        return {'version': VERSION, 'status': 'offline_preparation_verified', 'cases': 3,
                'real_model_calls': 0, 'private_data_read': False,
                'naturalness': 'not_evaluated', 'viewpoint_and_character_presence': 'not_evaluated'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('prepare')
    p = sub.add_parser('verify'); p.add_argument('package', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = prepare(); result = {**verify(root), 'package': str(root)}
        else:
            result = verify(args.package)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(json.dumps({'status': 'offline_preparation_failed', 'real_model_calls': 0}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
