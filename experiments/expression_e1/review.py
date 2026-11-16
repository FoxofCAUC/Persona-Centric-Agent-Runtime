"""从冻结回执生成匿名全文对照；不调用模型，也不预填用户结论。"""
import argparse
from html import escape
import json
from pathlib import Path
import random
import re

from persona_runtime.config import atomic_json
from .core import build_messages, decode_output
from .runner import fingerprint, load, report, verify_frozen


def view(body, segments=None):
    pieces = segments or [{'text': body}]
    return ''.join('<pre class="bubble">' + escape(item['text']) + '</pre>' for item in pieces)


def candidate(root, plan, ident):
    path = root / 'positions' / ident / 'result.json'
    if not path.exists():
        return None, '该位置尚未取得返回'
    result = load(path)
    if result['candidate_status'] != 'usable':
        if isinstance(result.get('raw_content'), str) and not result['raw_content'].strip():
            return None, f"首次响应为全空白（{len(result['raw_content'])}个空白字符），没有可审阅正文；原始回执已保留。"
        return None, '该位置没有可用表达稿；首次返回与错误已保留：' + (result.get('error_code') or result['state'])
    material = load(root / 'inputs' / (result['case_id'] + '.json'))
    _, offered = build_messages(material, plan['fixtures'], with_assets=result['arm'] == 'C')
    decoded = decode_output(result['raw_content'], material, offered)
    if decoded != result['candidate']:
        raise ValueError('review_candidate_differs_from_first_receipt')
    raw = load(root / 'positions' / ident / 'response.json')
    if raw['choices'][0]['message']['content'] != result['raw_content']:
        raise ValueError('review_raw_content_changed')
    return decoded, ''


def markdown_text(body):
    longest = max((len(m[0]) for m in re.finditer(r'`+', body)), default=0)
    fence = '`' * max(3, longest + 1)
    return fence + 'text\n' + body + '\n' + fence + '\n\n'


def build(root):
    plan = load(root / 'plan.json')
    verify_frozen(root, plan)
    counts = report(root, plan)
    seed = fingerprint(plan)
    cards = []
    mapping = {}
    schema = []
    markdown = ['# E1首次全文匿名对照\n\n真实主稿与人工边界稿分别标注。气泡为呈现计划，尚未模拟桌面时序。用户审阅待完成。\n\n']

    def comparison(key, title, item, variants):
        labels = ['甲', '乙', '丙'][:len(variants)]
        order = list(variants)
        random.Random(seed + key).shuffle(order)
        mapping[key] = dict(zip(labels, order))
        schema.append({'id': key, 'labels': labels})
        columns = []
        markdown.append('## ' + title + '\n\n用户：' + item['user'] + '\n\n')
        for label, arm in zip(labels, order):
            body, segments, unavailable = variants[arm]
            columns.append('<article><h3>方案' + label + '</h3>' +
                           ('<p class="unavailable">' + escape(unavailable) + '</p>' if unavailable else view(body, segments)) + '</article>')
            markdown.append('### 方案' + label + '\n\n')
            markdown.append(unavailable + '\n\n' if unavailable else markdown_text(body))
        spoken = ''.join('<pre>' + escape(text) + '</pre>' for text in item['already_spoken'])
        history = ''.join('<p class="speaker">' + ('用户' if line['speaker'] == 'user' else '人物') + '</p><pre>' + escape(line['text']) + '</pre>' for line in item['history'])
        options = ''.join('<option value="' + label + '">方案' + label + '</option>' for label in labels)
        base_id = key.removesuffix('-diagnostic')
        evidence = ''.join('<p><a href="positions/' + base_id + '-' + arm + '/response.json">首次原始响应 ' + str(i + 1) + '</a></p>'
                           for i, arm in enumerate(('C', 'C0') if key.endswith('-diagnostic') else ('C',))
                           if (root / 'positions' / (base_id + '-' + arm) / 'response.json').exists())
        cards.append('<section id="' + key + '"><h2>' + escape(title) + '</h2><p class="question">' + escape(item['user']) + '</p>' +
                     ('<details><summary>本轮已说原文</summary>' + spoken + '</details>' if spoken else '') +
                     ('<details><summary>前文语境</summary>' + history + '</details>' if history else '') +
                     '<div class="comparison">' + ''.join(columns) + '</div><div class="review"><label>更愿意继续聊：<select data-choice="' + key + '"><option value="">暂未选择</option>' + options +
                     '<option value="no_difference">无区别</option><option value="all_bad">都不好</option></select></label><textarea data-note="' + key + '" placeholder="口吻、语义变化或其他具体感受"></textarea></div><details><summary>首次回执</summary>' + evidence + '</details></section>')

    for case in plan['cases']:
        item = load(root / 'inputs' / (case['id'] + '.json'))
        base = load(root / 'baselines' / (case['id'] + '.json'))
        transformed, error = candidate(root, plan, case['id'] + '-C')
        variants = {'A': (base['A'], None, ''), 'B': (base['A'], base['B'], ''),
                    'C': (transformed['body'], transformed['segments'], '') if transformed else ('', None, error)}
        source = '已审阅真实v8主稿' if case['source_kind'] == 'approved_first_v8_reply' else '人工边界稿'
        comparison(case['id'], case['id'] + ' · ' + item['role_projection']['name'] + ' · ' + source, item, variants)
    for ident in plan['diagnostic_case_ids']:
        item = load(root / 'inputs' / (ident + '.json'))
        variants = {}
        for arm in ('C', 'C0'):
            transformed, error = candidate(root, plan, ident + '-' + arm)
            variants[arm] = (transformed['body'], transformed['segments'], '') if transformed else ('', None, error)
        comparison(ident + '-diagnostic', ident + ' · 表达候选诊断对照', item, variants)
    safe_schema = json.dumps(schema, ensure_ascii=False).replace('<', '\\u003c')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>E1首次全文对照</title><style>
*{box-sizing:border-box}body{font:16px/1.75 "Microsoft YaHei",sans-serif;color:#26343b;background:#f3f4f1;margin:0;padding:32px 24px}main{max-width:1480px;margin:auto}h1{margin-bottom:10px}h2{font-size:19px;margin:0 0 12px}h3{font-size:16px;color:#536864}section{background:#fff;border:1px solid #d9dfd9;border-radius:12px;padding:24px;margin:28px 0}.comparison{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px}.bubble{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;padding:14px 16px;background:#eef3ef;border-radius:10px;border:1px solid #e0e7e1}.question{background:#f5f1e6;padding:14px 16px}.review{border-top:1px solid #e3e7e1;margin-top:18px;padding-top:16px}textarea{display:block;width:100%;min-height:74px;padding:10px;font:inherit;margin-top:10px;border:1px solid #cfd7d0;border-radius:6px}select,button{font:inherit;padding:6px 10px}button{background:#365d50;color:white;border:0;border-radius:6px;cursor:pointer}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}.speaker{font-size:14px;color:#5d7069;margin-bottom:0}summary{cursor:pointer;color:#527064}.unavailable{color:#8a4d31}small{color:#5c7067}@media(max-width:700px){body{padding:18px 12px}section{padding:16px}.comparison{grid-template-columns:1fr}}</style><main><h1>E1 · 首次全文对照</h1>
<p>24组三方案对照，另有8组表达候选诊断。真实主稿16份、人工边界稿8份分别标明。气泡是呈现计划，当前页面未模拟时间等待。</p><p>请看自然度、人物口吻和必要内容是否保持，允许“无区别”或“都不好”。审阅结论由你填写。</p><button id="export">导出审阅记录</button>'''
    html += '<details><summary>调用记录</summary><p>已返回' + str(counts['settled_positions']) + '份，其中' + str(counts['usable_candidates']) + '份具有可用表达稿；已知tokens ' + str(counts['known_tokens']) + '，未知用量' + str(counts['unknown_usage_records']) + '笔。保守费用估算CNY ' + escape(str(counts['estimated_cost_cny'])) + '，实际账单金额未知。</p></details>'
    html += ''.join(cards) + '</main><script id="schema" type="application/json">' + safe_schema + '''</script><script>
const cases=JSON.parse(document.getElementById('schema').textContent);
document.getElementById('export').addEventListener('click',()=>{
 const rows=cases.map(c=>({id:c.id,choice:document.querySelector('[data-choice="'+c.id+'"]').value,note:document.querySelector('[data-note="'+c.id+'"]').value}));
 const payload={version:'expression-e1-user-review-v1',reviewed_at:new Date().toISOString(),rows};
 const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download='E1-用户审阅.json';a.click();URL.revokeObjectURL(url);
});</script></html>'''
    (root / 'E1-首次全文匿名对照.html').write_text(html, encoding='utf-8')
    (root / 'E1-首次全文匿名对照.md').write_text(''.join(markdown), encoding='utf-8')
    atomic_json(root / 'review-key.json', {'mapping': mapping, 'user_review': 'pending'})
    return {'comparisons': len(cards), 'review_filled_by_agent': False, 'html': str(root / 'E1-首次全文匿名对照.html')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.root), ensure_ascii=False))


if __name__ == '__main__':
    main()
