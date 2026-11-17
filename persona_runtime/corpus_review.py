"""Offline private review pages; raw message text is HTML-escaped, never executed."""
from html import escape
import json
from pathlib import Path

from .corpus_files import CorpusError
from .corpus_store import outside_repository


CSS = '''
body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;background:#f5f7f7;color:#183137;margin:0}
main{max-width:1000px;margin:32px auto;padding:24px;background:white;border:1px solid #dce5e4;border-radius:10px}
a{color:#086d74}h1{font-size:26px}h2{font-size:21px}.record{padding:16px 0;border-bottom:1px solid #dce5e4}
.meta{color:#566c70;font-size:14px}.text,blockquote{white-space:pre-wrap;overflow-wrap:anywhere}
.text{padding:12px 16px;background:#eff5f3;border-radius:8px}.excluded .text{background:#f3f3f3;color:#667376}
blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid #afc6c0;color:#566c70}
nav{display:flex;gap:18px;flex-wrap:wrap;margin:16px 0}table{border-collapse:collapse;width:100%}
td,th{padding:8px;border:1px solid #dce5e4;text-align:left}code{overflow-wrap:anywhere}
@media(max-width:650px){main{margin:10px;padding:16px}h1{font-size:23px}}
'''
ROLES = {'self': '本人', 'peer': '对方', 'system': '系统'}


def _page(title, body):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + escape(title) + '</title><style>' + CSS + '</style></head><body><main>' + body + '</main></body></html>')


def export_review(store, parse_id, output):
    store.verify_parse(parse_id)
    output = outside_repository(output)
    if output.exists() and any(output.iterdir()):
        raise CorpusError('corpus_review_empty_target_required')
    output.mkdir(parents=True, exist_ok=True)
    with store.connection() as db:
        parse = db.execute('SELECT * FROM corpus_parses WHERE id=?', (parse_id,)).fetchone()
        store._dataset(db, parse['dataset_id'])
        status = store._parse_status(db, parse_id)
        json_raw = store._import_bytes(db, parse['dataset_id'], parse['json_import_id'], 'json')
        md_raw = store._import_bytes(db, parse['dataset_id'], parse['md_import_id'], 'markdown')
        rows = [dict(row) for row in db.execute('SELECT * FROM corpus_messages WHERE parse_id=? ORDER BY ordinal', (parse_id,))]
    originals = json.loads(json_raw.decode('utf-8-sig'))['messages']
    summary = status['summary']
    per_page = 500
    pages = (len(rows) + per_page - 1) // per_page
    for number in range(pages):
        def nav():
            links = ['<a href="index.html">数据核对摘要</a>']
            if number:
                links.append('<a href="records-' + str(number) + '.html">上一页</a>')
            if number + 1 < pages:
                links.append('<a href="records-' + str(number + 2) + '.html">下一页</a>')
            return '<nav>' + ''.join(links) + '</nav>'
        parts = ['<h1>私聊原文核对</h1><p class="meta">仅本机审阅；文字候选仍需后续隐私、分区与模型输入核对。</p>', nav()]
        for row in rows[number * per_page:(number + 1) * per_page]:
            original = originals[row['ordinal']]
            candidate = bool(row['target_candidate'])
            parts.append('<section class="record' + ('' if candidate else ' excluded') + '">')
            parts.append('<div class="meta">#' + str(row['ordinal'] + 1) + ' · ' + escape(row['time_str']) + ' · ' + ROLES[row['role']] +
                         ' · ' + ('顶层文字候选' if candidate else '保留记录，非本次文字学习目标') + '</div>')
            parts.append('<div class="text">' + escape(row['text']) + '</div>')
            quote = original.get('quote')
            if quote:
                parts.append('<details><summary>被引用原文（不重复计入本次发言）</summary><blockquote>' + escape(quote.get('text', '')) + '</blockquote></details>')
            if original.get('forward'):
                parts.append('<p class="meta">合并转发单独保留，未把转发中的发言归给当前发送者。</p>')
            parts.append('<div class="meta">JSON位置：' + escape(row['source_locator']) +
                         '/text · MD字符位置：' + str(row['md_text_start']) + '—' + str(row['md_text_end']) + '</div></section>')
        parts.append(nav())
        (output / ('records-' + str(number + 1) + '.html')).write_text(_page('私聊原文核对', ''.join(parts)), encoding='utf-8')
    items = [
        ('原始记录', summary['records']), ('文字候选', summary['text_candidates']),
        ('本人文字候选', summary['candidate_speakers'].get('self', 0)),
        ('对方文字候选', summary['candidate_speakers'].get('peer', 0)),
        ('引用回复', summary['reply_edges']), ('可定位引用', summary['resolved_replies']),
        ('原文件内未找到引用目标', summary['unresolved_replies']),
        ('合并转发容器', summary['forward_containers']),
        ('JSON与MD全文核对', '一致'), ('数据用途', '作者授权双方真人表达作为后续训练输入'),
        ('当前模型调用', 0), ('学习输入版本批准', '尚未进行'),
    ]
    body = '<h1>微信私聊语料核对</h1><p>原件已保留，双方原文分别登记；此页是数据审阅，不是模型学习结果。</p>'
    body += '<p>时间范围：' + escape(summary['first_time']) + ' 至 ' + escape(summary['last_time']) + '</p>'
    body += '<table><thead><tr><th>项目</th><th>结果</th></tr></thead><tbody>'
    body += ''.join('<tr><td>' + escape(k) + '</td><td>' + escape(str(v)) + '</td></tr>' for k, v in items) + '</tbody></table>'
    body += '<p>媒体、系统消息、合并转发和未确认的应用文字均保留在原件，未直接当作真人顶层文字候选。候选尚未经过隐私与学习分区处理。</p>'
    body += '<nav><a href="source.md">原始Markdown副本</a><a href="summary.json">核对数据</a></nav><h2>完整原文索引</h2><nav>'
    body += ''.join('<a href="records-' + str(i + 1) + '.html">' + str(i * per_page + 1) + '—' + str(min((i + 1) * per_page, len(rows))) + '</a>' for i in range(pages))
    body += '</nav>'
    (output / 'index.html').write_text(_page('微信私聊语料核对', body), encoding='utf-8')
    (output / 'source.md').write_bytes(md_raw)
    (output / 'summary.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'review_directory': str(output), 'record_pages': pages, 'records': len(rows), 'private': True, 'model_calls': 0}
