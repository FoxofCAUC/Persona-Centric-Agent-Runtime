"""Show the frozen native decision responses and their actual validation outcome."""
from collections import Counter
from html import escape
import json
from pathlib import Path
from statistics import median

from .corpus_reply_study import read_private
from .corpus_reply_trial_report import save_text_once
from .en5_decision_contract import require
from .en5_decision_packet import file_hash


def export(root):
    root=Path(root).resolve()
    saved=read_private(root/'d2/results.private.json')
    packet=read_private(root/'material.private.json')
    ref=read_private(root/'d1/results.private.json')
    require(file_hash(root/'d1/results.private.json')==saved['d1_sha256'],'d2_d1_changed')
    shapes=Counter()
    waits=[]
    articles=[]
    for case in packet['cases']:
        pos=case['position']
        out=saved['cases'][pos]
        raw=out['flash']['content']
        try:
            parsed=json.loads(raw)
            pattern=' / '.join(sorted(parsed)) if isinstance(parsed,dict) else type(parsed).__name__
        except (TypeError,ValueError):
            pattern='无法解析为单个 JSON 对象'
        shapes[pattern]+=1
        ms=out['flash'].get('elapsed_ms')
        if ms is not None:waits.append(ms/1000)
        field=', '.join((str(out['ref']['target']),out['ref']['action'],out['ref']['task_state']))
        articles.extend([f'<article><h2>{pos}</h2><p>'+escape(case['original']['messages'][-1]['content'])+'</p>',
                         '<p>作者判断　'+escape(field)+'</p><p>Flash 返回　'+escape(pattern)+'</p>',
                         '<pre>'+escape(raw or '未取得完整响应')+'</pre>',
                         '<p>正文来源　'+escape(out['body_origin'])+'</p><details><summary>D1 人工决策稿和 D2 最终正文</summary>',
                         '<pre>'+escape(ref['results'][pos]['B']['body'] or '未取得')+'</pre><pre>'+
                         escape(out['body']['body'] or '未取得')+'</pre></details></article>'])
    html=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
          '<title>D2 首次决策回执</title><style>body{max-width:56rem;margin:auto;padding:1rem;font:16px/1.65 system-ui}article{border-top:1px solid #cad7cf;padding:.6rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf4ef;padding:.7rem}</style>',
          '<h1>D2 首次决策回执</h1><p>已冻结的决策字段为 target、action、task_state。每轮列出收到的原文，供核对判断能否交给 Pro。</p>',
          '<p>'+escape('，'.join(f'{name} 有 {n} 份' for name,n in shapes.items()))+'。</p>',
          '<p><a href="index.html">查看逐轮匿名正文、实际耗时与回退记录</a></p>',*articles,'</html>']
    target=root/'d2/review/decisions.html'
    save_text_once(target,''.join(html))
    return target,dict(shapes),median(waits) if waits else None
