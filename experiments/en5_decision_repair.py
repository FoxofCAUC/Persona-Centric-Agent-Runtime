"""Freeze the strict-tool revision for the same sixteen positions, for author review."""
import argparse
from html import escape
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.deepseek_reply_decision import BASE, VERSION
from persona_runtime.corpus_store import outside_repository
from persona_runtime.private_personas import provider_identity
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en5_decision_contract import flash_wire, require
from .en5_decision_packet import build, file_hash
from .en5_decision_tool import MODE, decision_pair

CONTRACT = 'inputs.private.json'
REVIEW = 'inputs.html'


def repair_folder(root):
    return root/'d2'/MODE


def assemble(root, packet, docs, old_flash, old_permit):
    first = root/'d2/results.private.json'
    old = read_private(first)
    require(old['d1_sha256'] == old_permit['d1_results']
            and set(old['cases']) == {r['position'] for r in docs['positions']}, 'repair_first_scope')
    cases = []
    for case, row in zip(packet['cases'], docs['positions'], strict=True):
        pos = case['position']
        old_wire = row['flash']
        require(row['position'] == pos and row['scope'] == case['scope']
                and flash_wire(case['flash_base'], docs['author_text']['flash']) == old_wire
                and fingerprint(old_wire) == row['flash_sha256'] == old['cases'][pos]['flash']['request_hash'],
                'repair_frozen_input_changed')
        model, wire = decision_pair(case['flash_base'], docs['author_text']['flash'], case['targets'], old_flash['timeout'])
        require(wire['messages'][1:] == old_wire['messages'][1:], 'repair_dialogue_changed')
        cases.append({'position': pos, 'old_flash_sha256': row['flash_sha256'], 'model': model, 'wire': wire,
                      'flash_sha256': fingerprint(wire), 'author_decision': row['decision'],
                      'original_pro_sha256': row['original'], 'd1_pro_sha256': row['pro_sha256']})
    require(len(cases) == 16 and len({c['flash_sha256'] for c in cases}) == 16, 'repair_count')
    return {'version': VERSION, 'stage': 'D2-repair', 'original_d2_results_sha256': file_hash(first),
            'd0_review_sha256': old_permit['material_approval'], 'd0_requests_sha256': old_permit['requests'],
            'd1_results_sha256': old_permit['d1_results'], 'd1_stage_review_sha256': old_permit['d1_stage_review'],
            'original_provider': {'base_url': old_flash['base_url'], 'name': old_flash['name']},
            'provider': {'base_url': BASE, 'name': old_flash['name']}, 'first_d2': old['stats'],
            'approved_flash_text': docs['author_text']['flash'], 'approved_pro_text': docs['author_text']['pro'],
            'pro_policy': 'reuse_exact_d1_or_v8_else_only_valid_changed_decisions',
            'draft_use': 'awaiting_author_review', 'cases': cases}


def page(value, docs, checksum):
    old = {r['position']: r['flash'] for r in docs['positions']}
    def pre(obj):
        return '<pre>'+escape(json.dumps(obj, ensure_ascii=False, indent=2))+'</pre>'
    nodes = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>D2 · Flash 决策接口修复版</title>',
             '<style>body{max-width:68rem;margin:1rem auto;padding:1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf4ef;padding:.8rem}article{border-top:1px solid #ccd8d0;padding:.6rem 0}nav a{margin-right:.6rem}details{padding:.3rem 0}code,small{overflow-wrap:anywhere}summary{cursor:pointer}</style>',
             '<h1>D2 · Flash 决策接口修复版</h1>',
             '<p>首批16次 Flash 均返回完整 HTTP 回执。14次沿用了聊天消息的 id、source、text 字段，另2次'
             '无法解析为单个 JSON 对象。修复版改用非思考模式下的指定函数与严格参数校验。函数参数只作本地判断记录。</p>',
             '<p>Flash 目标地址 <code>'+escape(value['provider']['base_url']+'/chat/completions')
             +'</code>。每份请求包含已审 Flash 原文、人物投影及原轮次前文。工具字段 target、action、task_state 必填；'
             '目标编号0和两项未定成对出现时，PCAR 使用原 v8 回复。'
             '若本次有效判断得到的 Pro 完整请求与 D1 相同，即复用 D1 首答。</p>',
             '<p><a href="https://api-docs.deepseek.com/guides/tool_calls/">DeepSeek 官方严格模式说明</a> · '
             '<a href="../review/decisions.html">查看首次 D2 原始回执</a></p>',
             '<p>本批请求版本 <code>'+checksum+'</code></p><nav>']
    nodes.extend(f'<a href="#p-{c["position"]}">{c["position"]}</a>' for c in value['cases'])
    nodes.append('</nav>')
    for c in value['cases']:
        pos=c['position']
        nodes.extend([f'<article id="p-{pos}"><h2>{pos}</h2>',
                      '<p>作者判断供审阅对照</p>'+pre(c['author_decision']),
                      '<details><summary>修复版 Flash 完整请求</summary>'+pre(c['wire'])+'</details>',
                      '<details><summary>首次 D2 的完整请求</summary>'+pre(old[pos])+'</details>',
                      '<small>修复版请求摘要 '+c['flash_sha256']+'</small></article>'])
    nodes.append('</html>')
    return ''.join(nodes)


def prepare(root):
    from .en5_decision_d2 import materials
    root = outside_repository(root)
    packet, docs, _, _, _, old_flash, old_permit = materials(root)
    require(fingerprint(build(Path(packet['prior']),packet['upstream'])) == fingerprint(packet), 'repair_source_changed')
    value = assemble(root, packet, docs, old_flash, old_permit)
    folder = repair_folder(root)
    checksum = save_once(folder/CONTRACT, value)
    save_text_once(folder/REVIEW, page(value, docs, checksum))
    return folder/REVIEW


def load(root, packet, docs, old_flash, old_permit):
    """Validate the reviewed artifact and derive the exact independent-use scope."""
    folder = repair_folder(root)
    value = read_private(folder/CONTRACT)
    checksum = file_hash(folder/CONTRACT)
    require(value == assemble(root, packet, docs, old_flash, old_permit)
            and (folder/REVIEW).read_bytes() == page(value, docs, checksum).encode(), 'repair_review_changed')
    by_slot = {c['position']+'.decision':(c['model'], c['wire']) for c in value['cases']}
    permit = {**old_permit, 'version': 'en5-d2-strict-tool-use-v1',
              'provider': provider_identity(value['provider']),
              'slots': {s: fingerprint(w) for s,(_,w) in by_slot.items()},
              'repair_contract_sha256': checksum, 'repair_review_sha256': file_hash(folder/REVIEW),
              'original_d2_results_sha256': value['original_d2_results_sha256']}
    return by_slot, permit


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root', type=Path, required=True)
    print(prepare(cli.parse_args().root))
