"""Verbatim upstream expression-selector candidate, pending separate approval.

Preparation has no credentials or provider access. A bounded run requires an
explicit approval receipt bound to this manifest; original reply calls stay put.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.provider_compat import build_request
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import authority_guard, parse_receipt, public_model
from .corpus_scene_selection import load_materials


VERSION = 'verbatim-maibot-selector-review-v1'


def parse_selected_ids(body, candidates, maximum=5):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise CorpusError('semantic_selector_invalid_json') from None
    if not isinstance(value, dict) or set(value) != {'selected_ids'} or not isinstance(value['selected_ids'], list):
        raise CorpusError('semantic_selector_invalid_schema')
    selected = value['selected_ids']
    if (len(selected) > maximum or any(type(i) is not int for i in selected)
            or len(set(selected)) != len(selected) or not set(selected) <= set(candidates)):
        raise CorpusError('semantic_selector_invalid_candidate_ids')
    return selected


def prepare(study_root, root):
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    study_root, root = outside_repository(study_root), outside_repository(root)
    old = read_private(study_root / 'plan.private.json')
    corpus = DatasetCorpusStore(Path(old['corpus_root']))
    learning = CorpusLearning(Path(old['learning_root']), corpus)
    plan, sources, messages, excluded, understandings, guard = load_materials(study_root, corpus, learning)
    source = read_private(root / 'upstream-source.private.json')
    raw_template = (root / 'maibot-selector-original.prompt').read_bytes()
    import hashlib
    blob = hashlib.sha1(b'blob ' + str(len(raw_template)).encode() + b'\0' + raw_template).hexdigest()
    if (source['commit'] != '478f671e64467121fd3e39205c8d3507b5ce0bd6'
            or source['path'] != 'prompts/zh-CN/main/maisaka_expression_selector.prompt'
            or blob != source['git_blob_sha1']):
        raise CorpusError('semantic_selector_upstream_source_changed')
    assets = sorted(plan['assets']['assets'], key=lambda a: a['id'])
    numbers = {n: a['id'] for n, a in enumerate(assets, 1)}
    # Exact candidate-line format from MaiBot._build_selector_prompt.
    lines = [f"{n}: 情景={a['situation']} | 风格={a['style']}" for n, a in enumerate(assets, 1)]
    system = raw_template.decode('utf-8-sig').format(max_selected=5, candidates='\n'.join(lines))
    model = public_model('deepseek-flash', .2)
    model.update(max_tokens=512, _json_output=True)
    packets = []
    for p in plan['positions']:
        ordinal = p['position']
        moment = datetime.fromisoformat(plan['captures'][str(ordinal)]['accepted_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        time_text = plan['binding']['variables']['time_block']['prefix'] + moment
        understanding = understandings[ordinal]
        reference = plan['binding']['variables']['planner_reasoning']['format'].format(thought=understanding['body'])
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': time_text},
                    {'role': 'user', 'content': p['input_text']}, {'role': 'user', 'content': reference}]
        request = build_request(model, messages)[0]
        packet = {'position': ordinal, 'input_hash': p['input_text_hash'], 'understanding_response_hash': understanding['response_hash'],
                  'candidate_numbers': numbers, 'model': model, 'request': request, 'request_hash': fingerprint(request),
                  'model_use_approved': False, 'status': 'pending_author_review'}
        filename = f'{ordinal:02}.selector-request.private.json'
        save_once(root / filename, packet)
        packets.append({'position': ordinal, 'path': filename, 'sha256': digest((root / filename).read_bytes()),
                        'request_hash': packet['request_hash']})
    manifest = {'version': VERSION, 'date': '2026-11-17', 'status': 'pending_new_selector_and_budget_approval',
                'study_root': str(study_root), 'study_plan_sha256': digest((study_root / 'plan.private.json').read_bytes()),
                'upstream_source': source, 'upstream_prompt_sha256': digest(raw_template),
                'candidate_numbers': numbers, 'candidate_pool': len(numbers), 'max_selected': 5,
                'proposed_max_new_selector_requests': 16, 'existing_reply_requests_reused': 80,
                'proposed_new_reply_requests': 0, 'driver_llm_calls_this_preparation': 0,
                'current_route_allows_extra_selection_model': False, 'route_change_pending_author_review': True,
                'new_model_visible_prose_authored': False, 'model_use_approved': False, 'formal_activation': False,
                'model': model, 'packets': packets, 'paid_selector_estimate_before_usage': None,
                'input_scope': 'verbatim upstream system and numbered situation/style; actual current input, original accepted time and saved Flash facts; no C/D output, author votes or learning source quotes',
                'failure_policy': 'first attempt and complete receipt only; invalid or unavailable selection means no expression assets, never random or lexical fallback'}
    save_once(root / 'selector-manifest.private.json', manifest)
    return manifest


def approved_manifest(root, approval_path):
    root = outside_repository(root)
    approval = read_private(approval_path)
    manifest = read_private(root / 'selector-manifest.private.json')
    if (manifest.get('version') != VERSION or len(manifest.get('packets', [])) != 16
            or {p['position'] for p in manifest['packets']} != set(range(1, 17))
            or approval.get('version') != 'semantic-selector-author-use-v1' or approval.get('max_dispatches') != 16
            or approval.get('manifest_sha256') != digest((root / 'selector-manifest.private.json').read_bytes())
            or approval.get('allow_extra_selection_model') is not True
            or approval.get('model_use_approved') is not True
            or approval.get('reply_regeneration_authorized') is not False):
        raise CorpusError('semantic_selector_new_author_approval_required')
    for p in manifest['packets']:
        if digest((root / p['path']).read_bytes()) != p['sha256']:
            raise CorpusError('semantic_selector_frozen_packet_changed')
    binding_path = Path(approval_path).with_suffix('.run-binding.private.json')
    binding = {'root': str(root), 'manifest_sha256': approval['manifest_sha256'],
               'approval_sha256': digest(Path(approval_path).read_bytes()), 'max_dispatches': 16}
    if binding_path.exists():
        if (read_private(binding_path) != binding or not (root / 'selector-calls').is_dir()
                or not (root / 'selector-calls/index.private.json').is_file()):
            raise CorpusError('semantic_selector_original_call_ledger_required')
    else:
        if (root / 'selector-calls').exists():
            raise CorpusError('semantic_selector_unbound_calls_refused')
        save_once(binding_path, binding)
        (root / 'selector-calls').mkdir()
        save_once(root / 'selector-calls/index.private.json', {'version': 1, 'reserved_positions': []})
    return manifest


async def run_approved(root, approval_path, credentials, client=None):
    from persona_runtime.corpus_learning_transport import CorpusLearningHTTPClient
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    root = outside_repository(root)
    manifest = approved_manifest(root, approval_path)
    plan = read_private(Path(manifest['study_root']) / 'plan.private.json')
    corpus = DatasetCorpusStore(Path(plan['corpus_root']))
    learning = CorpusLearning(Path(plan['learning_root']), corpus)
    guard = authority_guard(plan, corpus, learning)
    client = client or CorpusLearningHTTPClient()
    results = []
    for p in manifest['packets']:
        approved_manifest(root, approval_path)
        n = p['position']
        folder = root / 'selector-calls' / f'{n:02}'
        attempt_path = folder / 'attempt.private.json'
        result_path = folder / 'outcome.private.json'
        raw_path, raw_meta_path = folder / 'response.private.bytes', folder / 'response-meta.private.json'
        packet = read_private(root / p['path'])
        index_path = root / 'selector-calls/index.private.json'
        index = read_private(index_path)
        if n in index['reserved_positions'] and not attempt_path.exists():
            raise CorpusError('semantic_selector_original_attempt_required')
        if result_path.exists():
            result = read_private(result_path)
        elif attempt_path.exists():
            # A crash with missing response metadata is unknown; never resend.
            if raw_path.exists() and raw_meta_path.exists():
                result = finalize_selection(packet, raw_path.read_bytes(), read_private(raw_meta_path))
            else:
                result = {'position': n, 'state': 'unknown', 'selected_ids': [], 'usage': None, 'error': 'interrupted_without_complete_receipt'}
            save_once(result_path, result)
        else:
            guard({'position': n})
            live = packet['model'] | {'encrypted_key': credentials.get('encrypted_key', '')}
            if credentials.get('base_url', '').rstrip('/') != live['base_url']:
                raise CorpusError('semantic_selector_official_credential_origin_required')
            client.preflight(live, packet['request'])
            if len(index['reserved_positions']) >= 16 or n in index['reserved_positions']:
                raise CorpusError('semantic_selector_budget_or_identity_conflict')
            from persona_runtime.config import atomic_json
            atomic_json(index_path, {'version': 1, 'reserved_positions': [*index['reserved_positions'], n]})
            folder.mkdir()
            save_once(attempt_path, {'position': n, 'attempt_id': str(uuid4()), 'request_hash': packet['request_hash'],
                                     'approval_sha256': digest(Path(approval_path).read_bytes())})
            def save_raw(raw, *, http_status, diagnostics):
                if raw_path.exists() and raw_path.read_bytes() != raw:
                    raise CorpusError('semantic_selector_first_receipt_conflict')
                if not raw_path.exists():
                    with raw_path.open('xb') as f:f.write(raw)
                save_once(raw_meta_path, {'http_status': http_status, 'diagnostics': diagnostics, 'raw_sha256': digest(raw)})
            try:
                await client.complete(live, packet['request'], save_raw=save_raw)
                result = finalize_selection(packet, raw_path.read_bytes(), read_private(raw_meta_path))
            except Exception as error:
                if raw_path.exists() and raw_meta_path.exists():
                    result = finalize_selection(packet, raw_path.read_bytes(), read_private(raw_meta_path))
                else:
                    d = getattr(error, 'diagnostics', {})
                    unsent = d.get('request_headers_sent') is False and d.get('response_headers_received') is False
                    result = {'position': n, 'state': 'failed' if unsent else 'unknown', 'selected_ids': [], 'usage': None,
                              'error': 'selection_transport_interrupted', 'diagnostics': d}
            save_once(result_path, result)
            guard({'position': n})
            approved_manifest(root, approval_path)
        results.append(result)
        if result['state'] != 'completed':
            break
    return {'selector_calls': len(read_private(root / 'selector-calls/index.private.json')['reserved_positions']),
            'new_reply_calls': 0, 'results': results}


def finalize_selection(packet, raw, metadata):
    if digest(raw) != metadata['raw_sha256']:
        raise CorpusError('semantic_selector_response_changed')
    parsed = parse_receipt(raw)
    result = {'position': packet['position'], 'state': 'failed', 'selected_ids': [], 'usage': parsed.get('usage'),
              'actual_model': parsed.get('model'), 'raw_sha256': metadata['raw_sha256'], 'http_status': metadata['http_status']}
    if metadata['http_status'] == 200 and parsed['valid']:
        try:
            chosen = parse_selected_ids(parsed['body'], {int(k): v for k, v in packet['candidate_numbers'].items()})
            result.update(state='completed', selected_ids=chosen,
                          selected_proposal_ids=[packet['candidate_numbers'].get(str(i), packet['candidate_numbers'].get(i)) for i in chosen])
        except CorpusError:
            result['error'] = 'invalid_selection_ids_or_json'
    else:
        result['error'] = 'invalid_selection_response'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run'))
    parser.add_argument('--study-root', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--credential-home', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        if not args.study_root:parser.error('study_root_required')
        manifest = prepare(args.study_root, args.root)
        result = {'packets': len(manifest['packets']), 'pending_author_review': True, 'new_driver_llm_calls': 0}
    else:
        if not args.approval or not args.credential_home:parser.error('new_explicit_approval_and_credential_home_required')
        from persona_runtime.config import ConfigStore
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.instance import InstanceLock
        lock = InstanceLock(args.root / 'selector-runner.lock')
        try:
            approved_manifest(args.root, args.approval)
            credentials = ConfigStore(RuntimePaths(args.credential_home)).load()['model']
            result = asyncio.run(run_approved(args.root, args.approval, credentials))
        finally:lock.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':raise SystemExit(main())
