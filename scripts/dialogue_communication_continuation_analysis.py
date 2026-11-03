"""Read-only input analysis for the separately authorized CR3-A.1 continuation.

No clients, generation, or old report writers are imported. Outputs belong only
to the continuation package; the original stopped batch stays immutable.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import encoded, fingerprint
from persona_runtime.config import atomic_json

OUT = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-continuation')
PARENT = OUT.with_name('CR3-A.1-real-consumption-ablation')
VERSION = 'cr3-continuation-analysis-v1'
ASSESSMENT_VERSION = 'cr3-continuation-assessment-v1'
DIMENSIONS = ('not_customer_service', 'not_mechanical', 'im_naturalness')
SAMPLE_CATEGORIES = ('natural_borrow', 'fitting_reuse', 'forced', 'fact_transfer', 'not_observable')
AUXILIARY_COVERAGE = ('frame', 'required_content', 'state_constraints', 'gap_policy', 'allowed_optional', 'excluded_inferences')


def require(value, code):
    if not value:
        raise ValueError('cr31c_' + code)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output_hash(reply):
    """The authored reading binds the actual raw supplier string, not its layout."""
    return hashlib.sha256(reply.encode('utf-8')).hexdigest()


def answer(value):
    try:
        return '\n\n'.join(x['content'] for x in json.loads(value['reply'])['messages'])
    except (KeyError, ValueError, TypeError):
        return value['reply']


def confined(root):
    root = Path(root).resolve()
    require(root.is_relative_to(OUT.resolve()), 'output_outside_continuation')
    require(not (OUT / 'closure.json').exists(), 'continuation_sealed')
    return root


def _pointer(root, item, allowed):
    """An import is a verified read pointer; it is never rewritten locally."""
    require(isinstance(item, dict), 'import_pointer')
    path = Path(item['path'])
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    require(path.is_relative_to(allowed.resolve()), 'import_outside_parent')
    require(digest_file(path) == item['sha256'], 'import_changed')
    return path


def _imports(root):
    manifest = read(root / 'imports.json')
    parent = Path(manifest.get('parent_root', manifest.get('source_root', ''))).resolve()
    require(parent == (PARENT / 'real-v1').resolve(), 'wrong_parent')
    if manifest.get('parent_closure'):
        _pointer(root, manifest['parent_closure'], parent.parent)
    return manifest, parent


def load_bundle(root):
    """Combine references and current records without losing receipt provenance."""
    root = Path(root).resolve()
    manifest, parent = _imports(root)
    # A continuation prepares only remaining positions; its complete plan is the
    # original frozen plan and must not be inferred from completed output.
    prepared = read(parent / 'prepared.json')
    current_prepared = read(root / 'prepared.json')
    protocol = read(root / 'protocol.json') if (root / 'protocol.json').exists() else read(parent / 'protocol.json')
    require(protocol == read(parent / 'protocol.json'), 'protocol_changed')
    materials = read(root / 'materials.json') if (root / 'materials.json').exists() else read(parent / 'materials.json')
    require(materials == read(parent / 'materials.json'), 'materials_changed')
    records, auxiliary, origins = {}, {}, {}
    for category, target in (('results', records), ('auxiliary', auxiliary)):
        entries = manifest.get(category, {})
        if isinstance(entries, list):
            entries = {entry.get('id', entry.get('group', Path(entry['path']).stem)): entry for entry in entries}
        require(isinstance(entries, dict), 'import_collection')
        for ident, pointer in entries.items():
            path = _pointer(root, pointer, parent)
            value = read(path)
            require(ident == path.stem and (category != 'results' or value['id'] == ident), 'import_identity')
            target[ident] = value
            origins[(category, ident)] = parent
        for path in sorted((root / category).glob('*.json')):
            ident, value = path.stem, read(path)
            require(ident not in target, 'successful_position_replayed')
            if category == 'results':
                require(value['id'] == ident, 'result_identity')
            target[ident] = value
            origins[(category, ident)] = root
    receipts = {}
    for origin in (parent, root):
        batch_path = origin / 'prepared.json'
        batch = read(batch_path).get('batch', origin.name)
        for path in sorted((origin / 'receipts').glob('*.reserve.json')):
            ident = path.name.split('.')[0]
            qualified = str(origin) + '::' + str(batch) + '::' + ident
            row = {'origin_root': str(origin), 'batch': batch, 'receipt_id': ident,
                   'qualified_id': qualified, 'reserve': read(path)}
            for part in ('transport', 'terminal'):
                file = path.with_name(ident + '.' + part + '.json')
                row[part] = read(file) if file.exists() else None
            if row['terminal']:
                require(fingerprint(row['reserve']) == row['terminal']['reservation_hash'], 'receipt_binding')
                require(row['reserve']['request_hash'] == row['terminal']['runtime_attempt']['request_hash'], 'attempt_binding')
                require(row['reserve']['real'] is read(batch_path)['real'], 'receipt_mode')
            receipts[qualified] = row
    real = current_prepared['real']
    for category, values in (('results', records), ('auxiliary', auxiliary)):
        for ident, value in values.items():
            expected = prepared['real'] if origins[(category, ident)] == parent else real
            require(value.get('real') is expected and value.get('simulation') is not expected, 'mixed_record_modes')
    return {'root': root, 'parent': parent, 'imports': manifest, 'prepared': prepared,
            'protocol': protocol, 'materials': materials, 'results': records,
            'auxiliary': auxiliary, 'origins': origins, 'receipts': receipts, 'real': real}


def receipt(bundle, category, ident, call):
    origin = str(bundle['origins'][(category, ident)])
    rows = [r for r in bundle['receipts'].values() if r['origin_root'] == origin and r['receipt_id'] == call]
    require(len(rows) == 1, 'receipt_reference')
    return rows[0]


def canonical(bundle, ident):
    value = bundle['results'][ident]
    if value['status'] != 'aliased':
        require(value['status'] == 'completed', 'incomplete_result')
        return value
    target = bundle['results'].get(value['canonical_id'])
    require(target and target['status'] == 'completed' and target['group'] == value['group'], 'alias_target')
    require(all(value[k] == target[k] for k in ('request_hash', 'messages', 'reply')), 'alias_not_identical')
    require(value.get('actual_manifest_from') == target['id'], 'alias_manifest')
    require(bundle['origins'][('results', ident)] == bundle['origins'][('results', target['id'])], 'alias_origin')
    return target


def provider_parameters(model, messages):
    """Compare the deterministic supplier payload, never per-attempt deadlines."""
    from persona_runtime.provider_compat import build_request
    body, _ = build_request(model, messages)
    return {key: value for key, value in body.items() if key != 'messages'}


def response_rows(bundle):
    rows = []
    for ident, value in sorted(bundle['results'].items()):
        actual = canonical(bundle, ident)
        call = receipt(bundle, 'results', actual['id'], actual['calls'][-1])
        terminal, transport = call['terminal'], call['transport']
        require(terminal and terminal['status'] == 'completed' and transport, 'successful_receipt_missing')
        require(call['reserve']['request_hash'] == value['request_hash'], 'request_receipt_changed')
        require(call['reserve']['messages'] == value['messages'], 'messages_receipt_changed')
        require(transport['raw'] == value['reply'], 'reply_receipt_changed')
        require(fingerprint(value['messages']) == actual['manifest']['input_snapshot_hash'], 'manifest_changed')
        parameters = provider_parameters(call['reserve']['model'], call['reserve']['messages'])
        require(fingerprint(parameters | {'messages': value['messages']}) == value['request_hash'], 'provider_wire_changed')
        diagnostics = transport.get('diagnostics') or {}
        rows.append({'id': ident, 'canonical_id': actual['id'], 'group': value['group'], 'condition': value['condition'],
                     'status': value['status'], 'origin_root': str(bundle['origins'][('results', ident)]),
                     'request_hash': value['request_hash'], 'message_hash': fingerprint(value['messages']),
                     'model': diagnostics.get('response_model'), 'backend': diagnostics.get('system_fingerprint'),
                     'reply': answer(value), 'raw_output_hash': output_hash(value['reply']),
                     'projection': value['view']['projection'], 'material': value['view']['character'],
                     'qualified_calls': [] if value['status'] == 'aliased' else
                     [receipt(bundle, 'results', ident, x)['qualified_id'] for x in value['calls']],
                     'physical_attempts': 0 if value['status'] == 'aliased' else len(value['calls']),
                     'model_parameters': parameters})
    return rows


def usage_summary(bundle):
    summaries = {}
    for scope, origin in (('original', bundle['parent']), ('continuation', bundle['root']), ('combined', None)):
        rows = [r for r in bundle['receipts'].values() if origin is None or r['origin_root'] == str(origin)]
        usage = {'known_calls': 0, 'unknown_calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        for row in rows:
            value = (row['transport'] or {}).get('usage')
            if not isinstance(value, dict):
                usage['unknown_calls'] += 1
                continue
            usage['known_calls'] += 1
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if type(value.get(key)) is int:
                    usage[key] += value[key]
        slots = defaultdict(list)
        for row in rows:
            slots[(row['origin_root'], row['reserve']['slot'])].append(row)
        successful = [sorted(v, key=lambda x: x['reserve']['ordinal']) for v in slots.values()
                      if any((r['terminal'] or {}).get('status') == 'completed' for r in v)]
        summaries[scope] = {'physical_calls': len(rows), 'completed_calls': sum((r['terminal'] or {}).get('status') == 'completed' for r in rows),
                            'real_physical_calls': sum(r['reserve']['real'] is True for r in rows),
                            'simulation_physical_calls': sum(r['reserve']['real'] is False for r in rows),
                            'failed_calls': sum((r['terminal'] or {}).get('status') == 'failed' for r in rows),
                            'unsettled_calls': sum(not r['terminal'] for r in rows), 'usage': usage,
                            'first_attempt_successful_positions': sum(len(v) == 1 for v in successful),
                            'native_retry_recovered_positions': sum(len(v) > 1 for v in successful),
                            'native_retry_attempts': sum(max(0, len(v) - 1) for v in slots.values()),
                            'money': 'unknown_not_estimated'}
    old_failed = {r['reserve']['slot'] for r in bundle['receipts'].values()
                  if r['origin_root'] == str(bundle['parent']) and (r['terminal'] or {}).get('status') == 'failed'}
    recovered = sorted({r['reserve']['slot'] for r in bundle['receipts'].values()
                        if r['origin_root'] == str(bundle['root']) and r['reserve']['slot'] in old_failed
                        and (r['terminal'] or {}).get('status') == 'completed'})
    return {'scopes': summaries, 'user_authorized_new_window_recoveries': recovered,
            'original_failures_retained': True, 'continuation_is_not_an_automatic_retry': True,
            'routing': 'configured gateway grok-4.7; not official direct xAI', 'backend_does_not_prove_determinism': True}


def _authored(document):
    if isinstance(document, dict):
        require(document.get('version') == ASSESSMENT_VERSION, 'assessment_version')
        document = document['assessments']
    require(isinstance(document, list), 'assessment_collection')
    return document


def _reason_excerpt(row, text, *, empty=False):
    require(isinstance(row.get('reason'), str) and bool(row['reason'].strip()), 'assessment_reason')
    excerpt = row.get('excerpt')
    require(isinstance(excerpt, str) and (bool(excerpt) or empty) and excerpt in text, 'assessment_excerpt')


def validate_main(document, bundle):
    assessed = {}
    for row in _authored(document):
        ident = row['id']
        require(ident not in assessed and ident in bundle['results'], 'assessment_identity')
        value = canonical(bundle, ident)
        require(value['id'] == ident, 'alias_duplicate_assessment')
        require(row['output_hash'] == output_hash(value['reply']), 'assessment_output_changed')
        text = answer(value)
        require(set(row['dimensions']) == set(DIMENSIONS), 'assessment_dimensions')
        for judgment in row['dimensions'].values():
            require(type(judgment.get('score')) is int and 1 <= judgment['score'] <= 5, 'score_range')
            _reason_excerpt(judgment, text)
        for key, allowed in (('task', ('satisfied', 'partial', 'not_satisfied', 'unable')),
                             ('boundary', ('respected', 'violated', 'unable'))):
            require(row[key].get('status') in allowed, 'semantic_status')
            _reason_excerpt(row[key], text, empty=row[key]['status'] == 'unable')
        require(row['sample_use'].get('category') in SAMPLE_CATEGORIES, 'sample_category')
        _reason_excerpt(row['sample_use'], text, empty=row['sample_use']['category'] == 'not_observable')
        assessed[ident] = row
    return assessed


def _source_text(bundle, group):
    case_id = group.split('-')[0]
    case = next(x for x in bundle['materials']['cases'] if x['id'] == case_id)
    parts = list(case['members'])
    for pair in case['history']:
        parts.extend(pair)
    if isinstance(case.get('reference'), str):
        parts.append(case['reference'])
    return '\n'.join(parts)


def validate_auxiliary(document, bundle):
    assessed = {}
    for row in _authored(document):
        group = row['group']
        require(group not in assessed and group in bundle['auxiliary'], 'auxiliary_assessment_identity')
        parsed = bundle['auxiliary'][group]['parsed']
        require(row['result_hash'] == fingerprint(parsed), 'auxiliary_assessment_changed')
        items = row.get('critical_items')
        require(isinstance(items, list) and bool(items), 'auxiliary_items')
        require(len({x['key'] for x in items}) == len(items), 'auxiliary_item_duplicate')
        require(all(isinstance(x.get('covers'), list) and bool(x['covers']) for x in items), 'auxiliary_coverage_missing')
        require({key for x in items for key in x['covers']} == set(AUXILIARY_COVERAGE), 'auxiliary_coverage_incomplete')
        parsed_text = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        source_text = _source_text(bundle, group)
        for item in items:
            require(item.get('status') in ('correct', 'incorrect', 'missing', 'unable'), 'auxiliary_item_status')
            require(isinstance(item.get('reason'), str) and bool(item['reason'].strip()), 'auxiliary_item_reason')
            source, quote = item.get('input_excerpt', ''), item.get('frame_excerpt', '')
            require(isinstance(source, str) and isinstance(quote, str) and bool(source or quote), 'auxiliary_evidence_missing')
            require(not source or source in source_text, 'auxiliary_input_excerpt')
            require(not quote or quote in parsed_text or quote in encoded(parsed), 'auxiliary_frame_excerpt')
        assessed[group] = row
    return assessed


def task_error(assessment):
    if not assessment or assessment['task']['status'] == 'unable' or assessment['boundary']['status'] == 'unable':
        return None
    return assessment['task']['status'] != 'satisfied' or assessment['boundary']['status'] == 'violated'


def _without_projection(value):
    messages = deepcopy(value['messages'])
    block = value['view']['projection']
    if block:
        matches = [(i, m['content'].count(block)) for i, m in enumerate(messages) if block in m['content']]
        require(sum(n for _, n in matches) == 1, 'projection_not_unique')
        i = matches[0][0]
        messages[i]['content'] = messages[i]['content'].replace(block, '', 1)
    return messages


def comparisons(bundle, rows, assessments, authored_pairs=None):
    indexed = {x['id']: x for x in rows}
    readings = {}
    for item in authored_pairs or []:
        key = (item['group'], item['left'], item['right'])
        require(key not in readings, 'pair_duplicate')
        require(item.get('outcome') in ('left', 'tie', 'right', 'unable'), 'pair_outcome')
        require(isinstance(item.get('reason'), str) and bool(item['reason'].strip()), 'pair_reason')
        for side in ('left', 'right'):
            result = bundle['results'].get(item['group'] + '-' + item[side])
            require(result is not None, 'pair_result_missing')
            quote = item.get(side + '_excerpt')
            require(isinstance(quote, str) and bool(quote) and quote in answer(result), 'pair_excerpt')
        readings[key] = item
    comparisons = []
    for position in bundle['prepared']['positions']:
        if len(position['conditions']) != 3:
            continue
        group = position['id']
        for left, right, kind in (('full', 'raw', 'primary'), ('full', 'essential', 'secondary'), ('essential', 'raw', 'exploratory')):
            a, b = (bundle['results'].get(group + '-' + c) for c in (left, right))
            row = {'group': group, 'kind': kind, 'left': left, 'right': right, 'complete': bool(a and b), 'verdict': 'unable'}
            comparisons.append(row)
            if not a or not b:
                row['reason'] = 'incomplete_pair'
                continue
            x, y = indexed[a['id']], indexed[b['id']]
            aux = bundle['auxiliary'][group]
            aux_receipt = receipt(bundle, 'auxiliary', group, aux['calls'][-1])
            ad = (aux_receipt['transport'] or {}).get('diagnostics') or {}
            same = a['request_hash'] == b['request_hash']
            explained = (_without_projection(a) == _without_projection(b) and x['model_parameters'] == y['model_parameters']
                         and all(a['view'].get(k) == b['view'].get(k) for k in ('shared_evidence_hash', 'mother_spec_hash', 'character')))
            known = bool(x['model'] and x['backend'] and y['model'] and y['backend'])
            coherent = known and (x['model'], x['backend']) == (y['model'], y['backend'])
            ca, cb = assessments.get(x['canonical_id']), assessments.get(y['canonical_id'])
            row.update(identical_request=same, deduplicated=x['canonical_id'] == y['canonical_id'],
                       explained_consumption_only=explained, main_backend_known=known, main_backend_equal=coherent,
                       shared_auxiliary_hash=fingerprint(aux['parsed']), shared_auxiliary_model=ad.get('response_model'),
                       shared_auxiliary_backend=ad.get('system_fingerprint'),
                       shared_auxiliary_calls=[receipt(bundle, 'auxiliary', group, c)['qualified_id'] for c in aux['calls']],
                       left_request_hash=a['request_hash'], right_request_hash=b['request_hash'], causal_proof=False,
                       attribution='identical_input_no_consumption_gain' if same else 'unexplained_difference' if not explained else
                       'controlled_consumption_difference_not_determinism' if coherent else 'consumption_difference_backend_limited')
            if row['deduplicated']:
                row.update(verdict='tie', reason='same physical result; not independent quality observations')
            elif ca and cb:
                ce, be = task_error(ca), task_error(cb)
                delta = ca['dimensions']['im_naturalness']['score'] - cb['dimensions']['im_naturalness']['score']
                reading = readings.get((group, left, right))
                # Dimension numbers are descriptive, never a substitute for an
                # authored whole-context comparison.
                verdict = ('unable' if not reading or ce is None or be is None else 'loss' if ce and not be else 'win' if be and not ce
                           else 'unable' if ce and be else {'left': 'win', 'right': 'loss', 'tie': 'tie', 'unable': 'unable'}[reading['outcome']])
                row.update(verdict=verdict, im_delta=delta, left_task_or_boundary_error=ce, right_task_or_boundary_error=be,
                           left_excerpt=ca['dimensions']['im_naturalness']['excerpt'], right_excerpt=cb['dimensions']['im_naturalness']['excerpt'],
                           reason=reading['reason'] if reading else 'whole-context pair reading pending; dimension difference is not a verdict',
                           pair_read=bool(reading), method='authored whole-context machine reading; task/boundary precedence; no keyword scoring')
                if reading:
                    row.update(left_excerpt=reading['left_excerpt'], right_excerpt=reading['right_excerpt'], authored_outcome=reading['outcome'])
            else:
                row['reason'] = 'actual full-output reading pending; no invented score'
    require(set(readings) <= {(r['group'], r['left'], r['right']) for r in comparisons}, 'pair_unknown')
    return comparisons


def review_plan(bundle):
    plan = bundle['protocol']['review']
    require(len(plan) == 12 and {p['number'] for p in plan} == set(range(1, 13)), 'review_plan')
    require(Counter(p['full_side'] for p in plan) == {'A': 6, 'B': 6}, 'review_balance')
    mapping = []
    for item in sorted(plan, key=lambda x: x['number']):
        group = f"{item['case_id']}-{item['role']}-r{item['replicate']}"
        full, raw = group + '-full', group + '-raw'
        a, b = bundle['results'].get(full), bundle['results'].get(raw)
        same = bool(a and b and a['request_hash'] == b['request_hash'])
        mapping.append(item | {'group': group, 'candidate': full, 'control': raw,
                               'A': full if item['full_side'] == 'A' else raw,
                               'B': raw if item['full_side'] == 'A' else full,
                               'complete': bool(a and b), 'identical_request': same,
                               'vote_required': bool(a and b and not same)})
    return mapping


def resolve_user_review(rows, mapping, results):
    indexed, resolved = {}, []
    for row in rows:
        number, choice = row['number'], row.get('choice', row.get('answer'))
        require(type(number) is int and 1 <= number <= 12 and number not in indexed, 'user_review_number')
        require(choice in ('A', 'B', '平', '无法判断', 'tie', 'unable'), 'user_review_choice')
        indexed[number] = row | {'choice': choice}
    for pair in mapping:
        row = indexed.get(pair['number'])
        if row is None:
            if pair['complete'] and pair['identical_request']:
                resolved.append(pair | {'user_choice': None, 'user_reason': None, 'chosen_position': None,
                                        'user_candidate_verdict': 'no_vote_identical_input',
                                        'is_user_opinion': False})
            continue
        require(pair['complete'] and pair['A'] in results and pair['B'] in results, 'user_review_missing_result')
        choice = row['choice']
        chosen = pair[choice] if choice in ('A', 'B') else None
        verdict = ('no_vote_identical_input' if pair['identical_request'] else 'win' if chosen == pair['candidate']
                   else 'loss' if chosen == pair['control'] else 'tie' if choice in ('平', 'tie') else 'unable')
        resolved.append(pair | {'user_choice': choice, 'user_reason': row.get('reason'), 'chosen_position': chosen,
                                'user_candidate_verdict': verdict, 'is_user_opinion': not pair['identical_request']})
    return resolved


def review_coverage(mapping, resolved):
    received = sum(r.get('user_choice') is not None for r in resolved)
    exemptions = sum(r['user_candidate_verdict'] == 'no_vote_identical_input' for r in resolved)
    covered = {r['number'] for r in resolved}
    return {'received': received, 'exempt_identical_input': exemptions, 'covered': len(covered),
            'complete': all(p['complete'] and p['number'] in covered for p in mapping)}


def append_user_review(root, rows, *, after_mapping_revealed=False, raw_answer=None):
    """Explicit caller-supplied choices only. Corrections append; originals stay."""
    root = confined(root)
    bundle = load_bundle(root)
    resolve_user_review(rows, review_plan(bundle), bundle['results'])
    folder = root / 'analysis'
    folder.mkdir(exist_ok=True)
    existing = sorted(folder.glob('user-review-[0-9][0-9][0-9].json'))
    path = folder / f'user-review-{len(existing) + 1:03d}.json'
    require(not path.exists(), 'review_append_collision')
    previous = read(existing[-1]).get('rows', []) if existing else []
    effective = {r['number']: r for r in previous}
    effective.update({r['number']: r for r in rows})
    merged = [effective[k] for k in sorted(effective)]
    resolve_user_review(merged, review_plan(bundle), bundle['results'])
    document = {'rows': merged, 'submitted_rows': rows, 'raw_answer': raw_answer, 'after_mapping_revealed': after_mapping_revealed,
                'independent_new_blind_review': False, 'supersedes': existing[-1].name if existing else None}
    atomic_json(path, document)
    return path


def _fenced(lines, text):
    # Four backticks preserve actual code and prevent answer Markdown changing
    # the surrounding anonymous document's structure.
    fence = '`' * max(4, max((len(x) for x in re.findall(r'`+', text)), default=0) + 1)
    lines.extend([fence, text, fence, ''])


def render_review(bundle, mapping):
    lines = ['# CR3-A.1：12组匿名关键对照', '',
             '这是机器开发实验的用户偏好复核，不是多人独立盲审。两个人工角色均为普通朋友、无共同事件，身份与知识相同，仅关注与取舍不同。', '',
             '请按任务完成、接话、人物口气、不客服式、不人机式和即时通讯自然度阅读。自然补白、拟稿礼貌回请、贴切复述或追问本身不扣分。每组可选A、B、平或无法判断，可附原因。', '',
             '完全相同请求只保留一个实际结果，标记“同输入已合并”，无需投票；固定组缺失时不补替代组。', '']
    if not all(p['complete'] for p in mapping):
        lines.extend(['固定12组尚未齐备；当前仅展示留存进度，不发起正式用户审阅。', ''])
    if not bundle['real']:
        lines.extend(['续验记录是模拟输出；本材料仅验证报告功能，不发起真实效果的用户验收。', ''])
    for pair in mapping:
        case = next(x for x in bundle['materials']['cases'] if x['id'] == pair['case_id'])
        lines.extend([f"## {pair['number']:02d}", '', '人物关注：' + bundle['materials']['roles'][pair['role']]['style'], '', '普通前文：', ''])
        _fenced(lines, '\n'.join('用户：' + u + '\n助手：' + a for u, a in case['history']) or '无')
        lines.extend(['本轮有序输入：', ''])
        _fenced(lines, '\n'.join('用户：' + x for x in case['members']))
        if case.get('reference'):
            lines.extend(['本轮含对上面已保存前文的合法明确引用。', ''])
        if not pair['complete']:
            lines.extend(['未完成；不替换、不评分。', ''])
        elif pair['identical_request']:
            lines.extend(['同输入已合并，无需投票。', ''])
            _fenced(lines, answer(bundle['results'][pair['candidate']]))
        else:
            for side in ('A', 'B'):
                lines.extend([side + '：', ''])
                _fenced(lines, answer(bundle['results'][pair[side]]))
    return '\n'.join(lines)


def render(root):
    root = confined(root)
    bundle = load_bundle(root)
    folder = root / 'analysis'
    folder.mkdir(exist_ok=True)
    rows = response_rows(bundle)
    main_document = read(folder / 'authored-main.json') if (folder / 'authored-main.json').exists() else {'version': ASSESSMENT_VERSION, 'assessments': []}
    authored = validate_main(main_document, bundle)
    auxiliary = validate_auxiliary(read(folder / 'authored-auxiliary.json'), bundle) if (folder / 'authored-auxiliary.json').exists() else {}
    pair_readings = main_document.get('comparisons', []) if isinstance(main_document, dict) else []
    pairs, mapping = comparisons(bundle, rows, authored, pair_readings), review_plan(bundle)
    reviews = sorted(folder.glob('user-review-[0-9][0-9][0-9].json'))
    review_document = read(reviews[-1]) if reviews else {'rows': []}
    choices = resolve_user_review(review_document['rows'], mapping, bundle['results'])
    review_status = review_coverage(mapping, choices)
    unique = [r for r in rows if r['status'] == 'completed']
    unsettled = sum(not r['terminal'] for r in bundle['receipts'].values())
    state = read(root / 'state.json') if (root / 'state.json').exists() else {
        'status': 'running_uncheckpointed' if (root / 'run.started.json').exists() else 'not_started'}
    engineering_complete = len(rows) == 76 and len(bundle['auxiliary']) == 24 and not unsettled and state['status'] == 'completed'
    full_ids = [r['id'] for r in rows if r['condition'] == 'full']
    full_tasks = [task_error(authored.get(canonical(bundle, ident)['id'])) for ident in full_ids]
    controls = [task_error(authored.get(r['canonical_id'])) for r in rows if r['group'].split('-')[0] in ('G05', 'G06')]
    user_counts = Counter(x['user_candidate_verdict'] for x in choices)
    nonidentical_positive = [x for x in pairs if x['kind'] == 'primary' and x['complete'] and not x['identical_request']
                            and x['verdict'] == 'win' and x['explained_consumption_only']]
    pair_read_complete = all(p.get('deduplicated') or p.get('pair_read') for p in pairs if p['complete'])
    quality_complete = engineering_complete and len(authored) == len(unique) and len(auxiliary) == 24 and review_status['complete'] and pair_read_complete
    research_signal = bool(bundle['real'] and quality_complete and len(full_tasks) == 24 and all(x is False for x in full_tasks)
                           and len(controls) == 4 and all(x is False for x in controls)
                           and user_counts['win'] > user_counts['loss'] and nonidentical_positive)
    summary = {'version': VERSION, 'real': bundle['real'], 'logical_main_conditions': len(rows), 'unique_main_outputs': len(unique),
               'planned_main_conditions': 76, 'main_conditions_pending': 76 - len(rows),
               'aliases': len(rows) - len(unique), 'auxiliary_outputs': len(bundle['auxiliary']),
               'engineering_complete': engineering_complete, 'execution_state': state['status'], 'machine_unique_main_read': len(authored),
               'machine_main_read_pending': len(unique) - len(authored), 'machine_auxiliary_read': len(auxiliary),
               'auxiliary_semantic_items': dict(Counter(x['status'] for a in auxiliary.values() for x in a['critical_items'])),
               'auxiliary_coverage_categories': list(AUXILIARY_COVERAGE),
               'full_task_boundary_observed': len(full_tasks), 'full_task_boundary_planned': 24,
               'full_task_boundary_errors': sum(x is True for x in full_tasks),
               'full_task_boundary_unread_or_unable': 24 - len(full_tasks) + sum(x is None for x in full_tasks),
               'control_task_boundary_errors': sum(x is True for x in controls), 'control_unread_or_unable': sum(x is None for x in controls),
               'whole_context_pair_readings': len(pair_readings), 'whole_context_pair_read_complete': pair_read_complete,
               'user_fixed_groups': 12, 'user_groups_ready': sum(p['complete'] for p in mapping),
               'user_vote_required': sum(p['vote_required'] for p in mapping), 'user_review_received': review_status['received'],
               'user_review_identical_input_exempt': review_status['exempt_identical_input'],
               'user_review_plan_coverage': review_status['covered'], 'user_review_complete': review_status['complete'],
               'user_verdicts': dict(user_counts), 'user_review_after_mapping_revealed': review_document.get('after_mapping_revealed', False),
               'quality_analysis_complete': quality_complete, 'research_signal': research_signal,
               'conclusion': 'quality_analysis_pending' if not quality_complete else 'supports_next_research_only' if research_signal else 'no_stable_research_signal',
               'real_quality_verified': bool(bundle['real'] and quality_complete), 'release_approved': False,
               'naturalness_method': 'authored full-output readings with exact evidence; not keyword/length/punctuation scoring',
               'sample_effect_is_not_proven_by_output_difference': True,
               'comparisons': {kind: dict(Counter(x['verdict'] for x in pairs if x['kind'] == kind)) for kind in ('primary', 'secondary', 'exploratory')},
               'dimension_means_unique_outputs': {d: statistics.mean(x['dimensions'][d]['score'] for x in authored.values()) if authored else None for d in DIMENSIONS}}
    for name, value in (('responses', rows), ('comparisons', pairs), ('usage', usage_summary(bundle)),
                        ('review-mapping-private', {'fixed_before_run': True, 'disclose_after_user_response': True, 'groups': mapping}),
                        ('user-review-resolved', choices), ('summary', summary)):
        atomic_json(folder / (name + '.json'), value)
    (folder / 'CR3-A.1-12组匿名审核.md').write_text(render_review(bundle, mapping), encoding='utf-8')
    reading = ['# 实际主回复完整阅读索引', '', '真实输出' if bundle['real'] else '原批次真实片段与续验模拟分别保留；模拟只验证工具，不作为真实质量证据', '']
    for row in unique:
        reading.extend(['## ' + row['id'], ''])
        _fenced(reading, row['reply'])
    (folder / 'all-unique-main-reading.md').write_text('\n'.join(reading), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('report',))
    parser.add_argument('--root', type=Path, default=OUT / 'real-v1')
    args = parser.parse_args()
    print(encoded(render(args.root)))


if __name__ == '__main__':
    main()
