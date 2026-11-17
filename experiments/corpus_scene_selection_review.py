"""Read-only selection repair evidence and verbatim-selector approval package."""
import argparse
from html import escape
import json
from pathlib import Path

from persona_runtime.corpus_files import digest
from .corpus_reply_study import read_private
from .corpus_reply_trial_report import save_text_once


def pre(value):return '<pre>' + escape(value) + '</pre>'


def export(study_root, root):
    manifest = read_private(root / 'selector-manifest.private.json')
    variants = [(name, read_private(study_root / folder / 'summary.private.json'),
                  read_private(study_root / folder / 'candidate.private.json')) for name, folder in
                 [('只看情境语义', 'selection-repair-v1'), ('另加前文同义门', 'selection-repair-v2'), ('同方向真实前文', 'selection-repair-v3')]]
    final = variants[-1][2]
    assets = {a['id']: a for a in final['assets']}
    body = '<header><p class="eyebrow">PCAR · 选材与情境匹配研发审阅</p><h1>选材修订：回归证据与待审方案</h1><p>本轮保持人物提示和资产原文，复用16份真人输入及首次Flash理解。只进行了本地Embedding计算，没有新生成回复，也没有新增云端LLM请求。</p><p class="notice">本地修订仍有否定／接话意图误配，尚未验收或启用。已准备MaiBot原有语义终选接口、完整原文和16份实际请求预览，等待你决定是否进行有限验证。</p><nav><a href="#issues">已核对的问题</a><a href="#source">上游原文</a><a href="#requests">16份完整输入</a><a href="#local">本地选材回放</a><a href="#budget">拟议验证范围</a></nav></header>'
    body += '<section id="issues"><h2>本地回归没有被当成通过</h2><p>词面旧方案在13个位置采用27条表达。情境语义筛选去掉了“一般”的误命中，但仍把游戏机制讨论召回成游戏自夸。增加前文同义门又把真实来源全部挡住，说明“对方说什么”和“自己如何接话”不能直接按同义文本判断。</p><p>同方向前文版本保留15条来源前文缺失的事实，不替它们编造触发场景；其余18条只有时间相邻证据，未冒充明确回复关系。它在16位中只推荐1条，但这条仍不合适：</p>'
    for case in final['cases']:
        if case['new_selection']['selected_ids']:
            body += '<h3>位置' + str(case['position']) + '：原话与实际理解</h3>' + pre(case['input_text']) + pre(case['actual_understanding']['body'])
            for ident in case['new_selection']['selected_ids']:
                a = assets[ident]
                body += '<p>本地候选情境／说法</p>' + pre(a['situation'] + '\n' + a['style'])
    body += '<p>这处把“没休息、还要上班”误召回成午间道别，说明减少选材数量不等于判断正确。三版结果完整保留，未通过调高阈值或加词语黑名单掩盖反例。</p><details><summary>三版数量及来源回找记录</summary><table><tr><th>研发版</th><th>非空位置</th><th>条目总数</th><th>来源回找</th></tr>'
    for label, s, c in variants:
        body += '<tr><td>' + escape(label) + '</td><td>' + str(s['new_nonempty_positions']) + '</td><td>' + str(s['new_selected_total']) + '</td><td>' + str(s['source_roundtrip_returned']) + ' / ' + str(s['source_roundtrip_total']) + '</td></tr>'
    body += '</table><p>来源回找只是索引检查，使用真实来源原文，不是新生成理解，也不是独立自然度或适用率验证。</p></details></section>'
    body += '<section id="source"><h2>待审方向：候选召回与语义终选分开</h2><p>MaiBot的向量档在候选池之后，还有独立选择器根据真实上下文选择0—若干条表达。本轮已复用该选择器原文，逐字保留；没有另写角色口吻、场景清单或模拟对话。</p>'
    body += '<div class="flow"><span>真实原话＋已存理解</span><span>→</span><span>表达候选池</span><span>→</span><span>语义终选0—5条</span><span>→</span><span>原Pro自行发言</span></div>'
    body += '<p>选择器只返回编号，不回答用户、不重制正文、不裁决主回复事实。候选无效或选择不可用时，不使用表达库，不做随机／词面兜底补齐。</p><h3>MaiBot完整原文</h3>'
    body += pre((root / 'maibot-selector-original.prompt').read_text(encoding='utf-8-sig'))
    source = manifest['upstream_source']
    body += '<p><a href="' + escape(source['source_url'], quote=True) + '">固定上游源码</a> · <a href="maibot-selector-original.prompt">本地原件</a> · Git blob校验一致</p>'
    body += '<p>当前库只有33条研究资产，拟议小验证先完整提供，避免让尚未可靠的本地门限漏掉候选；未来大库召回需要另行验证。编号只作本轮选择映射，来源原句和资产账目不发送。</p></section>'
    body += '<section id="requests"><h2>16份完整模型输入预览</h2><p>每份使用上游system原文＋33条既有情境／表达；user材料分别为原受理时间、未改写的当前原话、已保存Flash局面与事实。没有输入旧C／D正文、作者投票、角色完整故事或学习来源原句。</p>'
    for p in manifest['packets']:
        packet = read_private(root / p['path'])
        body += '<details' + (' open' if p['position'] == 2 else '') + '><summary>位置' + str(p['position']) + ' · 完整请求</summary><p><a href="' + p['path'] + '">下载冻结输入</a></p>'
        body += pre(json.dumps(packet['request'], ensure_ascii=False, indent=2)) + '</details>'
    body += '</section><section id="local"><h2>16位本地选材回放</h2><p>下面展示当前本地版本为何保留或退出条目，用于审阅机制局限；不将它标为可用策略，也不把旧正文伪称为新策略输出。</p>'
    def asset_list(ids):
        return ''.join('<div class="asset"><p>' + escape(assets[i]['situation']) + '</p>' + pre(assets[i]['style']) + '</div>' for i in ids) or '<p class="meta">本版未采用条目</p>'
    for case in final['cases']:
        body += '<details><summary>位置' + str(case['position']) + ' · 原词面' + str(len(case['old_selected_ids'])) + '条／本地修订' + str(len(case['new_selection']['selected_ids'])) + '条</summary>'
        body += pre(case['input_text']) + '<div class="columns"><div><h3>原词面</h3>' + asset_list(case['old_selected_ids']) + '</div><div><h3>本地修订（未验收）</h3>' + asset_list(case['new_selection']['selected_ids']) + '</div></div>'
        body += '<details><summary>全部33条的前文证据、分数和未采用原因</summary>'
        for record in case['new_selection']['audit']:
            a = assets[record['id']]
            cue = final['source_evidence'][record['id']]
            body += '<div class="asset"><h3>' + escape(a['situation']) + '</h3>' + pre(a['style'])
            if cue['available']:
                body += '<p>源片段里可见的较早对方发言（相邻不等于已确认回复）</p>' + pre(cue['peer_text'])
            else:body += '<p class="meta">现有源片段缺少更早对方发言，未补造。</p>'
            body += '<p>来源原句（仅供核对）</p>' + pre(cue['quote']) + pre(json.dumps(record, ensure_ascii=False, indent=2)) + '</div>'
        body += '</details></details>'
    body += '</section><section id="budget"><h2>需要你审阅的新增范围</h2><ul><li>是否接受在现有不增额外选择模型的路线之外，研究一次独立语义终选。</li><li>若认可本页原文及完整输入，拟最多新增16次Flash选择请求；每位一次，输出上限512 tokens，关闭思考，温度0.2。</li><li>只验证选材编号及适用性，复用原16份理解，不重跑原80次、不新生成Pro正文、不改已批准人物提示。</li><li>金额在取得真实回执后统计；当前没有新增LLM费用。新环节的额外等待和长期成本仍需后续评估。</li></ul><p class="notice">新选择器模板和新增16次范围尚未获准。当前接口默认不运行，只有另行绑定本包摘要的明确批准才可派发；正式实例和E.L4价值门保持原状态。</p><p><a href="selector-manifest.private.json">完整范围与版本清单</a> · <a href="upstream-source.private.json">来源核验</a></p></section>'
    css = 'body{margin:0;background:#f4f6ef;color:#263d33;font:16px/1.85 system-ui,"Microsoft YaHei",sans-serif;overflow-wrap:anywhere}main{max-width:1120px;margin:auto;padding:30px 22px 90px}header,section{background:white;border:1px solid #d9e2d2;border-radius:10px;padding:25px;margin-bottom:24px}h1{font-size:29px;line-height:1.4}h2{font-size:24px;line-height:1.5}h3{font-size:18px}.meta,.eyebrow{color:#68796a;font-size:14px}.notice{border-left:3px solid #a4b68c;padding-left:13px}nav,.flow{display:flex;flex-wrap:wrap;gap:9px 18px;margin:18px 0}.flow{background:#eef4e7;padding:16px;border-radius:7px}a{color:#25694f}pre{font:14px/1.8 ui-monospace,"Cascadia Code","Microsoft YaHei",monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5eb;padding:13px;border-radius:6px}details{border:1px solid #dce5d6;margin:16px 0;padding:13px;border-radius:7px}summary{cursor:pointer;font-weight:600}.columns{display:grid;grid-template-columns:1fr 1fr;gap:18px}.asset{border-bottom:1px solid #e0e7dc;margin:12px 0;padding:8px 0}table{width:100%;border-collapse:collapse}td,th{padding:9px;border:1px solid #dbe3d7;text-align:left}th{background:#eff4e9}@media(max-width:700px){main{padding:13px 10px 60px}header,section{padding:17px 13px}h1{font-size:24px}h2{font-size:22px}.columns{grid-template-columns:1fr}pre{font-size:13px;padding:10px}details{padding:10px}}'
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>选材与情境匹配修订审阅</title><style>' + css + '</style><main>' + body + '</main></html>'
    save_text_once(root / 'index.html', html)
    return {'review_page': str(root / 'index.html'), 'preview_requests': len(manifest['packets']),
            'new_driver_llm_calls': 0, 'new_selector_approval_required': True,
            'selector_manifest_sha256': digest((root / 'selector-manifest.private.json').read_bytes())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-root', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export(args.study_root, args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':main()
