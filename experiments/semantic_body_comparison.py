"""Prepare the private E.L4 body comparison from approved inputs and first receipts."""
import argparse
from html import escape
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_learning import CorpusLearning
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.provider_compat import build_request
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial import TrialLedger, assemble_new, authority_guard, parse_receipt, public_model
from .corpus_reply_trial_report import save_text_once
from .corpus_semantic_selector import finalize_selection


VERSION = 'el4-semantic-body-comparison-v1'
DIRECTORY = 'body-comparison-v1'
POSITIONS = set(range(1, 17))


def require(condition, reason):
    if not condition:
        raise CorpusError('semantic_body_' + reason)


def file_hash(path):
    return digest(outside_repository(path).read_bytes())


def frozen_json(path, expected_hash):
    raw = outside_repository(path).read_bytes()
    require(digest(raw) == expected_hash, 'source_hash_changed')
    return json.loads(raw)


def complete_receipt(ledger, position, kind):
    row = ledger.row(f'{position:02}.{kind}')
    request = json.loads(row['request_json'])
    require(row['state'] == 'completed' and row['http_status'] == 200 and row['attempt_id']
            and row['request_hash'] == fingerprint(request) and row['raw_response'] is not None
            and row['raw_hash'] == digest(row['raw_response']), 'old_receipt_changed')
    returned = parse_receipt(row['raw_response'], request.get('stream', False))
    require(returned['valid'] and returned['model'] == request['model']
            and returned == json.loads(row['result_json']), 'old_result_invalid')
    return row, request, returned


def reuse_slot(selected, target, prior):
    # The baseline for an empty selection is fixed even when two old calls
    # returned different bodies for that identical request.
    matches = [slot for slot in ('C', 'D') if fingerprint(target) == fingerprint(prior[slot])]
    if not selected:
        require('C' in matches, 'empty_selection_not_control')
    return matches[0] if matches else None


class ReviewedSources:
    def __init__(self, decision_path):
        self.decision_path = outside_repository(decision_path)
        self.selector_root = self.decision_path.parent
        self.output = self.selector_root / DIRECTORY
        self.decision = read_private(self.decision_path)
        require(self.decision.get('version') == 'semantic-selection-author-overall-review-v1'
                and self.decision.get('selection_suitability_review_passed') is True, 'selector_review_required')
        self.decision_hash = file_hash(self.decision_path)
        self.selector = frozen_json(self.selector_root / 'selector-manifest.private.json',
                                    self.decision['selector_manifest_sha256'])
        results = frozen_json(self.selector_root / 'result-review-v1/results.private.json',
                              self.decision['result_set_sha256'])
        summary = frozen_json(self.selector_root / 'result-review-v1/summary.private.json',
                              self.decision['summary_sha256'])
        require(results['summary'] == summary and summary['states'] == {'completed': 16}
                and results['manifest_sha256'] == self.decision['selector_manifest_sha256']
                and fingerprint(results['cases']) == results['result_set_hash'], 'wrong_selection_results')
        self.study = outside_repository(self.selector['study_root'])
        self.plan = frozen_json(self.study / 'plan.private.json', self.selector['study_plan_sha256'])
        self.model = public_model('deepseek-v4-pro', 1.0)
        self.positions = {p['position']: p for p in self.plan['positions']}
        self.cases = {c['position']: c for c in results['cases']}
        self.packets = {p['position']: p for p in self.selector['packets']}
        require(all(len(rows) == 16 and set(rows) == POSITIONS for rows in
                    (self.positions, self.cases, self.packets)), 'invalid_positions')
        require(len(results['cases']) == len(self.plan['positions']) == len(self.selector['packets']) == 16,
                'duplicate_position')
        require({a['id']: a for a in self.plan['assets']['assets']} == results['assets'], 'asset_snapshot_changed')
        self.old = TrialLedger(self.study / 'ledger')
        binding = read_private(Path(self.plan['approval_path']).with_suffix('.run-binding.private.json'))
        plan_hash = fingerprint(self.plan)
        require(Path(binding['ledger']).resolve() == self.old.path and binding['plan_hash'] == plan_hash,
                'wrong_original_ledger')
        with self.old.connection() as db:
            require(tuple(db.execute('SELECT plan_hash,cancelled FROM trial_meta').fetchone()) == (plan_hash, 0),
                    'old_ledger_unavailable')
            self.past_hashes = {r[0] for r in db.execute('SELECT request_hash FROM trial_calls WHERE attempt_id IS NOT NULL')}
        corpus = DatasetCorpusStore(Path(self.plan['corpus_root']))
        learning = CorpusLearning(Path(self.plan['learning_root']), corpus)
        self.guard = authority_guard(self.plan, corpus, learning)

    def position_packet(self, n):
        require(file_hash(self.decision_path) == self.decision_hash, 'review_changed')
        self.guard({'position': n})
        pos, case, spec = self.positions[n], self.cases[n], self.packets[n]
        folder = self.selector_root / 'selector-calls' / f'{n:02}'
        packet = frozen_json(self.selector_root / spec['path'], spec['sha256'])
        outcome = finalize_selection(packet, (folder / 'response.private.bytes').read_bytes(),
                                     read_private(folder / 'response-meta.private.json'))
        require(outcome == read_private(folder / 'outcome.private.json') and outcome['state'] == 'completed'
                and case['response_hash'] == outcome['raw_sha256'] and case['new_ids'] == outcome['selected_proposal_ids']
                and packet['candidate_numbers'] == self.selector['candidate_numbers']
                and packet['position'] == n and packet['input_hash'] == case['input_hash'] == pos['input_text_hash']
                and case['input'] == pos['input_text'] and case['request_hash'] == fingerprint(packet['request']),
                'selector_receipt_mismatch')
        previous = {k: complete_receipt(self.old, n, k) for k in ('U', 'B', 'C', 'D')}
        u_row, _, understanding = previous['U']
        moment = self.plan['captures'][str(n)]['accepted_at']
        require(u_row['raw_hash'] == packet['understanding_response_hash'], 'understanding_changed')
        require(all(previous[k][0]['dependency_hash'] == u_row['raw_hash']
                    and json.loads(previous[k][0]['model_json']) == self.model for k in ('C', 'D')),
                'control_context_changed')
        def assemble(ids):
            messages = assemble_new(self.plan['prompts'], self.plan['binding'],
                                    pos | {'selection': {'selected_ids': ids}}, moment,
                                    understanding=understanding['body'], assets=self.plan['assets'])
            return build_request(self.model, messages)[0]
        require(assemble([]) == previous['C'][1] and assemble(pos['selection']['selected_ids']) == previous['D'][1],
                'original_assembly_changed')
        selected = case['new_ids']
        request = assemble(selected)
        reuse = reuse_slot(selected, request, {k: previous[k][1] for k in ('C', 'D')})
        require(reuse or fingerprint(request) not in self.past_hashes, 'prior_request_unaccounted_for')
        references = {k: {'slot': row['slot'], 'request_hash': row['request_hash'], 'raw_hash': row['raw_hash'],
                          'body_hash': result['body_hash']} for k, (row, _, result) in previous.items()}
        return {'position': n, 'selected_ids': selected, 'input_hash': case['input_hash'],
                'selection_receipt': case['response_hash'], 'references': references,
                'reuse': reuse, 'model': self.model, 'request_hash': fingerprint(request), 'request': request}


def make_key(positions):
    # Counterbalance the side occupied by the candidate independently in each comparison.
    rng = SystemRandom()
    key = {str(n): {} for n in sorted(positions)}
    for control in ('C', 'B'):
        sides = ['left', 'right'] * (len(positions) // 2) + (['left'] if len(positions) % 2 else [])
        rng.shuffle(sides)
        for n, side in zip(sorted(positions), sides, strict=True):
            key[str(n)][control] = side
    return key


def prepare(decision_path):
    sources = ReviewedSources(decision_path)
    root = sources.output
    packets = [sources.position_packet(n) for n in sorted(POSITIONS)]
    new = [p for p in packets if p['reuse'] is None]
    require(len({p['request_hash'] for p in new}) == len(new), 'duplicate_new_request')
    cases = []
    for packet in packets:
        path = root / 'requests' / f"{packet['position']:02}.private.json"
        cases.append({k: packet[k] for k in ('position', 'reuse', 'selected_ids', 'request_hash', 'references')} |
                     {'file': path.relative_to(root).as_posix(), 'sha256': save_once(path, packet)})
    key_path = root / 'blind-key.private.json'
    if not key_path.exists():
        require(not (root / 'manifest.private.json').exists(), 'original_blind_key_missing')
        save_once(key_path, make_key(POSITIONS))
    manifest = {'version': VERSION, 'decision_path': str(sources.decision_path),
                'selector_approval_sha256': sources.decision_hash,
                'original_plan_sha256': sources.selector['study_plan_sha256'],
                'selector_manifest_sha256': sources.decision['selector_manifest_sha256'],
                'selected_results_sha256': sources.decision['result_set_sha256'],
                'controls': ['C', 'B'], 'reuse_order': ['C', 'D'], 'blind_key_sha256': file_hash(key_path),
                'model': sources.model, 'positions': cases, 'max_new_pro_requests': len(new),
                'other_model_requests': 0, 'retries': 0}
    manifest_hash = save_once(root / 'manifest.private.json', manifest)
    name = sources.plan['persona_snapshot']['persona_name']
    preview = [f'# {name}的正文对照 · 冻结输入', '',
               f'16个独立位置，其中{len(new)}个需要一次新的DeepSeek Pro正文请求。'
               f'其余{16-len(new)}个沿用与新请求等价的首次回执。', '',
               '所有位置使用原受理时间、原话和同一次Flash理解。审阅时分别比较新稿与原无库稿、新稿与原v8 Pro。'
               '空选材造成的相同正文只展示一份。', '',
               '新请求采用原Pro配置，关闭思考，温度1，输出上限8,192 tokens。每个位置至多一次。', '',
               f'本包清单摘要 `{manifest_hash}`', '',
               '| 位置 | 表达条数 | 正文来源 |', '| ---: | ---: | --- |']
    for row in cases:
        source = '新Pro请求' if not row['reuse'] else '复用原' + row['reuse'] + '回执'
        preview.append(f"| [{row['position']}]({row['file']}) | {len(row['selected_ids'])} | {source} |")
    save_text_once(root / '输入与调用范围.md', '\n'.join(preview) + '\n')
    blocks = []
    for row, packet in zip(cases, packets, strict=True):
        source = '新Pro请求' if not row['reuse'] else '复用原' + row['reuse'] + '回执'
        blocks.append(f"<details><summary>位置 {row['position']} · {len(row['selected_ids'])} 条表达 · {source}</summary>"
                      f"<p><a href='{row['file']}'>冻结请求和来源摘要</a></p>"
                      f"<pre>{escape(json.dumps(packet['request'], ensure_ascii=False, indent=2))}</pre></details>")
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>正文对照 · 输入与调用范围</title><style>body{max-width:60em;margin:2em auto;padding:0 1em;'
            'font:16px/1.7 system-ui,sans-serif;color:#26382e}details{padding:.7em 0;border-bottom:1px solid #ddd}'
            'summary{cursor:pointer}a{color:#316847}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><main>'
            f'<h1>{escape(name)}的正文对照</h1><p>16个位置，{len(new)}次新增Pro请求，{16-len(new)}份正文直接复用。'
            '两项对照分别使用相同理解下的无库正文和原v8 Pro正文。</p>'
            '<p>逐位展开可查看模型收到的完整请求。原受理时间、原话、理解与人物提示均按之前的版本装配。'
            'DeepSeek Pro关闭思考，温度1，每次最多输出8,192 tokens。</p>'
            + ''.join(blocks) + '</main></html>')
    save_text_once(root / 'input-review.html', html)
    return root, manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection-approval', type=Path, required=True)
    args = parser.parse_args()
    output, plan = prepare(args.selection_approval)
    print(json.dumps({'input_review': str(output / 'input-review.html'), 'new_requests_proposed': plan['max_new_pro_requests'],
                      'reuse': [p['position'] for p in plan['positions'] if p['reuse']], 'calls_made': 0}, ensure_ascii=False))
