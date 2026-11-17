"""Private first-result review; all prose here is UI, never model input."""
import argparse
from collections import Counter
from decimal import Decimal
from html import escape
import json
from pathlib import Path
import secrets
from statistics import median

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import TrialLedger, parse_receipt


LABELS = ('甲', '乙', '丙', '丁')
NAMES = {'A': '原v8 Flash', 'B': '原v8 Pro', 'C': 'Flash理解＋Pro，无表达库', 'D': 'Flash理解＋Pro，有表达库'}
PRICE_URL = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'


def save_text_once(path, value):
    path = outside_repository(path)
    raw = value.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise CorpusError('reply_trial_text_artifact_conflict')
    else:
        with path.open('xb') as file:
            file.write(raw)


def freeze_review_key(root):
    root = outside_repository(root)
    path = root / 'review-key.private.json'
    if path.exists():
        return read_private(path)
    ledger = TrialLedger(root / 'ledger')
    if ledger.status()['attempts']:
        raise CorpusError('reply_trial_review_key_required_before_calls')
    key = {}
    for position in range(1, 17):
        kinds = list('ABCD')
        secrets.SystemRandom().shuffle(kinds)
        key[str(position)] = dict(zip(LABELS, kinds))
    save_once(path, {'version': 'four-condition-review-key-v1', 'positions': key,
                     'ledger_attempts_at_freeze': 0, 'assignment_before_model_results': True})
    return read_private(path)


def estimated_peak(kind, usage):
    if not usage or type(usage.get('prompt_tokens')) is not int or type(usage.get('completion_tokens')) is not int:
        return None
    hit, miss, out = (Decimal('.04'), Decimal('2'), Decimal('8')) if kind in ('A', 'U') else (Decimal('.30'), Decimal('9'), Decimal('27'))
    cached, uncached = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
    cost = (Decimal(cached) * hit + Decimal(uncached) * miss if type(cached) is int and type(uncached) is int
            else Decimal(usage['prompt_tokens']) * miss) + Decimal(usage['completion_tokens']) * out
    return str(cost / Decimal(1000000))


def export(root, output):
    root, output = outside_repository(root), outside_repository(output)
    plan = read_private(root / 'plan.private.json')
    key = read_private(root / 'review-key.private.json')
    ledger = TrialLedger(root / 'ledger')
    output.mkdir(parents=True, exist_ok=True)
    cases, accounting, plain, verification = [], [], [], []
    for position in plan['positions']:
        ordinal = position['position']
        case = {'position': ordinal, 'input': position['input_text'], 'labels': key['positions'][str(ordinal)],
                'choices': {}, 'expression_count': len(position['selection']['selected_ids']),
                'original_context': position['review_only_original_human_context']}
        for kind in ('A', 'B', 'U', 'C', 'D'):
            row = ledger.row(f'{ordinal:02}.{kind}')
            request = json.loads(row['request_json']) if row['request_json'] else None
            result = json.loads(row['result_json']) if row['result_json'] else {}
            if row['raw_response'] is not None:
                if digest(row['raw_response']) != row['raw_hash']:
                    raise CorpusError('reply_trial_report_raw_changed')
                rechecked = parse_receipt(row['raw_response'], (request or {}).get('stream', False))
                if result and result != rechecked:
                    raise CorpusError('reply_trial_report_result_changed')
                raw_path = output / f'{ordinal:02}.{kind}.response.private.txt'
                if raw_path.exists() and raw_path.read_bytes() != row['raw_response']:
                    raise CorpusError('reply_trial_report_receipt_conflict')
                if not raw_path.exists():
                    raw_path.write_bytes(row['raw_response'])
            if request is not None:
                save_once(output / f'{ordinal:02}.{kind}.request.private.json', request)
            item = {'state': row['state'], 'body': result.get('body', ''), 'error': row['error_code'],
                    'http_status': row['http_status'], 'requested_model': json.loads(row['model_json'])['name'],
                    'response_model': result.get('model'), 'usage': result.get('usage'),
                    'elapsed_ms': (json.loads(row['diagnostics_json']) if row['diagnostics_json'] else {}).get('elapsed_ms'),
                    'request_hash': row['request_hash'], 'response_hash': row['raw_hash'],
                    'dependency_response_hash': row['dependency_hash']}
            if kind == 'U':
                case['understanding'] = item
            else:
                case['choices'][kind] = item
            usage = result.get('usage')
            accounting.append({'position': ordinal, 'kind': kind, 'state': row['state'], 'usage': usage,
                               'estimated_peak_CNY': estimated_peak(kind, usage), 'actual_bill_CNY': None})
        c, d, u = (ledger.row(f'{ordinal:02}.{kind}') for kind in ('C', 'D', 'U'))
        paired = bool(c['request_json'] and d['request_json'])
        case['same_cd_request'] = paired and c['request_json'] == d['request_json']
        if paired:
            if c['dependency_hash'] != d['dependency_hash'] or c['dependency_hash'] != u['raw_hash']:
                raise CorpusError('reply_trial_report_shared_understanding_changed')
            c_msg, d_msg = json.loads(c['request_json'])['messages'], json.loads(d['request_json'])['messages']
            if c_msg[0] != d_msg[0]:
                raise CorpusError('reply_trial_report_cd_system_changed')
            from .corpus_reply_trial import assemble_new
            thought = json.loads(u['result_json'])['body']
            for kind, message in [('C', c_msg), ('D', d_msg)]:
                rebuilt = assemble_new(plan['prompts'], plan['binding'], position, plan['captures'][str(ordinal)]['accepted_at'],
                                       understanding=thought, assets=plan['assets'] if kind == 'D' else None)
                if message != rebuilt:
                    raise CorpusError('reply_trial_report_cd_projection_changed')
        verification.append({'position': ordinal, 'shared_understanding_verified': paired,
                             'selected_expressions': case['expression_count'], 'same_cd_request': case['same_cd_request']})
        plain.append('位置 ' + str(ordinal) + '\n输入原文\n' + case['input'])
        for label in LABELS:
            kind = case['labels'][label]
            item = case['choices'][kind]
            plain.append(label + '\n' + (item['body'] if item['body'] else '状态：' + item['state']))
        cases.append(case)
    tokens = sum(a['usage'].get('total_tokens', 0) for a in accounting if a['usage'])
    known_costs = [Decimal(a['estimated_peak_CNY']) for a in accounting if a['estimated_peak_CNY'] is not None]
    timing = {}
    for kind in ('A', 'B', 'U', 'C', 'D'):
        values = [(c['understanding'] if kind == 'U' else c['choices'][kind])['elapsed_ms'] for c in cases]
        values = [v for v in values if isinstance(v, (int, float))]
        timing[kind] = {'known_requests': len(values), 'median_ms': median(values) if values else None,
                        'minimum_ms': min(values) if values else None, 'maximum_ms': max(values) if values else None}
    status = ledger.status()
    summary = {'version': 'four-condition-first-result-review-v1', 'positions': len(cases),
               'condition_replies': sum(item['state'] == 'completed' for c in cases for item in c['choices'].values()),
               'understanding_completed': sum(c['understanding']['state'] == 'completed' for c in cases),
               'dispatch_attempts': status['attempts'], 'states': dict(Counter(a['state'] for a in accounting)),
               'known_tokens': tokens, 'unknown_usage_calls': sum(a['usage'] is None and a['state'] not in ('prepared', 'waiting', 'blocked', 'cancelled') for a in accounting),
               'estimated_peak_CNY': str(sum(known_costs, Decimal(0))), 'actual_bill_CNY': None,
               'price_reference': PRICE_URL, 'price_checked_date': '2026-11-17',
               'zero_expression_positions': sum(c['expression_count'] == 0 for c in cases),
               'same_cd_request_positions': sum(c['same_cd_request'] for c in cases),
               'request_timing_by_kind': timing, 'desktop_display_wait_measured': False,
               'scope': '16 independent fixed-context inputs; persona history empty; no multi-turn superiority claim',
               'model_retries': 0, 'Jev_calls': 0, 'search_calls': 0, 'formal_publication': False}
    save_once(output / 'summary.private.json', summary)
    save_once(output / 'outputs.private.json', {'summary': summary, 'cases': cases, 'accounting': accounting, 'verification': verification})
    save_text_once(output / '全文匿名审阅.txt', '\n\n' + ('\n\n' + '=' * 48 + '\n\n').join(plain) + '\n')
    data = {'summary': summary, 'cases': cases, 'names': NAMES, 'plan_hash': fingerprint(plan),
            'persona_name': plan['persona_snapshot']['persona_name']}
    data_json = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E.L4 四条件首次全文审阅</title><style>
body{margin:0;background:#f3f5ee;color:#263d33;font:16px/1.85 system-ui,"Microsoft YaHei",sans-serif;overflow-wrap:anywhere}main{max-width:1300px;margin:auto;padding:30px 22px 90px}header,.case{background:white;border:1px solid #d8e2d2;border-radius:10px;padding:24px;margin:20px 0}h1{font-size:29px;line-height:1.45}h2{font-size:23px}h3{font-size:17px;margin-top:0}a{color:#25684d}.meta{color:#647666;font-size:14px}.toolbar{position:sticky;top:0;background:#f3f5eef5;padding:12px 0;display:flex;gap:12px;flex-wrap:wrap;align-items:center;z-index:2}button,select{font:inherit;border:1px solid #b9cbb2;background:white;border-radius:6px;padding:7px 11px;color:inherit;cursor:pointer}button[aria-pressed="true"]{background:#e1edd9}.input{background:#f1f5eb;border-radius:7px;padding:15px;margin-bottom:18px}.answers{display:grid;grid-template-columns:1fr 1fr;gap:16px}.answer{border:1px solid #dce4d7;border-radius:7px;padding:18px;min-width:0}.label{font-weight:600;margin-bottom:11px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0}.votes{margin-top:20px;display:flex;gap:9px;flex-wrap:wrap}textarea{box-sizing:border-box;width:100%;font:inherit;min-height:75px;padding:10px;border:1px solid #cfddc5;border-radius:6px;margin-top:12px;resize:vertical}details{margin:18px 0;border:1px solid #dce4d7;border-radius:6px;padding:12px}summary{cursor:pointer}details pre{margin-top:12px}.notice{padding:10px 13px;border-left:3px solid #a5b78e}.failure{color:#945137}.hidden{display:none!important}@media(max-width:760px){main{padding:15px 10px 60px}header,.case{padding:18px 13px}h1{font-size:25px}.answers{grid-template-columns:1fr}.answer{padding:16px}.toolbar{position:relative}}
</style><main><header><h1 id="title">四条件首次全文审阅</h1><p id="overview"></p><p>同一份真实输入、同一人物来源。每个位置的人物历史为空，原双人前文仅供核对；本页是16个独立位置，未进行连续多轮或真实联网。</p><p>四份正文按同一排版呈现，甲乙丙丁在每个位置单独随机。先看正文，再按需要展开理解、表达条目与条件。失败和相同结果均保留。</p><p class="meta">重点判断：表达库是否改善同一新链；新链是否优于原v8 Pro。空表达位置只能观察采样差异，不能作为表达库作用证据。</p><p><a href="全文匿名审阅.txt">全文文本</a> · <a href="summary.private.json">耗时用量与汇总</a> · <a href="outputs.private.json">完整派生审阅材料</a></p></header><div class="toolbar"><select id="filter"><option value="all">全部16个位置</option><option value="assets">有选中表达的位置</option><option value="empty">空表达／采样观察</option></select><button id="reveal">显示条件身份</button><button id="export">导出审阅意见</button><span id="progress"></span></div><div id="cases"></div></main><script id="data" type="application/json">DATA_JSON</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let reveal=false;const storage='pcar-el4-four-condition-'+data.plan_hash;let votes={};try{votes=JSON.parse(localStorage.getItem(storage)||'{}')}catch{}
function element(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function save(){try{localStorage.setItem(storage,JSON.stringify(votes))}catch{}document.getElementById('progress').textContent=Object.values(votes).filter(v=>v.choice).length+' / '+data.cases.length+' 已记录'}
document.getElementById('title').textContent=data.persona_name+' · 四条件首次全文审阅';const s=data.summary;document.getElementById('overview').textContent=s.positions+'个位置，取得'+s.condition_replies+'份人物正文、'+s.understanding_completed+'份理解；本轮尝试'+s.dispatch_attempts+'次，已知'+s.known_tokens+' tokens。按核对的高峰单价估算CNY '+s.estimated_peak_CNY+'，实际账单未知。';
function render(){const root=document.getElementById('cases');root.replaceChildren();const filter=document.getElementById('filter').value;for(const c of data.cases){if(filter==='assets'&&!c.expression_count||filter==='empty'&&c.expression_count)continue;const card=element('section','case');card.append(element('h2','', '位置 '+c.position));const input=element('div','input');input.append(element('pre','',c.input));card.append(input);if(!c.expression_count)card.append(element('p','notice','本位置没有选中表达；C／D请求相同，正文差异属于采样观察。'));const grid=element('div','answers');for(const label of ['甲','乙','丙','丁']){const kind=c.labels[label],r=c.choices[kind],box=element('div','answer');box.append(element('div','label',label+(reveal?' · '+data.names[kind]:'')));if(r.state!=='completed')box.append(element('p','failure','状态：'+r.state+'；'+(r.error||'未派发')));box.append(element('pre','',r.body||'未取得正文'));grid.append(box)}card.append(grid);const options=element('div','votes');for(const label of ['甲','乙','丙','丁','接近','均不满意']){const b=element('button','',label);b.setAttribute('aria-pressed',String(votes[c.position]?.choice===label));b.onclick=()=>{votes[c.position]={...(votes[c.position]||{}),choice:label};save();render()};options.append(b)}card.append(options);const note=element('textarea');note.placeholder='正文、角色感、承接或不恰当借用的意见';note.value=votes[c.position]?.note||'';note.oninput=()=>{votes[c.position]={...(votes[c.position]||{}),note:note.value};save()};card.append(note);const detail=element('details');detail.append(element('summary','','理解与工程证据（展开会显示条件）'));detail.append(element('pre','',JSON.stringify({conditions:c.labels,understanding:c.understanding,expression_count:c.expression_count,same_cd_request:c.same_cd_request},null,2)));for(const kind of ['A','B','U','C','D']){const p=element('p');const a=element('a','',kind+' 请求');a.href=String(c.position).padStart(2,'0')+'.'+kind+'.request.private.json';p.append(a);detail.append(p)}card.append(detail);const context=element('details');context.append(element('summary','','原双人前文（本次不发送）'));for(const m of c.original_context){context.append(element('p','meta',m.role+' · '+m.kind));context.append(element('pre','',m.text))}card.append(context);root.append(card)}save()}
document.getElementById('filter').onchange=render;document.getElementById('reveal').onclick=()=>{reveal=!reveal;document.getElementById('reveal').textContent=reveal?'隐藏条件身份':'显示条件身份';render()};document.getElementById('export').onclick=()=>{const blob=new Blob([JSON.stringify({version:'el4-author-review-v1',date:new Date().toISOString(),votes},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download='E.L4-16位四条件审阅意见.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};render();
</script></html>'''.replace('DATA_JSON', data_json)
    save_text_once(output / 'index.html', html)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze-key', 'report'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    result = freeze_review_key(args.root) if args.action == 'freeze-key' else export(args.root, args.output or args.root / 'review-v1')
    if args.action == 'freeze-key':
        result = {'review_key_frozen': True, 'positions': len(result['positions']), 'driver_llm_calls': 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
