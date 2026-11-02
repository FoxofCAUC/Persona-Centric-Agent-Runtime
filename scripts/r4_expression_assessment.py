"""Aggregate quoted machine reading; no model judge and no text-shape scoring."""
from collections import Counter
from scripts.r4_expression_realization import OUT,read,atomic_json,StopRun

ISSUES={'redundant_restatement','strategy_narration','forced_example'}


def validate_reading(review,prepared,values,fixture):
    items={i['key']:i for i in prepared['items']};rows=review['requests']
    if len(rows)!=len(items) or {r['key'] for r in rows}!=set(items):raise StopRun('reading_requests_incomplete_or_duplicated')
    if review.get('review_type')!='machine_development_analysis':raise StopRun('review_provenance_missing')
    for row in rows:
        item=items[row['key']];body=values[row['key']]['turn']['reply'] or ''
        if not row['reason'] or not row['reply_quotes'] or any(q not in body or not q for q in row['reply_quotes']):raise StopRun('reading_quote_not_in_reply:'+row['key'])
        if not set(row['issues'])<=ISSUES:raise StopRun('unknown_reading_issue')
        if any(type(row[k]) is not bool for k in ('specific_engagement','boundary_pass','task_pass')):raise StopRun('reading_boolean_required')
        if row['sample_usage'] not in {'natural_transfer','fitting_reuse','forced_template','fact_transplant','no_example'}:raise StopRun('unknown_sample_usage')
        case=next(c for c in fixture['cases'] if c['id']==item['case'])
        source='\n'.join(case['members'])+'\n'+str(case['reference'])
        if not row['input_quote'] or row['input_quote'] not in source:raise StopRun('input_quote_missing')
        included=[e for e in fixture['characters'][item['role']]['examples'] if e['id'] in item['selected_examples']]
        if included:
            if not row['sample_quote'] or not any(row['sample_quote'] in e['text'] for e in included):raise StopRun('sample_quote_missing')
        elif row['sample_quote'] is not None:raise StopRun('invented_sample_evidence')
    pairs=review['pairs'];expected={i['pair'] for i in prepared['items']}
    if len(pairs)!=len(expected) or {p['pair'] for p in pairs}!=expected:raise StopRun('pair_reading_incomplete')
    if any(p['verdict'] not in ('win','tie','loss','unknown') or not p['reason'] for p in pairs):raise StopRun('pair_reading_invalid')


def calculate(review,prepared,metrics,differences,rubric):
    rows={r['key']:r for r in review['requests']};items=prepared['items'];primary=set(rubric['primary_cases']);stats={}
    for arm in ('control','experiment'):
        all_rows=[rows[i['key']] for i in items if i['arm']==arm]
        main=[rows[i['key']] for i in items if i['arm']==arm and i['case'] in primary]
        stats[arm]={'primary_requests':len(main),'issue_requests':sum(bool(r['issues']) for r in main),
            'issue_types':dict(Counter(issue for r in main for issue in r['issues'])),
            'specific_engagement':sum(r['specific_engagement'] for r in main),
            'hard_boundary_failures':sum(not r['boundary_pass'] for r in all_rows),
            'task_control_failures':sum(not rows[i['key']]['task_pass'] for i in items if i['arm']==arm and i['case'] not in primary),
            'sample_usage':dict(Counter(r['sample_usage'] for r in main))}
    eligible={d['pair'] for d in differences if d['primary'] and d['eligible_for_directional_analysis']}
    verdicts=Counter(p['verdict'] for p in review['pairs']);causal=Counter(p['verdict'] for p in review['pairs'] if p['pair'] in eligible)
    regressions=[p['pair'] for p in review['pairs'] if rows[p['pair']+'-control']['boundary_pass'] and not rows[p['pair']+'-experiment']['boundary_pass']]
    checks={'real_not_simulated':metrics['real'],'all_requests_complete':metrics['complete_requests']==48,
        'issues_reduced_at_least_two':stats['control']['issue_requests']-stats['experiment']['issue_requests']>=2,
        'specific_engagement_not_decreased':stats['experiment']['specific_engagement']>=stats['control']['specific_engagement'],
        'no_hard_boundary_regression':not regressions and stats['experiment']['hard_boundary_failures']<=stats['control']['hard_boundary_failures'],
        'all_task_controls_pass':all(stats[a]['task_control_failures']==0 for a in stats),
        'eligible_primary_pairs_at_least_four':len(eligible)>=4,'eligible_primary_wins_more_than_losses':causal['win']>causal['loss']}
    passed=all(checks.values())
    return {'engineering_complete':metrics['complete_requests']==48 and metrics['all_complete_differences_explained'],
        'effect_acceptance_passed':passed,'checks':checks,'arm_statistics':stats,'all_pair_preferences':dict(verdicts),
        'eligible_primary_preferences':dict(causal),'eligible_primary_pairs':sorted(eligible),'boundary_regressions':regressions,
        'conclusion':'本批R4-A开发验收通过' if passed else '本批未达到R4-A效果验收条件',
        'formal_release':False,'independent_human_blind_review':False,'paid_judge_calls':0,
        'generalization':'新人工材料上的机器开发分析；不替代冻结质量门，不证明所有自然度问题已解决。',
        'R4_B':'未完成','R4_C':'未完成'}


def assess(root):
    prepared=read(root/'prepared.json');data=read(root/'fixture.json');rules=read(root/'rubric.json')
    review=read(root/'analysis/machine-reading.json');metrics=read(root/'analysis/metrics.json');diff=read(root/'analysis/request-differences.json')
    amendment=read(OUT/'evaluation-policy-amendment.json')
    if review.get('policy_version')!=amendment['version']:raise StopRun('current_user_reading_policy_required')
    values={p.stem:read(p) for p in (root/'results').glob('*.json')}
    validate_reading(review,prepared,values,data)
    result=calculate(review,prepared,metrics,diff,rules)
    has_lively_voice=any(i['voice']['version'] in ('natural-dialogue-v4','natural-dialogue-v5','natural-dialogue-v6','natural-dialogue-v7') for i in prepared['items'])
    result.update(policy_version=amendment['version'],policy_authority='用户2026-11-01追加并要求长期沿用的原则',
        frozen_request_boundary_note=('本批新版表达已纳入活泼性优先原则；两臂阅读同用新口径，已受理请求不改写。' if has_lively_voice else
            '本批提示仍为事前冻结版本；新原则用于验收阅读，未改写已受理请求。'),
        traceability_only_fill_not_failure=True)
    atomic_json(root/'analysis/final-assessment.json',result);return result
