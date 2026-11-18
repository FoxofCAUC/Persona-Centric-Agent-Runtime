"""Assemble a repaired private reply from the original v8 messages and approved material."""
import argparse
from collections import Counter
from difflib import SequenceMatcher
from html import escape
import json
from pathlib import Path
import re
import unicodedata

from persona_runtime.corpus_store import fingerprint
from persona_runtime.provider_compat import build_request
from .body_repair_review import freeze_sides
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .semantic_body_comparison import ReviewedSources, complete_receipt, file_hash, require
from .semantic_body_report import stored_body
from .corpus_reply_trial import TrialLedger


def content_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def projection_reason(asset):
    """Use long quote overlap only as an exclusion from this optional projection.

    General descriptions and short reactions may pass. An expression asset
    alone cannot establish that the character shared the source speaker's past.
    """
    style = asset['style']
    source = asset['source_quote']
    if '括号' in style and re.search(r'(?:（\s*）|\(\s*\))', source):
        return 'source_blank_parenthetical_instruction'
    for match in re.finditer(r'“([^”]*)”|「([^」]*)」|‘([^’]*)’|"([^"]*)"', style):
        example = next(group for group in match.groups() if group is not None)
        if '…' in example or '...' in example:
            return 'unbound_example_slot'
        quote, original = content_key(example), content_key(source)
        if SequenceMatcher(None, quote, original, autojunk=False).find_longest_match().size >= 4:
            return 'source_literal_four_or_more'
    return None


def repair_messages(baseline_messages, binding, thought, selected_assets, current_input):
    require(baseline_messages[-1] == {'role': 'user', 'content': current_input}, 'v8_target_changed')
    variables = binding['variables']
    chosen, omitted = [], {}
    for asset in selected_assets:
        reason = projection_reason(asset)
        if reason:
            omitted[asset['id']] = reason
        else:
            chosen.append(asset)
    blocks = []
    if chosen:
        wrap = variables['expression_habits_block']
        lines = [wrap['item_format'].format(situation=a['situation'], style=a['style']) for a in chosen]
        expressions = wrap['prefix'] + '\n'.join(lines)
        require(len(expressions + wrap['suffix_if_present']) <= 3000 and len(lines) <= 5, 'expression_capacity')
        blocks.append(expressions)
    if thought.strip() and thought.strip() != current_input.strip():
        blocks.append(variables['planner_reasoning']['format'].format(thought=thought))
    context = [{'role': 'user', 'content': '\n\n'.join(blocks)}] if blocks else []
    messages = [*baseline_messages[:-1], *context, baseline_messages[-1]]
    return messages, [a['id'] for a in chosen], omitted


class RepairInputs:
    def __init__(self, state_path):
        state = read_private(state_path)
        self.sources = ReviewedSources(state['selection_overall_approval'])
        self.body_root = self.sources.output
        results = read_private(Path(state['semantic_body_result_review_page']).with_name('results.private.json'))
        require(fingerprint(results['cases']) == results['result_set_hash'] == state['semantic_body_result_set_hash'],
                'prior_bodies_changed')
        self.source_result_hash = results['result_set_hash']
        self.previous = {c['position']: c for c in results['cases']}
        self.assets = {a['id']: a for a in self.sources.plan['assets']['assets']}
        self.semantic = TrialLedger(self.body_root / 'receipts')
        self.root = self.body_root / 'paired-repair-v1'
        self.state_path = state_path

    def packet(self, n):
        src = self.sources
        old_packet = src.position_packet(n)
        old = self.previous[n]
        b, wire, result = complete_receipt(src.old, n, 'B')
        u, _, understanding = complete_receipt(src.old, n, 'U')
        repaired, retained, omitted = repair_messages(wire['messages'], src.plan['binding'], understanding['body'],
                                                       [self.assets[i] for i in old_packet['selected_ids']], old['input'])
        model = json.loads(b['model_json'])
        request = build_request(model, repaired)[0]
        require(sum(len(m['content']) for m in repaired) <= model['max_input_chars'], 'body_capacity')
        require(wire['messages'][0] == repaired[0] and wire['messages'][-1] == repaired[-1]
                and request['thinking'] == {'type':'disabled'} and 'tools' not in request, 'repair_wire_changed')
        selected = old['bodies']['S']
        if old_packet['reuse']:
            prior_row, _, prior_result = complete_receipt(src.old, n, old_packet['reuse'])
            checked = {'raw_hash':prior_row['raw_hash'], 'body':prior_result['body']}
        else:
            checked = stored_body(self.semantic, f'{n:02}.semantic')
        require(checked['body'] == selected['body'] and checked['raw_hash'] == selected['raw_hash']
                and old_packet['request_hash'] == selected['request_hash']
                and old['bodies']['B']['body'] == result['body'] and old['bodies']['B']['raw_hash'] == b['raw_hash'],
                'control_receipt_changed')
        controls = {k:{f:old['bodies'][k][f] for f in ('state','body','request_hash','raw_hash','slot')} for k in ('B','S')}
        request_hash = fingerprint(request)
        reuse = next((v for v in controls.values() if v['request_hash'] == request_hash), None)
        require(reuse or request_hash not in src.past_hashes, 'unaccounted_prior_request')
        return {'position':n, 'input':old['input'], 'model':model, 'request':request, 'request_hash':request_hash,
                'controls':controls,'reuse':reuse,'selected':old_packet['selected_ids'],'retained':retained,
                'omitted':omitted,'understanding_raw_hash':u['raw_hash'],'selection_raw_hash':old_packet['selection_receipt']}

    def guard(self, packet):
        require(self.packet(packet['position']) == packet, 'repair_source_or_packet_changed')

    def prepare(self):
        positions = [self.packet(n) for n in sorted(self.previous)]
        key_hash = freeze_sides(self.root, [p['position'] for p in positions])
        require(len({p['request_hash'] for p in positions}) == len(positions),'duplicate_repair_wire')
        manifest = {'version':'reviewed-body-repair-v1','study_id':'el4-private-im-repair-01',
                    'source_home':self.sources.plan['source_home'],'state_path':str(self.state_path),
                    'source_result_hash':self.source_result_hash,'blind_key_sha256':key_hash,'positions':positions,
                    'study':'paired development regression, fixed real inputs','role_prompt_source':'captured original v8 system',
                    'current_input':'original v8 user message, once','materials':'same approved fields and optional verbatim assets',
                    'max_dispatches':None,'retries':0,'formal_activation':False}
        manifest_path = self.root / 'manifest.private.json'
        save_once(manifest_path, manifest)
        def panel(p):
            return (f'<details><summary>位置 {p["position"]} · 选入 {len(p["selected"])} 条，实际传入 {len(p["retained"])} 条</summary>'
                    + ''.join('<h3>'+escape(m['role'])+'</h3><pre>'+escape(m['content'])+'</pre>' for m in p['request']['messages'])+'</details>')
        html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>修复后实际输入审阅</title><style>body{max-width:60em;margin:2em auto;padding:0 1em;font:16px/1.7 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}details{padding:.6em 0;border-bottom:1px solid #ccc}</style>'
                '<h1>私聊回复修复后的完整输入</h1><p>每个位置使用原v8人物系统消息，随后提供已保存的对话理解和可迁移表达，末尾放当轮真人原话。'
                '可展开核对每条模型可见消息。角色、输入、理解和资产文字均取自已审版本。</p>'
                + ''.join(panel(p) for p in positions) + '</html>')
        save_text_once(self.root/'inputs.html', html)
        return manifest_path, manifest


if __name__ == '__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--state', type=Path, required=True)
    args=cli.parse_args()
    manifest, plan=RepairInputs(args.state).prepare()
    print(json.dumps({'path':str(manifest),'sha256':file_hash(manifest),'positions':len(plan['positions']),
                      'selected':sum(len(p['selected']) for p in plan['positions']),
                      'retained':sum(len(p['retained']) for p in plan['positions']),
                      'omitted_reasons':dict(Counter(v for p in plan['positions'] for v in p['omitted'].values())),
                      'new_calls':sum(p['reuse'] is None for p in plan['positions'])},ensure_ascii=False))
