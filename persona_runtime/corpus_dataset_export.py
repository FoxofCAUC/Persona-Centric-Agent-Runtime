"""Private dataset files and offline review; final holdout text is not in review pages."""
from datetime import datetime, timedelta, timezone
from html import escape
import json

from .corpus_files import CorpusError, digest
from .corpus_store import outside_repository


CSS = '''
body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f7f6;color:#183237}
main{max-width:1100px;margin:28px auto;padding:24px;background:white;border:1px solid #dbe5e2;border-radius:10px}
a{color:#087578}h1{font-size:26px}h2{font-size:21px}table{width:100%;border-collapse:collapse}
table{display:block;max-width:100%;overflow-x:auto}td,th{padding:9px;border:1px solid #dbe5e2;text-align:left}.meta{color:#587074;font-size:14px}
nav{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.fragment{border-top:2px solid #bfd5cd;margin-top:28px;padding-top:12px}
.message{padding:8px 0;border-bottom:1px solid #e5ece9}.text{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf5f1;padding:10px 14px;border-radius:7px}
.context .text{background:#f3f4f4;color:#687a7d}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f4;padding:12px;font:inherit}code{overflow-wrap:anywhere}
@media(max-width:650px){main{margin:10px;padding:14px}.pair{grid-template-columns:1fr}h1{font-size:23px}}
'''


def _page(title, body):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+escape(title)+'</title><style>'+CSS+'</style></head><body><main>'+body+'</main></body></html>')


def _time(epoch):
    return datetime.fromtimestamp(epoch, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def export_dataset(store, view_id, output):
    verified = store.verify_version(view_id)
    payload = store.load_version(view_id)
    status = store.view_status(view_id)
    output = outside_repository(output)
    if output.exists() and any(output.iterdir()):
        raise CorpusError('corpus_dataset_export_empty_target_required')
    output.mkdir(parents=True, exist_ok=True)
    with store.connection() as db:
        parse_id = status['parse_id']
        rows, relations = store._source_rows(db, parse_id)
    source = {r['id']: r for r in rows}
    by_id = {r['message_id']: r for r in payload['messages']}
    replies = {r['source_id']: {'message_id': r['target_id'], 'state': r['state']}
               for r in relations if r['kind'] == 'reply'}
    catalog = []

    def jsonl(name, records):
        path = output/name
        count = 0
        with path.open('w',encoding='utf-8',newline='\n') as stream:
            for item in records:
                stream.write(json.dumps(item,ensure_ascii=False,separators=(',',':'))+'\n')
                count += 1
        catalog.append({'name':name,'sha256':digest(path.read_bytes()),'records':count})

    for key in ('messages','fragments','groups','edges'):
        jsonl('audit-'+key+'.private.jsonl',payload[key])

    def projected(fragment):
        messages = []
        for ident in fragment['message_ids']:
            row = by_id[ident]
            item = {k:row[k] for k in ('message_id','role','kind','text','target_candidate','flags')}
            if ident in replies:
                item['reply_to'] = replies[ident]
            messages.append(item)
        return {'view_id':view_id,'fragment_id':fragment['id'],'group_id':fragment['group_id'],
                'split':fragment['split'],'messages':messages,'model_input_approved':False}

    for partition in ('learn','dev_review','final_holdout'):
        jsonl(partition+'.fragments.jsonl',(projected(f) for f in payload['fragments'] if f['split']==partition))
    # Review pages deliberately omit final-holdout text. The sealed file above
    # is an explicit artifact for a later evaluation, not a prompt-tuning sample.
    visible = [f for f in payload['fragments'] if f['split'] in ('learn','dev_review')]
    page_count = (len(visible)+19)//20
    for number in range(page_count):
        links = ['<a href="index.html">数据版本摘要</a>']
        if number:links.append('<a href="fragments-'+str(number)+'.html">上一页</a>')
        if number+1<page_count:links.append('<a href="fragments-'+str(number+2)+'.html">下一页</a>')
        nav = '<nav>'+''.join(links)+'</nav>'
        parts = ['<h1>学习与开发审阅片段</h1><p>保留消息顺序；A／B分别对应本人／对方。灰色条目仅作上下文或类型记录。最终留出原文不在这些页面中。</p>',nav]
        for fragment in visible[number*20:(number+1)*20]:
            parts.append('<section class="fragment"><h2>#'+str(fragment['ordinal']+1)+' · '+escape(fragment['split'])+'</h2>')
            parts.append('<p class="meta">'+_time(fragment['first_epoch'])+' 至 '+_time(fragment['last_epoch'])+' · 默认文字目标 '+str(len(fragment['target_ids']))+' 条</p>')
            for ident in fragment['message_ids']:
                row = by_id[ident]
                parts.append('<div class="message'+('' if row['target_candidate'] else ' context')+'"><div class="meta">#'+str(row['ordinal']+1)+' · '+row['role']+' · '+escape(row['kind'])+'</div>')
                if row['text']:parts.append('<div class="text">'+escape(row['text'])+'</div>')
                if row['flags']:parts.append('<div class="meta">'+escape(', '.join(row['flags']))+'</div>')
                if row['redactions']:
                    parts.append('<details><summary>本条脱敏前后核对</summary><div class="pair"><pre>'+escape(source[ident]['text'])+'</pre><pre>'+escape(row['text'])+'</pre></div></details>')
                parts.append('</div>')
            parts.append('</section>')
        parts.append(nav)
        (output/('fragments-'+str(number+1)+'.html')).write_text(_page('学习与开发审阅片段',''.join(parts)),encoding='utf-8')
    changed = [r for r in payload['messages'] if r['redactions'] and r['split'] in ('learn','dev_review')]
    parts = ['<h1>脱敏变更核对</h1><p>左侧为私有原文，右侧为派生视图；此页仅本机审阅，不能作为训练文件上传。最终留出原文默认封存。</p><nav><a href="index.html">返回摘要</a></nav>']
    for row in changed:
        parts.append('<section class="fragment"><h2>#'+str(row['ordinal']+1)+' · '+escape(row['split'])+'</h2><div class="pair"><pre>'+escape(source[row['message_id']]['text'])+'</pre><pre>'+escape(row['text'])+'</pre></div>')
        parts.append('<p class="meta">'+escape(', '.join(m['kind'] for m in row['redactions']))+'</p></section>')
    (output/'changes.html').write_text(_page('脱敏变更核对',''.join(parts)),encoding='utf-8')
    summary = payload['summary']
    body = '<h1>真实私聊数据集 E.L2 审阅</h1><p>本版本为本地草稿，未发送学习模型。原件保留；正文仅在明确跨度内替换敏感标识，没有润色或补写。</p>'
    body += '<table><thead><tr><th>项目</th><th>数量</th></tr></thead><tbody>'
    for label,name in [('原始记录','source_messages'),('源文字候选','source_text_candidates'),('默认目标候选','default_target_candidates'),
                       ('有脱敏变更的消息','redacted_messages'),('脱敏跨度','redaction_spans'),('时间片段','fragments'),('分区绑定组','split_groups')]:
        body += '<tr><td>'+label+'</td><td>'+str(summary[name])+'</td></tr>'
    body += '</tbody></table><h2>数据分区</h2><table><thead><tr><th>分区</th><th>片段</th><th>组</th><th>分区前候选</th><th>默认目标</th></tr></thead><tbody>'
    for partition, values in summary['splits'].items():
        body += '<tr><td>'+partition+'</td>'+''.join('<td>'+str(values[name])+'</td>' for name in ('fragments','groups','source_targets','targets'))+'</tr>'
    body += '</tbody></table><p>70／15／15按绑定组的候选量安排；引用和重复组优先保持完整。跨区共有短句保留上下文，但不再作为后一区的独立参考目标。</p>'
    body += '<h2>审阅范围</h2><p>规则覆盖明确格式的账号、电话、邮箱、链接和长标识。自由文本中的其他姓名、地址等尚未人工核对，当前不标记为可直接外发。最终留出正文默认封存，不用于本阶段挑提示或表达。</p>'
    body += '<nav><a href="changes.html">脱敏前后变更</a><a href="manifest.json">版本与核对清单</a></nav><h2>学习／开发片段索引</h2><nav>'
    body += ''.join('<a href="fragments-'+str(n+1)+'.html">审阅页 '+str(n+1)+'</a>' for n in range(page_count))+'</nav>'
    (output/'index.html').write_text(_page('真实私聊数据集 E.L2 审阅',body),encoding='utf-8')
    manifest = {'version':'corpus-dataset-export-v1','view':status,'recipe':payload['recipe'],
                'verification':verified,'files':catalog,'private':True,'final_holdout_text_in_review_pages':False,
                'audit_files_are_model_input':False,'model_input_approved':False,'model_calls':0}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'output':str(output),'view_id':view_id,'review_pages':page_count,'redaction_review_messages':len(changed),
            'final_holdout_sealed':True,'model_input_approved':False,'model_calls':0}
