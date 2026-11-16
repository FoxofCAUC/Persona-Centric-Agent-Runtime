"""只读核对E0封口、上游快照和固定材料；不加载实例或派发模型。"""
import argparse
import hashlib
import json
from pathlib import Path


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def source_digest(data):
    """源码按Git文本换行比较；对话、主稿和上游快照仍逐字节校验。"""
    return _digest(data.replace(b'\r\n', b'\n'))


def _load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('path_outside_selected_root')
    return path


def check_core(repo, baseline):
    errors = []
    seen = set()
    for item in baseline['files']:
        name = item['path']
        path = _inside(repo, name)
        if name in seen:
            errors.append({'code': 'duplicate_frozen_path', 'path': name})
        seen.add(name)
        if not path.is_file() or source_digest(path.read_bytes()) != item['sha256']:
            errors.append({'code': 'frozen_core_changed', 'path': name})
    allowed = set(baseline['allowed_new_core_files_e0'])
    for directory in ('persona_runtime', 'scripts', 'templates'):
        for path in (repo / directory).rglob('*'):
            if path.is_file() and path.suffix in ('.py', '.json', '.bat', '.ps1'):
                name = path.relative_to(repo).as_posix()
                if name not in seen and name not in allowed:
                    errors.append({'code': 'unregistered_core_extension', 'path': name})
    return {'passed': not errors, 'frozen_files': len(seen), 'baseline_commit': baseline['commit'], 'errors': errors}


def check_upstream(root, ledger):
    errors = []
    for item in ledger['verified_files']:
        path = _inside(root, item['path'])
        if not path.is_file():
            errors.append({'code': 'upstream_missing', 'path': item['path']})
            continue
        data = path.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if _digest(data) != item['sha256'] or blob != item['blob']:
            errors.append({'code': 'upstream_hash_mismatch', 'path': item['path']})
    for component in ledger['components']:
        path = _inside(root, component['source'])
        if not path.is_file():
            continue
        lines = len(path.read_text(encoding='utf-8-sig').splitlines())
        for location in component['locations']:
            if not (0 < location['start'] <= location['end'] <= lines):
                errors.append({'code': 'upstream_location_invalid', 'component': component['id'], 'name': location['name']})
    return {'passed': not errors, 'commit': ledger['commit'], 'verified_files': len(ledger['verified_files']),
            'components': len(ledger['components']), 'errors': errors}


def validate_material(material, case, fixtures):
    """校验单份冻结输入，错误信息只含材料ID及错误码。"""
    errors = []
    ident = case['id']

    def expect(ok, code):
        if not ok:
            errors.append({'code': code, 'case': ident})

    expect(material.get('version') == 'expression-e1-input-snapshot-v1', 'material_version_mismatch')
    expect(material.get('case_id') == ident and material.get('source_kind') == case['source_kind'], 'material_identity_mismatch')
    expect(material.get('persona') == case['persona'], 'material_persona_mismatch')
    body = material.get('draft')
    expect(isinstance(body, str) and bool(body.strip()), 'material_empty_draft')
    if not isinstance(body, str):
        return errors
    expect(_digest(body.encode('utf-8')) == material.get('draft_sha256'), 'material_draft_hash_mismatch')
    expect(material.get('candidate_ids') == case['candidate_ids'], 'material_candidates_changed')
    actual = case['source_kind'] == 'approved_first_v8_reply'
    expect(material.get('provider_generated') is actual, 'material_provenance_mismatch')
    end = 0
    ids = set()
    for span in material.get('protected', []):
        valid = (type(span.get('start')) is int and type(span.get('end')) is int and
                 end <= span['start'] < span['end'] <= len(body))
        expect(valid, 'material_protected_range_invalid')
        expect(span.get('span_id') not in ids, 'material_protected_id_duplicate')
        ids.add(span.get('span_id'))
        if valid:
            end = span['end']
    if actual:
        expect(material.get('origin', {}).get('result_sha256') == case.get('origin_result_sha256'), 'material_original_receipt_changed')
    else:
        fixture = next((c for c in fixtures['cases'] if c['id'] == ident), None)
        expect(fixture is not None, 'public_fixture_missing')
        if fixture:
            expect(body == fixture['draft'] and material.get('user') == fixture['user'], 'public_fixture_text_changed')
            expect(material.get('role_projection') == fixtures['personas'][fixture['persona']], 'public_fixture_role_changed')
            protected = {s['span_id']: body[s['start']:s['end']] for s in material.get('protected', [])
                         if type(s.get('start')) is int and type(s.get('end')) is int}
            expect(protected == {s['id']: s['text'] for s in fixture['protect']}, 'public_fixture_protected_text_changed')
    return errors


def check_materials(repo, root=None):
    directory = repo / 'experiments/expression_e0'
    fixtures = _load(directory / 'public_fixtures.json')
    manifest = _load(directory / 'materials_manifest.json')
    plan = _load(directory / 'call_plan.json')
    errors = []

    def expect(ok, code):
        if not ok:
            errors.append({'code': code})

    cases = manifest['cases']
    ids = [case['id'] for case in cases]
    expect(len(cases) == 24 and len(set(ids)) == 24, 'case_count_or_identity_invalid')
    expect(sum(c['source_kind'] == 'approved_first_v8_reply' for c in cases) == 16, 'actual_draft_count_invalid')
    expect(sum(c['source_kind'] == 'authored_public_fixture' for c in cases) == 8, 'authored_draft_count_invalid')
    diagnostic = manifest['diagnostic_case_ids']
    expect(len(diagnostic) == len(set(diagnostic)) == 8 and set(diagnostic) <= set(ids), 'diagnostic_cases_invalid')
    positions = manifest['positions']
    expected = {(ident, 'C') for ident in ids} | {(ident, 'C0') for ident in diagnostic}
    expect(len(positions) == 32 and len({p['id'] for p in positions}) == 32 and
           {(p['case_id'], p['arm']) for p in positions} == expected, 'call_positions_invalid')
    expect(plan['positions'] == {'C_with_assets': 24, 'C_without_assets_diagnostic': 8,
           'new_primary': 0, 'paid_selector': 0, 'paid_splitter': 0, 'total_physical_calls_max': 32}, 'finite_budget_changed')
    expect(plan['e0_paid_calls'] == 0 and plan['failure_budget']['automatic_retry_per_position'] == 0, 'call_replay_policy_changed')
    expect(plan['provider'] == 'deepseek_official' and plan['model'] == 'deepseek-flash' and plan['tools'] == [], 'expression_provider_or_tools_changed')
    expect(fixtures['provider_generated'] is False, 'authored_fixture_mislabeled')
    expect({f['id'] for f in fixtures['cases']} == {c['id'] for c in cases if c['store'] == 'public_fixture'}, 'public_fixture_set_changed')
    asset_map = {a['id']: a for a in fixtures['assets']}
    for case in cases:
        for ident in case['candidate_ids']:
            expect(ident in asset_map and asset_map[ident]['persona'] == case['persona'], 'candidate_persona_mismatch')
    count = 0
    originals = 0
    if root is not None:
        for case in cases:
            path = _inside(root, case['file'])
            if not path.is_file():
                errors.append({'code': 'material_missing', 'case': case['id']})
                continue
            data = path.read_bytes()
            if _digest(data) != case['sha256']:
                errors.append({'code': 'material_file_hash_mismatch', 'case': case['id']})
                continue
            material = json.loads(data.decode('utf-8-sig'))
            errors.extend(validate_material(material, case, fixtures))
            count += 1
            origin = material.get('origin', {})
            original = Path(origin['raw_result_path']) if origin.get('raw_result_path') else None
            if original is not None and original.is_file():
                if _digest(original.read_bytes()) != origin['result_sha256']:
                    errors.append({'code': 'original_receipt_file_changed', 'case': case['id']})
                else:
                    originals += 1
    return {'passed': not errors, 'development_cases': len(cases), 'diagnostic_cases': len(diagnostic),
            'expression_positions': len(positions), 'local_materials_checked': count,
            'original_receipts_checked': originals, 'errors': errors}


def inspect(repo, *, materials_root=None, upstream=None):
    baseline = _load(repo / 'docs/expression/E0-core-baseline.json')
    ledger = _load(repo / 'docs/expression/E0-upstream-reuse.json')
    materials = _load(repo / 'experiments/expression_e0/materials_manifest.json')
    checks = {'core': check_core(repo, baseline), 'materials': check_materials(repo, materials_root)}
    if upstream is not None:
        checks['upstream'] = check_upstream(upstream, ledger)
    matching = materials['baseline_commit'] == baseline['commit']
    return {'version': 'expression-e0-preflight-v1', 'passed': matching and all(c['passed'] for c in checks.values()),
            'baseline_commit_matches_materials': matching,
            'full_e0_inputs_checked': materials_root is not None and upstream is not None,
            'checks': checks, 'paid_calls': 0, 'instance_loaded': False, 'computer_use_opened': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--materials-root', type=Path)
    parser.add_argument('--upstream', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        if args.output is not None and args.output.resolve().is_relative_to(repo):
            raise ValueError('output_must_be_outside_source_repo')
        report = inspect(repo, materials_root=args.materials_root, upstream=args.upstream)
    except (OSError, ValueError, KeyError, TypeError):
        report = {'version': 'expression-e0-preflight-v1', 'passed': False, 'error': 'preflight_inputs_invalid',
                  'paid_calls': 0, 'instance_loaded': False}
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output is not None and not args.output.resolve().is_relative_to(repo):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
