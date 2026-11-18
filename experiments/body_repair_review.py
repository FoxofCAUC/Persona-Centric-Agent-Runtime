"""Offline, source-bound review of the E.L4 reply repair. No model transport."""
import argparse
from html import escape
import json
from pathlib import Path
from secrets import choice

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


GROUPS = {'B': '原 v8 Pro', 'S': '修复前的语义选材稿'}
LABELS = {'left': '甲', 'right': '乙'}
KEY_VERSION = 'body-repair-side-key-v1'


def checked(condition, code):
    if not condition:
        raise CorpusError('body_repair_review_' + code)


def freeze_sides(root, positions, groups=GROUPS):
    root = outside_repository(root)
    path = root / 'blind-key.private.json'
    numbers = [str(n) for n in positions]
    groups = tuple(groups)
    checked(numbers and len(numbers) == len(set(numbers)), 'invalid_positions')
    checked(groups and len(groups) == len(set(groups)), 'invalid_groups')
    if not path.exists():
        checked(not (root / 'manifest.private.json').exists(), 'original_key_required')
        sides = {n: {k: choice(tuple(LABELS)) for k in groups} for n in numbers}
        save_once(path, {'version': KEY_VERSION, 'positions': sides})
    key = read_private(path)
    checked(key['version'] == KEY_VERSION and set(key['positions']) == set(numbers)
            and all(set(v) == set(groups) and all(s in LABELS for s in v.values())
                    for v in key['positions'].values()), 'invalid_side_key')
    return digest(path.read_bytes())


def body_record(record):
    item = {k: record[k] for k in ('body', 'state', 'raw_hash', 'request_hash', 'slot', 'error',
                                   'error_code', 'http_status')
            if k in record}
    body = item.get('body')
    item.setdefault('state', 'completed' if isinstance(body, str) else 'unknown')
    if item['state'] == 'reused' and isinstance(body, str):
        item['state'] = 'completed'
    ready = item['state'] == 'completed'
    checked(isinstance(body, str) and bool(body.strip()) if ready else body is None, 'invalid_body_state')
    body_hash = digest(body.encode('utf-8')) if ready else None
    checked(record.get('body_sha256', body_hash) == body_hash, 'invalid_body_digest')
    if ready:
        checked(all(isinstance(item.get(k), str) and len(item[k]) == 64
                    and all(c in '0123456789abcdef' for c in item[k]) for k in ('raw_hash', 'request_hash')),
                'missing_receipt_digest')
    item.update(body=body, body_sha256=body_hash)
    return item


def candidate_display(raw, source):
    from .body_repair_display import project_display

    result = {**raw, 'raw_body': raw['body'], 'raw_body_hash': raw['body_sha256'],
              'removed_spans': [], 'presentation': None}
    if raw['state'] == 'completed':
        view = project_display(raw['body'], user_text=source)
        checked(view['body_sha256'] == raw['body_sha256']
                and digest(view['display_body'].encode('utf-8')) == view['display_sha256'],
                'display_digest_changed')
        result.update(body=view['display_body'], body_sha256=view['display_sha256'],
                      removed_spans=view['removed_spans'], presentation={
                          k: view[k] for k in ('version', 'coordinate', 'reason')})
    return result


def evidence_page(records, changed):
    rows = []
    for item in records:
        candidate = item['candidate']
        label = '复用既有回执' if item['reused'] else candidate['state']
        rows.append(f"<details><summary>位置 {item['position']} · {escape(label)}</summary>")
        if candidate['raw_body'] is not None:
            rows.extend(['<h2>模型回执原文</h2><pre><span>', escape(candidate['raw_body']), '</span></pre>',
                         '<h2>候选呈现正文</h2><pre><span>', escape(candidate['body']), '</span></pre>',
                         '<p>原文 SHA256</p><code>', escape(candidate['raw_body_hash']), '</code>',
                         '<p>呈现正文 SHA256</p><code>', escape(candidate['body_sha256']), '</code>'])
            for span in candidate['removed_spans']:
                rows.append(f"<p>原文字符 [{span['start']}, {span['end']})</p><pre>{escape(span['text'])}</pre>")
        else:
            rows.append('<p>未取得完整正文。</p>')
        rows.append('</details>')
    positions = '、'.join(map(str, changed)) or '无'
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>模型原文与实际呈现</title><style>'
            'body{max-width:56rem;margin:auto;padding:1rem;font:16px/1.65 system-ui;color:#253b2d}'
            'summary{cursor:pointer}details{padding:.8rem 0;border-bottom:1px solid #bbb}h2{font-size:1rem}'
            'pre,code{white-space:pre-wrap;overflow-wrap:anywhere}pre{font:inherit;background:#f3f6f1;'
            'padding:.6rem}a{color:#295a43}</style><main><a href="index.html">返回盲审</a>'
            '<h1>模型原文与实际呈现</h1><p>候选在 '+str(len(changed))+' 个位置移除了动作旁白。位置编号为 '
            +positions+'。正文与跨度均可逐位核对。</p><p><a href="results.private.json">完整证据</a> · '
            '<a href="../blind-key.private.json">各组甲乙身份（left 为甲，right 为乙）</a></p>'
            + ''.join(rows) + '</main></html>')


def export_review(output_dir, cases, *, study_id, evidence=None):
    """Compare the candidate display with archived controls. Preserve all original bodies."""
    report = outside_repository(output_dir)
    root = report.parent
    manifest_path = root / 'manifest.private.json'
    manifest = read_private(manifest_path)
    manifest_sha = digest(manifest_path.read_bytes())
    display_source = Path(__file__).with_name('body_repair_display.py')
    display_source_hash = digest(display_source.read_bytes().replace(b'\r\n', b'\n'))
    if 'display_module_sha256' in manifest:
        checked(display_source_hash == manifest['display_module_sha256'], 'display_module_changed')
    numbers = [row['position'] for row in manifest['positions']]
    checked(manifest['version'] == 'reviewed-body-repair-v1' and numbers and
            all(type(n) is int for n in numbers) and len(numbers) == len(set(numbers)), 'invalid_manifest')
    checked([c['position'] for c in cases] == numbers, 'incorrect_case_order')
    groups = manifest.get('control_labels') or GROUPS
    key_hash = freeze_sides(root, numbers, groups)
    checked(manifest['blind_key_sha256'] == key_hash, 'side_key_changed')
    sides = read_private(root / 'blind-key.private.json')['positions']
    public_rows, records, plain = [], [], []
    counts = {g: {'different': 0, 'identical': 0, 'incomplete': 0} for g in groups}
    changed = []
    for case, frozen in zip(cases, manifest['positions']):
        n = case['position']
        checked(case['input'] == frozen['input'] and type(case['reused']) is bool
                and case['reused'] == (frozen['reuse'] is not None), 'case_source_changed')
        raw = body_record(case['candidate'])
        checked(raw.get('request_hash') == frozen['request_hash'], 'request_source_changed')
        if case['reused']:
            original = frozen['reuse']
            checked(raw['body'] == original.get('body') and raw.get('raw_hash') == original.get('raw_hash'),
                    'reused_receipt_changed')
        new = candidate_display(raw, case['input'])
        if new['body'] != raw['body']:
            changed.append(n)
        row = {'position': n, 'input': case['input'], 'pairs': {}}
        details = {'position': n, 'input': case['input'], 'reused': case['reused'], 'candidate': new, 'controls': {}}
        controls = case.get('controls')
        if controls is None:
            controls = {'B': case['baseline'], 'S': case['previous']}
        checked(set(controls) == set(frozen['controls']) == set(groups), 'control_groups_changed')
        for group in groups:
            old = body_record(controls[group])
            checked(old == body_record(frozen['controls'][group]), 'control_body_changed')
            details['controls'][group] = old
            side = sides[str(n)][group]
            ordered = (new, old) if side == 'left' else (old, new)
            ready = all(v['state'] == 'completed' for v in ordered)
            status = 'incomplete' if not ready else 'identical' if new['body'] == old['body'] else 'different'
            counts[group][status] += 1
            row['pairs'][group] = {'status': status, 'sides': [
                {'text': v['body'], 'state': v['state']} for v in ordered]}
        public_rows.append(row)
        records.append(details)
    for group, name in groups.items():
        plain.append('与' + name + '比较')
        for row in public_rows:
            pair = row['pairs'][group]
            plain.extend([f"位置 {row['position']}", row['input']])
            if pair['status'] == 'identical':
                plain.extend(['两稿相同', pair['sides'][0]['text']])
                continue
            for label, entry in zip(LABELS.values(), pair['sides']):
                plain.extend([label, entry['text'] if entry['text'] is not None else f"未取得正文（{entry['state']}）"])
    summary = {'positions': len(cases), 'reused': sum(c['reused'] for c in cases),
               'new_completed': sum(not c['reused'] and c['candidate']['state'] == 'completed' for c in records),
               'presentation_changed_count': len(changed), 'comparisons': counts}
    identities = {'study_id': study_id, 'manifest_sha256': manifest_sha, 'blind_key_sha256': key_hash,
                  'result_set_hash': fingerprint(records)}
    save_once(report / 'results.private.json', {**identities, 'cases': records, 'summary': summary,
                                              'display_module_sha256': display_source_hash,
                                              'presentation_changed_positions': changed, 'evidence': evidence})
    save_text_once(report / '匿名正文全文.txt', '\n\n'.join(plain) + '\n')
    save_text_once(report / 'evidence.html', evidence_page(records, changed))
    payload = {**identities, 'summary': summary, 'groups': groups, 'cases': public_rows}
    template = Path(__file__).with_name('body_repair_review.html').read_text(encoding='utf-8')
    serialized = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c')
    save_text_once(report / 'index.html', template.replace('@@REPAIR_DATA@@', serialized))
    return report, summary


def export_from_run(manifest_path, approval_path):
    from .body_repair_run import load_results

    manifest_path = outside_repository(manifest_path)
    manifest = read_private(manifest_path)
    approval_path = outside_repository(approval_path)
    approval = read_private(approval_path)
    frozen_display = approval.get('display_module_sha256')
    display_source = Path(__file__).with_name('body_repair_display.py')
    checked(frozen_display == digest(display_source.read_bytes().replace(b'\r\n', b'\n')),
            'unapproved_display_module')
    results = load_results(manifest_path, approval_path)
    records = {r['position']: r for r in results['calls']}
    checked(len(records) == len(results['calls']) == len(manifest['positions']), 'missing_run_receipts')
    cases = []
    for p in manifest['positions']:
        n = p['position']
        call = records[n]
        cases.append({'position': n, 'input': p['input'], 'candidate': call,
                      'controls': p['controls'], 'reused': call['reused']})
    root = manifest_path.parent
    folder = 'review'
    if any(c['candidate']['state'] in ('waiting', 'prepared', 'dispatched') for c in cases):
        folder = 'partial-' + fingerprint([body_record(c['candidate']) for c in cases])[:12]
    return export_review(root / folder, cases, study_id=manifest.get('study_id', manifest['version']),
                         evidence={k: v for k, v in results.items() if k != 'calls'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--approval', required=True, type=Path)
    args = parser.parse_args()
    directory, totals = export_from_run(args.manifest, args.approval)
    print(json.dumps({'review': str(directory / 'index.html'), **totals}, ensure_ascii=False))
