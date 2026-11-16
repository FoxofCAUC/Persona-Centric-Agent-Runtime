"""E1修复全文对照：旧失败单列，未发送补位按首次实际返回关联。"""
import argparse
from html import escape
import json
import os
from pathlib import Path
import random

from persona_runtime.config import atomic_json
from . import repair, repair_continue
from .repair_protocol import PROTOCOLS
from .review import markdown_text, view
from .runner import fingerprint, load


def eligible(rows, original_position, protocol):
    matches = [row for row in rows if row['original_position'] == original_position
               and row['protocol'] == protocol and row['state'] == 'received']
    if len(matches) > 1:
        raise ValueError('multiple_model_results_for_one_repair_slot')
    return matches[0] if matches else None


def build(root):
    plan, revision = load(root / 'plan.json'), load(root / 'execution-v3.json')
    repair_continue.verify(root, plan, revision)
    execution = plan | {'positions': revision['positions']}
    repair.reconcile(root, execution)
    report = repair_continue.report(root, plan, revision)
    mapping, forms, cards = {}, [], []
    md = ['# E1协议修复 · 首次全文对照\n\n主稿和两个修复协议匿名排序。旧32个首次结果保持；原发送前连接失败与实际补位分别记录。用户审阅待完成。\n\n']
    positions = {p['id']: p for p in revision['positions']}
    original_root = Path(plan['original_root'])

    def relative(path):
        return escape(os.path.relpath(path, root).replace(os.sep, '/'), quote=True)

    for index, ident in enumerate(plan['target_original_positions']):
        prior = load(original_root / 'positions' / ident / 'result.json')
        item = load(root / 'inputs' / (prior['case_id'] + '.json'))
        variants = {'saved_draft': (item['draft'], None, None, '')}
        evidence = []
        for protocol in PROTOCOLS:
            row = eligible(report['rows'], ident, protocol)
            if row and row['candidate_status'] == 'usable':
                again = repair.result_from_receipt(root, plan, positions[row['position']])
                if again != row:
                    raise ValueError('repair_review_differs_from_saved_response')
                variants[protocol] = (row['candidate']['body'], row['candidate']['segments'], row['position'], '')
            else:
                variants[protocol] = ('', None, row['position'] if row else None, '尚无可用人物正文；首次状态保留。')
            if row:
                evidence.append('<a href="positions/' + row['position'] + '/response.json">修复首次原始响应</a>')
        order = list(variants)
        random.Random(fingerprint(plan) + ident).shuffle(order)
        mapping[ident] = dict(zip(('甲', '乙', '丙'), order))
        forms.append(ident)
        columns = []
        md.append('## ' + ident + '\n\n用户：' + item['user'] + '\n\n')
        for label, arm in zip(('甲', '乙', '丙'), order):
            body, segments, _, error = variants[arm]
            columns.append('<article><h3>方案' + label + '</h3>' + ('<p>' + error + '</p>' if error else view(body, segments)) + '</article>')
            md.append('### 方案' + label + '\n\n')
            md.append(error + '\n\n' if error else markdown_text(body))
        old_text = prior.get('raw_content')
        note = (f"旧首次响应为{len(old_text)}个空白字符。" if isinstance(old_text, str) and not old_text.strip()
                else '该位置原先有可用返回，本轮作为代码或短确认对照。')
        history = ''.join('<p>' + ('用户' if h['speaker'] == 'user' else '人物') + '</p><pre>' + escape(h['text']) + '</pre>' for h in item['history'])
        spoken = ''.join('<pre>' + escape(text) + '</pre>' for text in item['already_spoken'])
        evidence.insert(0, '<a href="' + relative(original_root / 'positions' / ident / 'response.json') + '">旧首次原始响应</a>')
        cards.append('<section><h2>' + str(index + 1) + ' · ' + escape(ident) + ' · ' + escape(item['role_projection']['name']) + '</h2><p class="question">' + escape(item['user']) + '</p>' +
                     '<details><summary>旧问题与原始回执</summary><p>' + note + '</p>' + '<br>'.join(evidence) + '</details>' +
                     ('<details><summary>必要前文</summary>' + history + spoken + '</details>' if history or spoken else '') +
                     '<div class="comparison">' + ''.join(columns) + '</div><label>更愿意继续聊：<select data-choice="' + ident + '"><option value="">暂未选择</option><option>甲</option><option>乙</option><option>丙</option><option>无区别</option><option>都不好</option></select></label>' +
                     '<textarea data-note="' + ident + '" placeholder="语气、内容保留和气泡方面的具体感受"></textarea></section>')
    schema = json.dumps(forms, ensure_ascii=False)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E1修复全文对照</title><style>
*{box-sizing:border-box}body{font:16px/1.75 "Microsoft YaHei",sans-serif;background:#f3f4f1;color:#26343b;padding:28px 20px}main{max-width:1440px;margin:auto}section{background:white;border:1px solid #dae0d8;padding:24px;border-radius:10px;margin:24px 0}h2{font-size:19px}h3{font-size:16px;color:#526b60}.comparison{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px}.bubble{background:#edf3ef;padding:14px;border:1px solid #e0e6df;border-radius:9px}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}.question{background:#f5f0e2;padding:14px}summary,a{color:#35634f}summary{cursor:pointer}select,textarea,button{font:inherit}textarea{display:block;width:100%;min-height:76px;margin-top:10px;padding:10px;border:1px solid #d0d9cf}select{padding:6px}button{background:#365d50;color:white;border:0;border-radius:6px;padding:9px 15px;cursor:pointer}label{display:block;margin-top:20px}@media(max-width:900px){.comparison{grid-template-columns:1fr}body{padding:16px 10px}section{padding:16px}}</style><main><h1>E1 · 修复后首次全文对照</h1><p>8组主稿与两种修复协议的匿名对照。人物发言保留首次返回，气泡仅展示呈现计划。请评价自然度、人物口吻和内容保留情况，允许无区别或都不好。</p>'''
    html += '<p><a href="' + relative(original_root / 'E1-首次全文匿名对照.html') + '">打开原32个位置的完整对照</a></p>'
    html += '<details><summary>调用与失败记录</summary><p>本次模型调用' + str(report['physical_calls_recorded']) + '次，可用正文' + str(report['usable_candidates']) + '份；另有' + str(len(report['not_dispatched_positions'])) + '次发送前失败保留，用量为不适用。已知tokens ' + str(report['known_tokens']) + '，未知用量' + str(report['unknown_usage_records']) + '笔。保守费用估算CNY ' + str(report['estimated_cost_cny']) + '，实际金额未知。</p></details><button id="export">导出审阅记录</button>'
    html += ''.join(cards) + '</main><script type="application/json" id="schema">' + schema + '''</script><script>
document.getElementById('export').addEventListener('click',()=>{
const ids=JSON.parse(document.getElementById('schema').textContent);
const rows=ids.map(id=>({id,choice:document.querySelector('[data-choice="'+id+'"]').value,note:document.querySelector('[data-note="'+id+'"]').value}));
const payload={version:'expression-e1-repair-user-review-v1',reviewed_at:new Date().toISOString(),rows};
const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='E1-修复审阅.json';a.click();URL.revokeObjectURL(url);
});</script></html>'''
    (root / 'E1-修复后首次全文对照.html').write_text(html, encoding='utf-8')
    (root / 'E1-修复后首次全文对照.md').write_text(''.join(md), encoding='utf-8')
    atomic_json(root / 'repair-review-key.json', {'mapping': mapping, 'user_review': 'pending'})
    return {'comparisons': len(cards), 'html': str(root / 'E1-修复后首次全文对照.html'), 'user_review_filled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.root), ensure_ascii=False))


if __name__ == '__main__':
    main()
