"""Private first-result review, with exact receipts and no automatic publication."""
from html import escape
import json

from .corpus_files import CorpusError,digest
from .corpus_store import outside_repository


PRICE_URL='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
CSS='''body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f7f6;color:#183238}main{max-width:1100px;margin:28px auto;padding:24px;background:white;border:1px solid #dce6e1;border-radius:10px}h1{font-size:26px}h2{font-size:22px}h3{font-size:18px}a{color:#087578}table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse}td,th{padding:9px;border:1px solid #dce6e1;text-align:left}.proposal{border-top:2px solid #bdd3cb;margin-top:24px;padding-top:12px}.source,pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf4f1;padding:12px;font:inherit}.meta{color:#5f7478;font-size:14px}nav{display:flex;flex-wrap:wrap;gap:16px}@media(max-width:650px){main{margin:10px;padding:14px}h1{font-size:23px}}'''


def peak_estimate(usage):
    if not isinstance(usage,dict) or any(type(usage.get(k)) is not int for k in ('prompt_tokens','completion_tokens')):
        return None
    incoming=usage['prompt_tokens'];outgoing=usage['completion_tokens']
    hit=usage.get('prompt_cache_hit_tokens');miss=usage.get('prompt_cache_miss_tokens')
    if type(hit) is int and type(miss) is int and hit+miss==incoming:
        return round((hit*.04+miss*2+outgoing*8)/1000000,9)
    return round((incoming*2+outgoing*8)/1000000,9)


def export_learning_review(learning,output):
    overview=learning.inspect()
    approvals={row['approval_id'] for row in overview['jobs']}
    for ident in approvals:learning._guard(ident)
    output=outside_repository(output)
    if output.exists() and any(output.iterdir()):raise CorpusError('corpus_learning_review_empty_target_required')
    output.mkdir(parents=True,exist_ok=True)
    parts=['<h1>E.L3首批真实表达学习结果</h1><p>本页保留首次调用和模型提案，不是人物聊天效果验收。提案均待作者审阅，未发布到正式人物。</p>']
    manifest={'version':'corpus-learning-review-v1','jobs':[],'actual_billing_cny':None,
              'price_reference':{'url':PRICE_URL,'observed_date':'2026-11-17','currency':'CNY','basis':'peak_price_upper_estimate'},
              'assets_automatically_published':False}
    for status in overview['jobs']:
        with learning.connection() as db:
            row=learning._row(db,status['id'])
            proposals=[dict(r) for r in db.execute('SELECT * FROM learning_proposals WHERE job_id=? ORDER BY ordinal',(status['id'],))]
        sources=json.loads(row['sources_json']);usage=status['usage'];estimate=peak_estimate(usage)
        diagnostics=json.loads(row['transport_json']) if row['transport_json'] else {}
        raw=bytes(row['raw_response']) if row['raw_response'] is not None else None
        data={}
        if raw is not None:
            if digest(raw)!=row['raw_response_hash']:raise CorpusError('corpus_learning_receipt_changed')
            (output/f'batch-{row["position"]}.response.private.json').write_bytes(raw)
            try:data=json.loads(raw)
            except (ValueError,UnicodeError):pass
        (output/f'batch-{row["position"]}.request.private.json').write_text(json.dumps(json.loads(row['request_json']),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        entry={'position':row['position'],'job_id':row['id'],'state':row['state'],'request_hash':row['request_hash'],
               'response_hash':row['raw_response_hash'],'usage':usage,'peak_estimate_cny':estimate,'actual_billing_cny':None,
               'response_model':data.get('model') if isinstance(data,dict) else None,
               'elapsed_ms':diagnostics.get('elapsed_ms'),'proposals':[]}
        entry['usage_status']=('known' if usage and type(usage.get('total_tokens')) is int else
                               'not_applicable' if not row['attempt_id'] or (status['result'] or {}).get('usage_class')=='not_applicable' else 'unknown')
        parts.append('<section class="proposal"><h2>批次 '+str(row['position'])+'</h2><table><tbody>')
        for label,value in [('状态',row['state']),('输入原话',len(sources)),('提案',len(proposals)),
                            ('总tokens',usage.get('total_tokens') if usage else None),('请求耗时ms',diagnostics.get('elapsed_ms')),
                            ('峰值单价估算CNY',estimate),('实际扣费','未知')]:
            parts.append('<tr><td>'+label+'</td><td>'+escape(str(value) if value is not None else '未知')+'</td></tr>')
        parts.append('</tbody></table><details><summary>完整片段原文</summary>')
        for index,source in enumerate(sources,1):
            parts.append('<p class="meta">来源 '+str(index)+' · '+escape(source['speaker'])+'</p><div class="source">'+escape(source['body'])+'</div>')
        parts.append('</details>')
        for proposal in proposals:
            value=json.loads(proposal['data_json'])
            if fingerprint_value(value)!=proposal['data_hash']:raise CorpusError('corpus_learning_proposal_changed')
            entry['proposals'].append({'id':proposal['id'],'data_hash':proposal['data_hash'],'state':'pending'})
            parts.append('<section class="proposal"><h3>提案 '+str(row['position'])+'-'+str(proposal['ordinal']+1)+'</h3><p class="meta">情境</p><div class="source">'+escape(value['situation'])+'</div><p class="meta">表达方式</p><div class="source">'+escape(value['style'])+'</div><p class="meta">程序定位的原文依据</p><div class="source">'+escape(value['source_quote'])+'</div><p class="meta">待审，未绑定正式角色或自动发布。</p></section>')
        content=''
        if isinstance(data,dict):
            try:content=data['choices'][0]['message']['content']
            except (KeyError,IndexError,TypeError):content=raw.decode('utf-8',errors='replace') if raw else ''
        parts.append('<details><summary>首次模型输出全文</summary><pre>'+escape(content if isinstance(content,str) else str(content))+'</pre></details>')
        parts.append('<nav><a href="batch-'+str(row['position'])+'.request.private.json">冻结请求</a>')
        if raw is not None:parts.append('<a href="batch-'+str(row['position'])+'.response.private.json">首次完整HTTP回执</a>')
        parts.append('</nav></section>');manifest['jobs'].append(entry)
    manifest['known_total_tokens']=sum(r['usage']['total_tokens'] for r in manifest['jobs'] if r['usage_status']=='known')
    manifest['unknown_usage_jobs']=sum(r['usage_status']=='unknown' for r in manifest['jobs'])
    manifest['not_applicable_usage_jobs']=sum(r['usage_status']=='not_applicable' for r in manifest['jobs'])
    estimates=[r['peak_estimate_cny'] for r in manifest['jobs']]
    manifest['peak_estimate_cny']=(None if manifest['unknown_usage_jobs'] else round(sum(v for v in estimates if v is not None),9))
    parts.append('<h2>作者审阅</h2><p>请判断提炼结果是否保留了原话的具体表达、情境是否成立，以及是否具有后续研究价值。可按提案编号指出接受、拒绝或修改；本页不会自动将提案写入人物。首批有价值并获准继续后，再准备后续覆盖。</p><p><a href="manifest.private.json">请求、回执与费用清单</a></p>')
    html='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E.L3首批真实表达学习结果</title><style>'+CSS+'</style></head><body><main>'+''.join(parts)+'</main></body></html>'
    (output/'index.html').write_text(html,encoding='utf-8')
    (output/'manifest.private.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'review':str(output/'index.html'),'jobs':len(manifest['jobs']),
            'proposals':sum(len(j['proposals']) for j in manifest['jobs']),
            'known_total_tokens':manifest['known_total_tokens'],'unknown_usage_jobs':manifest['unknown_usage_jobs'],
            'peak_estimate_cny':manifest['peak_estimate_cny'],'actual_billing_cny':None,'assets_automatically_published':False}


def fingerprint_value(value):
    from .corpus_store import fingerprint
    return fingerprint(value)
