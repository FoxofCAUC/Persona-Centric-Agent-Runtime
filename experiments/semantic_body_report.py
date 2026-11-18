"""Build two blinded comparisons from stored bodies, without a provider connection."""
import argparse
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from statistics import median

from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import TrialLedger, parse_receipt
from .corpus_reply_trial_report import PRICE_URL, estimated_peak, save_text_once
from .semantic_body_comparison import ReviewedSources, complete_receipt, file_hash, frozen_json, require
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint


def stored_body(ledger, slot):
    row = ledger.row(slot)
    body, usage = None, None
    if row['raw_response'] is not None:
        require(digest(row['raw_response']) == row['raw_hash'], 'result_bytes_changed')
        parsed = parse_receipt(row['raw_response'], json.loads(row['request_json']).get('stream', False))
        if row['result_json'] is not None:
            require(parsed == json.loads(row['result_json']), 'parsed_body_changed')
        usage = parsed['usage']
        if row['state'] == 'completed':
            require(parsed['valid'] and parsed['model'] == 'deepseek-v4-pro' and row['http_status'] == 200,
                    'invalid_body_response')
            body = parsed['body']
    diagnostics = json.loads(row['diagnostics_json'] or '{}')
    return {'slot': slot, 'state': row['state'], 'body': body, 'usage': usage, 'ms': diagnostics.get('elapsed_ms'),
            'raw_hash': row['raw_hash'], 'request_hash': row['request_hash'], 'http_status': row['http_status'],
            'error': row['error_code'], 'attempted': row['attempt_id'] is not None}


def pair(case, control, side):
    candidate = case['bodies']['S']
    baseline = case['bodies'][control]
    ready = candidate['state'] == baseline['state'] == 'completed'
    sides = [candidate, baseline] if side == 'left' else [baseline, candidate]
    return {'left': sides[0]['body'], 'right': sides[1]['body'], 'ready': ready,
            'same': ready and candidate['body'] == baseline['body'],
            'same_request': candidate['request_hash'] == baseline['request_hash'],
            'states': [r['state'] for r in sides]}


def export(decision_path):
    sources = ReviewedSources(decision_path)
    root = sources.output
    manifest_path = root / 'manifest.private.json'
    manifest_sha = file_hash(manifest_path)
    plan = read_private(manifest_path)
    key = frozen_json(root / 'blind-key.private.json', plan['blind_key_sha256'])
    ledger = TrialLedger(root / 'receipts')
    require(plan['max_new_pro_requests'] == 0 or ledger.path.is_file(), 'new_results_not_available')
    cases, new_receipts, archived_responses = [], [], {}
    component_sums, baseline_ms = [], []
    for row in plan['positions']:
        n = row['position']
        packet = frozen_json(root / row['file'], row['sha256'])
        require(packet == sources.position_packet(n), 'result_sources_changed')
        bodies = {}
        for k in ('B', 'C', 'D'):
            previous, _, result = complete_receipt(sources.old, n, k)
            bodies[k] = {'body': result['body'], 'state': 'completed', 'slot': previous['slot'],
                         'raw_hash': previous['raw_hash'], 'request_hash': previous['request_hash'],
                         'ms': json.loads(previous['diagnostics_json'] or '{}').get('elapsed_ms')}
        if row['reuse']:
            bodies['S'] = bodies[row['reuse']]
        else:
            bodies['S'] = stored_body(ledger, f'{n:02}.semantic')
            require(bodies['S']['request_hash'] == row['request_hash'], 'unexpected_result_request')
            new_receipts.append(bodies['S'])
            raw = ledger.row(bodies['S']['slot'])['raw_response']
            if raw is not None:
                archived_responses[n] = raw
        u_ms = json.loads(sources.old.row(f'{n:02}.U')['diagnostics_json'] or '{}').get('elapsed_ms')
        selector_ms = read_private(sources.selector_root / f'selector-calls/{n:02}/response-meta.private.json')['diagnostics'].get('elapsed_ms')
        if all(isinstance(t, (int, float)) for t in (u_ms, selector_ms, bodies['S']['ms'])):
            component_sums.append(u_ms + selector_ms + bodies['S']['ms'])
        if bodies['B']['ms'] is not None:
            baseline_ms.append(bodies['B']['ms'])
        case = {'position': n, 'input': sources.positions[n]['input_text'], 'reuse': row['reuse'],
                'selections': row['selected_ids'], 'bodies': bodies, 'sources': packet['references'],
                'selection_receipt': packet['selection_receipt'], 'sides': key[str(n)]}
        case['pairs'] = {k: pair(case, k, key[str(n)][k]) for k in ('C', 'B')}
        cases.append(case)
    attempted = [r for r in new_receipts if r['attempted']]
    usage = [r['usage'] for r in attempted if r['usage'] and type(r['usage'].get('total_tokens')) is int]
    times = [r['ms'] for r in attempted if r['ms'] is not None]
    done = sum(r['state'] == 'completed' for r in new_receipts)
    costs = [estimated_peak('D', r['usage']) for r in attempted]
    summary = {'positions': len(cases), 'new_completed': done, 'new_requested': plan['max_new_pro_requests'],
               'new_attempts': len(attempted), 'reused': sum(bool(c['reuse']) for c in cases),
               'known_tokens': sum(u['total_tokens'] for u in usage), 'unknown_usage_count': len(attempted) - len(usage),
               'prompt_tokens_known': sum(u['prompt_tokens'] for u in usage if 'prompt_tokens' in u),
               'completion_tokens_known': sum(u['completion_tokens'] for u in usage if 'completion_tokens' in u),
               'peak_estimate_CNY': str(sum((Decimal(cost) for cost in costs), Decimal(0))) if None not in costs else None,
               'price_reference': PRICE_URL, 'pricing_date': '2026-11-18',
               'new_request_median_ms': median(times) if times else None, 'actual_cost': None,
               'historical_baseline_median_ms': median(baseline_ms) if baseline_ms else None,
               'three_stage_sum_median_ms': median(component_sums) if len(component_sums) == len(cases) else None,
               'different_pairs': {k: sum(c['pairs'][k]['ready'] and not c['pairs'][k]['same'] for c in cases)
                                   for k in ('B', 'C')}}
    result_hash = fingerprint(cases)
    report = root / ('review' if done == len(new_receipts) else 'partial-' + result_hash[:12])
    data = {'version': 'semantic-body-results-v1', 'summary': summary, 'cases': cases,
            'manifest_sha256': manifest_sha, 'result_set_hash': result_hash, 'blind_key_sha256': plan['blind_key_sha256']}
    save_once(report / 'results.private.json', data)
    save_once(report / 'summary.private.json', summary)
    for n, raw in archived_responses.items():
        file = report / 'receipts' / f'{n:02}.response.private.json'
        file.parent.mkdir(exist_ok=True)
        if file.exists():
            require(file.read_bytes() == raw, 'receipt_export_conflict')
        else:
            with file.open('xb') as target:
                target.write(raw)
    save_text_once(report / 'evidence.html', evidence_page(cases, sources, summary))
    # The blind view receives only the ordered texts and their comparison status.
    display = {k: data[k] for k in ('summary', 'manifest_sha256', 'result_set_hash', 'blind_key_sha256')}
    display['cases'] = [{k: c[k] for k in ('position', 'input', 'pairs')} for c in cases]
    template = Path(__file__).with_name('semantic_body_review.html').read_text(encoding='utf-8')
    serialized = json.dumps(display, ensure_ascii=False).replace('<', '\\u003c')
    save_text_once(report / 'index.html', template.replace('@@BODY_DATA@@', serialized))
    plain = []
    for control, title in [('C', '与无库正文比较'), ('B', '与原v8 Pro比较')]:
        plain.append(title)
        for case in cases:
            item = case['pairs'][control]
            plain.extend([f"位置 {case['position']}", case['input']])
            if item['same']:
                plain.extend(['两份正文相同', item['left']])
            else:
                plain.extend(['甲', item['left'] if item['left'] is not None else item['states'][0],
                              '乙', item['right'] if item['right'] is not None else item['states'][1]])
    save_text_once(report / '匿名正文全文.txt', '\n\n'.join(plain) + '\n')
    return report, summary


def evidence_page(cases, sources, summary):
    assets = {a['id']: a for a in sources.plan['assets']['assets']}
    rows = []
    for case in cases:
        n = case['position']
        reuse = case['reuse']
        receipt = (f'../../../review-v2/{n:02}.{reuse}.response.private.txt' if reuse
                   else f'receipts/{n:02}.response.private.json')
        selection = '\n\n'.join(assets[i]['situation'] + '\n' + assets[i]['style'] for i in case['selections'])
        receipt_link = (f'<a href="{receipt}">正文首次回执</a>' if case['bodies']['S']['raw_hash'] else '正文回执尚未取得')
        reveal = '，'.join(f'与{label}相比时新稿为' + ('甲' if case['sides'][k] == 'left' else '乙')
                          for k, label in [('C', '无库稿'), ('B', '原v8 Pro')])
        rows.append(f'<details><summary>位置 {n} · ' + ('沿用无库稿' if reuse == 'C' else '沿用原有库稿' if reuse else '新请求') +
                    '</summary><p>' + escape(reveal) + '</p><pre>' + escape(selection or '该位置选材为空') + '</pre>'
                    + f'<p><a href="../requests/{n:02}.private.json">冻结请求及依赖</a> · {receipt_link} · '
                    + f'<a href="../../../review-v2/{n:02}.U.response.private.txt">共享理解</a> · '
                    + f'<a href="../../../review-v2/{n:02}.C.response.private.txt">无库对照</a> · '
                    + f'<a href="../../../review-v2/{n:02}.B.response.private.txt">原v8 Pro对照</a></p></details>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>选材与回执</title><style>body{max-width:60em;margin:1.4em auto;padding:0 1em;color:#23352d;'
            'font:16px/1.7 system-ui,sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{padding:.7em 0;'
            'border-bottom:1px solid #ddd}summary{cursor:pointer}a{color:#315e48}</style><main><a href="index.html">返回盲审</a>'
            '<h1>逐位选材与回执</h1><p>展开位置可查看本轮采用的表达、两组的甲乙身份及首次回执。</p>'
            '<p>本轮新增用量 ' + str(summary['known_tokens']) + ' tokens。按高峰单价估算人民币 '
            + escape(summary['peak_estimate_CNY'] or '未知') + ' 元，实际账单待核对。'
            + f'<a href="{PRICE_URL}">DeepSeek官方价格</a></p><p><a href="summary.private.json">用量与分段耗时</a></p>'
            + ''.join(rows) + '</main></html>')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection-approval', type=Path, required=True)
    path, summary = export(parser.parse_args().selection_approval)
    print(json.dumps({'report': str(path / 'index.html'), **summary}, ensure_ascii=False))
