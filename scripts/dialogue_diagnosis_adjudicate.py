"""CR1 post-review append-only adjudication; never regenerate or reselect review pairs.

The sealed scorer, first strict reading and originally presented A/B mapping stay
unchanged. User policy changes only the disputed draft boundary in a new view.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_frame import require
from scripts import dialogue_diagnosis_analysis as analysis
from scripts import dialogue_diagnosis_real as real
from scripts.dialogue_refactor import offline


def validate_review(review):
    require(len(review) == 12 and {r['number'] for r in review} == set(range(1, 13)), 'cr1_user_review_coverage')
    require(all(r['choice'] in ('A', 'B', 'tie', 'unable') for r in review), 'cr1_user_review_choice')
    return {r['number']: r for r in review}


def original_mapping(pairs, cases, protocol):
    result = []
    for number, pair in enumerate(analysis.review_subset(pairs, cases), 1):
        ids = [pair['candidate'], pair['control']]
        if int(hashlib.sha256((protocol['review']['anonymous_orientation_seed'] + pair['id']).encode()).hexdigest(), 16) % 2:
            ids.reverse()
        result.append({'number': number, 'pair': pair['id'], 'A': ids[0], 'B': ids[1], 'stratum': pair['stratum']})
    return result


def apply_draft_policy(rows, decision):
    require(decision['kind'] == 'ordinary_courtesy_in_unsent_draft', 'cr1_adjudication_kind')
    require(decision['choice'] in ('allow', 'disallow'), 'cr1_adjudication_choice')
    result = deepcopy(rows)
    by_id = {row['position_id']: row for row in result}
    changes = []
    if decision['choice'] == 'allow':
        for target in decision['affected_positions']:
            require(target.startswith('D10-') and target in by_id, 'cr1_adjudication_target')
            row = by_id[target]
            require(len(row['boundaries']) == 1 and row['boundaries'][0]['status'] == 'violated', 'cr1_adjudication_boundary')
            before = deepcopy(row['boundaries'][0])
            row['boundaries'][0] = {**before, 'status': 'respected', 'reason':
                '用户确认普通礼貌回请可作为未发送拟稿的自然发挥；此处没有实际发送或执行，不按首版人工Spec额外限制记为边界错误。'}
            changes.append({'position': target, 'before': before, 'after': row['boundaries'][0]})
    require(all(a['dimensions'] == b['dimensions'] and a['output_hash'] == b['output_hash'] and
                a['obligations'] == b['obligations'] and a['sample_use'] == b['sample_use']
                for a, b in zip(rows, result)), 'cr1_adjudication_changed_scores_or_outputs')
    return result, changes


def resolve_review(review, mapping, strict_pairs, policy_pairs):
    by_number = validate_review(review)
    strict, policy = ({p['id']: p for p in values} for values in (strict_pairs, policy_pairs))
    require(len(mapping) == 12 and {m['number'] for m in mapping} == set(range(1, 13)), 'cr1_review_mapping_coverage')
    result = []
    for item in mapping:
        vote = by_number[item['number']]
        pair = policy[item['pair']]
        chosen = item[vote['choice']] if vote['choice'] in ('A', 'B') else None
        verdict = ('win' if chosen == pair['candidate'] else 'loss' if chosen == pair['control'] else vote['choice'])
        require(chosen is None or chosen in (pair['candidate'], pair['control']), 'cr1_review_mapping_invalid')
        result.append({**item, 'user_choice': vote['choice'], 'chosen_position': chosen,
                       'chosen_condition': chosen.split('/')[1] if chosen else None,
                       'comparison': pair['comparison'], 'user_candidate_verdict': verdict,
                       'user_reason': vote.get('reason'), 'machine_strict_verdict': strict[item['pair']]['verdict'],
                       'machine_user_policy_verdict': pair['verdict'],
                       'scene': pair['scene'], 'role': pair['role']})
    return result


def adjudicate(root):
    root = real.base.confined(root)
    prepared = real.check_freeze(root)
    real.verify(root)
    real.protect()
    folder = root / 'analysis'
    materials, protocol = real.read(root / 'materials.json'), real.read(root / 'protocol.json')
    cases = {c['id']: c for c in materials['cases']}
    strict_pairs = real.read(folder / 'comparisons-strict-v1.json')
    mapping = real.read(folder / 'review-mapping-local.json')
    require(mapping == original_mapping(strict_pairs, cases, protocol), 'cr1_presented_review_mapping_changed')
    review = real.read(folder / 'user-review.json')
    decision = real.read(folder / 'boundary-adjudication.json')
    rows = real.read(folder / 'machine-assessments-strict-v1.json')
    results = {v['position']['id']: v for p in (root / 'results').glob('*.json') if (v := real.read(p))}
    analysis.validate_assessments(rows, results, materials, protocol)
    revised, changes = apply_draft_policy(rows, decision)
    assessments = analysis.validate_assessments(revised, results, materials, protocol)
    contexts = {i['key']: real.read(root / 'contexts' / (i['key'] + '.json')) for i in prepared['items']}
    pairs = analysis.compare(results, assessments, prepared, contexts, protocol)
    resolved = resolve_review(review, mapping, strict_pairs, pairs)
    summaries = {}
    for comp in ('B1-B0', 'B2-B1', 'B2-B0'):
        group = [p for p in pairs if p['comparison'] == comp]
        subset = [p for p in group if p['component_attribution_eligible']]
        human = [p for p in resolved if p['comparison'] == comp]
        interval = analysis.scene_interval(group)
        summaries[comp] = {'full_machine_pairs': len(group), 'machine_user_policy_verdicts': dict(Counter(p['verdict'] for p in group)),
            'naturalness_delta': interval, 'research_signal_met': interval['mean'] >= .5,
            'new_boundary_error_pairs': sum(p['candidate_task_or_boundary_error'] and not p['control_task_or_boundary_error'] for p in group),
            'same_backend_pairs': len(subset), 'same_backend_machine_verdicts': dict(Counter(p['verdict'] for p in subset)),
            'user_targeted_pairs': len(human), 'user_candidate_verdicts': dict(Counter(p['user_candidate_verdict'] for p in human))}
    version = 'cr1-user-adjudication-v2'
    out = {'version': version, 'engineering_complete': True, 'real_outputs': len(results), 'user_review_received': len(resolved),
           'user_review_complete': True, 'draft_policy': decision['choice'], 'adjudicated_boundary_outputs': len(changes),
           'naturalness_scores_unchanged': True, 'comparisons': summaries,
           'user_subset_scene_counts': dict(Counter(r['scene'] for r in resolved)),
           'user_subset_role_counts': dict(Counter(r['role'] for r in resolved)),
           'review_scope': 'targeted twelve pairs, not a random sample or twelve independent scenes; no user reasons or numeric scores supplied',
           'independent_multi_human_blind_review': False, 'real_calls_added': 0,
           'analysis_complete': True, 'stage_completion_record': '../../closure.json'}
    out['arm_semantics_user_policy'] = {}
    for condition in ('B0', 'B1', 'B2'):
        values = [r for r in revised if r['position_id'].split('/')[1] == condition]
        out['arm_semantics_user_policy'][condition] = {
            'outputs': len(values),
            'required_content_all_satisfied': sum(all(x['status'] == 'satisfied' for x in r['obligations']) for r in values),
            'boundary_error_outputs': sum(any(x['status'] == 'violated' for x in r['boundaries']) for r in values),
            'score_means_unchanged': {name: sum(r['dimensions'][name]['score'] for r in values) / len(values)
                                     for name in protocol['dimensions']}}
    atomic_json(folder / 'machine-assessments-user-policy-v2.json', revised)
    atomic_json(folder / 'comparisons-user-policy-v2.json', pairs)
    atomic_json(folder / 'policy-adjudication-v2.json', {'version': version, 'user_decision': decision, 'changes': changes,
        'scores_unchanged': True, 'original_strict_files_preserved': True, 'mapping_unchanged': True})
    atomic_json(folder / 'user-review-resolved.json', resolved)
    atomic_json(folder / 'final-analysis.json', out)
    atomic_json(folder / 'summary.json', {**out, 'current_view': 'user_policy_v2',
                                        'original_strict_summary': 'summary-strict-v1.json'})
    (folder / 'README.md').write_text(
        '# CR1分析证据入口\n\n'
        '当前结论：final-analysis.json与summary.json；阶段完成状态以../../closure.json为准。\n\n'
        '当前用户口径：machine-assessments-user-policy-v2.json、comparisons-user-policy-v2.json及policy-adjudication-v2.json。'
        '用户原始选择在user-review.json，揭示后的映射在user-review-resolved.json及CR1-匿名审核映射与结果.md。\n\n'
        'machine-assessments.json、comparisons.json与evidence-metrics.json记录首次严格机器阅读/统计；对应*-strict-v1.json完整保留。'
        '它们不是用户裁定后的边界结论。三维自然度分数从未改动。\n\n'
        '封存后只运行只读closure核查，不重跑会写旧包的report、人工评分编译器或旧统计脚本；不重新抽取已审核12组。\n',
        encoding='utf-8')
    lines = ['# CR1 匿名审核映射与结果', '', '用户已完成12组审核，现揭示冻结的左右映射。没有为裁定重新抽样或生成。', '',
             '| 编号 | 场景/人物 | A | B | 用户选择 | 换算后所选条件 |', '|---|---|---|---|---|---|']
    for row in resolved:
        lines.append(f'| {row["number"]:02d} | {row["scene"]}/{row["role"]} | {row["A"].split("/")[1]} | {row["B"].split("/")[1]} | {row["user_choice"]} | {row["chosen_condition"] or row["user_choice"]} |')
    lines += ['', 'B0：旧有效表达输入；B1：加人工情境；B2：再加人工规格。', '',
              f'该子集是事前规则选出的关键对照，覆盖{len(out["user_subset_scene_counts"])}个场景，人物分布为{out["user_subset_role_counts"]}。用户没有给出逐条理由或1–5分，不推测其偏好原因，也不据此估计全量效果。']
    (folder / 'CR1-匿名审核映射与结果.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return out


def verify_closure(root, *, worktree=False):
    """Read-only closure verification; does not rewrite reports or old evidence."""
    root = real.base.confined(root)
    stage = root.parent.resolve()
    closure = real.read(stage / 'closure.json')
    require(closure['stage'] == 'CR1' and closure['stage_complete'] and closure['completion_audit_passed'],
            'cr1_stage_not_closed')
    for relative, expected in closure['evidence_hashes'].items():
        path = (stage / relative).resolve()
        require(path.is_relative_to(stage) and path.is_file() and real.sha(path) == expected, 'cr1_sealed_evidence_changed')
    archive = (stage / closure['source_archive']).resolve()
    require(archive.is_relative_to(stage), 'cr1_source_archive_outside_stage')
    with zipfile.ZipFile(archive) as source:
        require(set(source.namelist()) == set(closure['source_hashes']), 'cr1_source_archive_inventory')
        for name, expected in closure['source_hashes'].items():
            require(hashlib.sha256(source.read(name)).hexdigest() == expected, 'cr1_archived_source_changed')
            if worktree:
                path = (ROOT / name).resolve()
                require(path.is_relative_to(ROOT) and path.is_file() and real.sha(path) == expected, 'cr1_worktree_differs_from_closure')
    return {'stage': 'CR1', 'passed': True, 'evidence_files': len(closure['evidence_hashes']),
            'archived_source_files': len(closure['source_hashes']), 'worktree_checked': worktree, 'writes': 0, 'generation_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=real.OUT / 'real-v1')
    parser.add_argument('--verify-closure', action='store_true')
    parser.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    with offline():
        if args.verify_closure: print(verify_closure(args.root, worktree=args.worktree))
        else:
            require(not args.worktree, 'cr1_worktree_flag_requires_closure_verification')
            print(adjudicate(args.root))


if __name__ == '__main__': main()
