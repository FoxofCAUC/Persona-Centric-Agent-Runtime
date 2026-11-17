"""Author review of actual expression selections; no reply regeneration."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
from statistics import median

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .corpus_semantic_selector import finalize_selection


def export(root, output):
    manifest = read_private(root / 'selector-manifest.private.json')
    study = Path(manifest['study_root'])
    plan = read_private(study / 'plan.private.json')
    local = read_private(study / 'selection-repair-v3/candidate.private.json')
    by_id = {a['id']: a for a in plan['assets']['assets']}
    cases, usages, times = [], [], []
    cost = Decimal(0)
    output.mkdir(parents=True, exist_ok=True)
    for p in manifest['packets']:
        n = p['position']
        packet = read_private(root / p['path'])
        folder = root / 'selector-calls' / f'{n:02}'
        result = read_private(folder / 'outcome.private.json')
        raw, metadata = (folder / 'response.private.bytes').read_bytes(), read_private(folder / 'response-meta.private.json')
        if result != finalize_selection(packet, raw, metadata):
            raise CorpusError('semantic_selection_report_receipt_changed')
        old = plan['positions'][n - 1]
        new = result.get('selected_proposal_ids', [])
        if any(i not in by_id for i in new):
            raise CorpusError('semantic_selection_report_unknown_asset')
        cases.append({'position': n, 'input': old['input_text'], 'input_hash': old['input_text_hash'],
            'actual_understanding': packet['request']['messages'][3]['content'],
            'old_ids': old['selection']['selected_ids'], 'local_ids': local['cases'][n - 1]['new_selection']['selected_ids'],
            'new_ids': new, 'state': result['state'], 'error': result.get('error'),
            'selected_numbers': result['selected_ids'], 'request_hash': packet['request_hash'], 'response_hash': result['raw_sha256']})
        usage = result['usage']
        usages.append(usage)
        if usage is not None:
            hit, miss, completion = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens'), usage.get('completion_tokens')
            if type(hit) is int and type(miss) is int and type(completion) is int:
                cost += (Decimal(hit) * Decimal('.04') + Decimal(miss) * 2 + Decimal(completion) * 8) / Decimal(1000000)
        elapsed = metadata['diagnostics'].get('elapsed_ms')
        if isinstance(elapsed, (int, float)):times.append(elapsed)
    summary = {'version': 'semantic-selection-first-results-v1', 'date': '2026-11-18',
        'selector_requests': len(list((root / 'selector-calls').glob('*/attempt.private.json'))),
        'states': dict(Counter(c['state'] for c in cases)), 'nonempty_positions': sum(bool(c['new_ids']) for c in cases),
        'empty_positions': [c['position'] for c in cases if not c['new_ids']],
        'old_selected_total': sum(len(c['old_ids']) for c in cases), 'new_selected_total': sum(len(c['new_ids']) for c in cases),
        'prompt_tokens': sum(u['prompt_tokens'] for u in usages if u),
        'completion_tokens': sum(u['completion_tokens'] for u in usages if u),
        'known_tokens': sum(u['total_tokens'] for u in usages if u), 'unknown_usage': sum(u is None for u in usages),
        'estimated_peak_CNY': str(cost), 'actual_bill_CNY': None,
        'price_reference': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/', 'price_checked_date': '2026-11-18',
        'request_median_ms': median(times) if times else None, 'request_maximum_ms': max(times) if times else None,
        'new_persona_reply_calls': 0, 'original_eighty_requests_replayed': 0, 'formal_activation': False,
        'selection_suitability_author_approved': False, 'body_improvement_evaluated': False}
    data = {'summary': summary, 'cases': cases, 'assets': by_id,
            'manifest_sha256': digest((root / 'selector-manifest.private.json').read_bytes()),
            'result_set_hash': fingerprint(cases)}
    save_once(output / 'results.private.json', data)
    save_once(output / 'summary.private.json', summary)
    encoded = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>语义选材首次结果审阅</title><style>
body{margin:0;background:#f3f5ee;color:#263d33;font:16px/1.85 system-ui,"Microsoft YaHei",sans-serif;overflow-wrap:anywhere}main{max-width:1260px;margin:auto;padding:30px 22px 90px}header,.case{background:white;border:1px solid #d9e2d3;border-radius:10px;padding:24px;margin:20px 0}h1{font-size:29px;line-height:1.4}h2{font-size:23px}h3{font-size:17px}.meta{font-size:14px;color:#657969}.notice{border-left:3px solid #a2b68b;padding:10px 13px;background:#f5f8ef}a{color:#25694f}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:10px 0}.input{background:#f0f5e9;padding:14px;border-radius:7px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:18px}.asset{padding:13px 15px;margin:13px 0;border:1px solid #dbe5d4;border-radius:7px}.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;position:sticky;top:0;background:#f3f5eef5;padding:12px 0;z-index:2}select,button{font:inherit;padding:7px 10px;color:inherit;border:1px solid #bfd0b4;border-radius:6px;background:white;cursor:pointer}textarea{box-sizing:border-box;width:100%;font:inherit;min-height:70px;margin:12px 0;padding:9px;border:1px solid #cfddc6;border-radius:6px;resize:vertical}details{border:1px solid #dbe5d4;border-radius:6px;padding:11px;margin:16px 0}summary{cursor:pointer;font-weight:600}.review-row{margin-top:16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}@media(max-width:760px){main{padding:13px 10px 60px}header,.case{padding:18px 13px}h1{font-size:25px}.columns{grid-template-columns:1fr}.toolbar{position:relative}.asset{padding:11px}}
</style><main><header><h1>语义选材 · 16位首次结果</h1><p id="overview"></p><p>本页审阅“哪些表达适合给当前人物参考”，没有生成新的人物回复；原80次正文与理解保持。相同一份原话、原受理时间和原Flash理解，语义终选逐位只调用一次。</p><p class="notice">请重点核对条目情境、说法及表述主体是否适合当前接话。条目仅是可借鉴表达，不要求照抄；当前仍有值得复核的候选，不自动启用，也不把选材数量当自然度收益。</p><p>位置2不再选择旧的评价程度条目；位置15仍含描述自身休息的条目，已单独标出供你判断。是否改善实际正文，需要后续另行验证。</p><p><a href="../index.html">已批准原文与完整请求</a> · <a href="summary.private.json">账目与范围</a> · <a href="results.private.json">完整结果与来源</a></p></header><div class="toolbar"><select id="filter"><option value="all">全部16位</option><option value="nonempty">有选中条目</option><option value="empty">空结果</option></select><button id="export">导出选材审阅意见</button><span id="progress"></span></div><div id="cases"></div></main><script id="data" type="application/json">DATA_JSON</script><script>
const data=JSON.parse(document.getElementById('data').textContent),storage='pcar-semantic-selection-review-'+data.result_set_hash;let reviews={};try{reviews=JSON.parse(localStorage.getItem(storage)||'{}')}catch{}
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function save(){try{localStorage.setItem(storage,JSON.stringify(reviews))}catch{}document.getElementById('progress').textContent=Object.values(reviews).filter(v=>v.decision&&v.decision!=='unreviewed').length+' / 16 已审'}
function selection(ids,isNew,c){const box=el('div');if(!ids.length){box.append(el('p','meta','没有选中条目'));return box}for(const id of ids){const a=data.assets[id],row=el('div','asset');row.append(el('h3','',a.situation));row.append(el('pre','',a.style));if(isNew){const pick=el('select');for(const [value,text] of [['unreviewed','这条：未判断'],['suitable','这条合适'],['unsuitable','这条不合适'],['uncertain','这条不确定']])pick.append(new Option(text,value));pick.value=reviews[c.position]?.assets?.[id]||'unreviewed';pick.onchange=()=>{const v=reviews[c.position]||{};reviews[c.position]={...v,assets:{...(v.assets||{}),[id]:pick.value}};save()};row.append(pick)}const source=el('details');source.append(el('summary','','原真人来源（未发送给选择器）'));source.append(el('pre','',a.source_quote));row.append(source);box.append(row)}return box}
const s=data.summary;document.getElementById('overview').textContent=s.selector_requests+'次选择请求全部完成，'+s.nonempty_positions+'位有条目、'+s.empty_positions.length+'位留空，共'+s.new_selected_total+'条；已知'+s.known_tokens+' tokens，按高峰价估算CNY '+s.estimated_peak_CNY+'，实际扣费未知。新增人物正文调用0次。';
function render(){const root=document.getElementById('cases');root.replaceChildren();for(const c of data.cases){const filter=document.getElementById('filter').value;if(filter==='nonempty'&&!c.new_ids.length||filter==='empty'&&c.new_ids.length)continue;const card=el('section','case');card.id='position-'+c.position;card.append(el('h2','','位置 '+c.position));const input=el('div','input');input.append(el('pre','',c.input));card.append(input);if(c.position===15)card.append(el('p','notice','请特别核对：当前说的是没休息且要上班；新结果含“告知自己要去休息”条目。这里未替你判定它应如何被人物转用。'));const cols=el('div','columns'),old=el('div'),fresh=el('div');old.append(el('h3','','原词面选材'));old.append(selection(c.old_ids,false,c));fresh.append(el('h3','','语义终选首次结果'));fresh.append(selection(c.new_ids,true,c));cols.append(old,fresh);card.append(cols);const row=el('div','review-row');row.append(el('span','','这一轮新选材：'));const choice=el('select');for(const [value,text] of [['unreviewed','未审阅'],['suitable','整体合适'],['partly_suitable','部分合适'],['unsuitable','不合适'],['should_be_empty','这轮应留空'],['missing_assets','有漏选'],['need_context','需要更多语境']])choice.append(new Option(text,value));choice.value=reviews[c.position]?.decision||'unreviewed';choice.onchange=()=>{reviews[c.position]={...(reviews[c.position]||{}),decision:choice.value};save()};row.append(choice);card.append(row);const note=el('textarea');note.placeholder='情境、主体或说法的意见';note.value=reviews[c.position]?.note||'';note.oninput=()=>{reviews[c.position]={...(reviews[c.position]||{}),note:note.value};save()};card.append(note);const detail=el('details');detail.append(el('summary','','原理解、模型编号与首次回执'));detail.append(el('pre','',c.actual_understanding));detail.append(el('pre','',JSON.stringify({selected_numbers:c.selected_numbers,request_hash:c.request_hash,response_hash:c.response_hash},null,2)));const p=el('p');for(const [label,path] of [['冻结请求','../'+String(c.position).padStart(2,'0')+'.selector-request.private.json'],['首次结果','../selector-calls/'+String(c.position).padStart(2,'0')+'/outcome.private.json'],['完整HTTP回执','../selector-calls/'+String(c.position).padStart(2,'0')+'/response.private.bytes']]){const a=el('a','',label+'　');a.href=path;p.append(a)}detail.append(p);card.append(detail);root.append(card)}save()}
document.getElementById('filter').onchange=render;document.getElementById('export').onclick=()=>{const payload={version:'semantic-selection-author-review-v1',date:new Date().toISOString(),selector_manifest_sha256:data.manifest_sha256,result_set_hash:data.result_set_hash,reviews};const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='E.L4-16位语义选材审阅意见.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};render();
</script></html>'''.replace('DATA_JSON', encoded)
    save_text_once(output / 'index.html', html)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export(args.root, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':main()
