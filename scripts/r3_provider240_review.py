"""Quote-checked human-readable machine analysis; never calls a provider."""
from scripts.r3_provider240 import OUT,OLD,read,write_once,sha


def record(pair,verdict,input_quote,control_quote,experiment_quote,reason,reviews):
    root=OUT/'real';fixture=read(root/'fixture.json')
    case_id,role=pair.split('-');case=next(c for c in fixture['cases'] if c['id']==case_id)
    assert input_quote in '\n'.join(case['members'])
    examples={r['id']:r for r in fixture['characters'][role]['examples']}
    labels=read(root/'labels.json')['rows']
    value={'pair':pair,'review_type':'machine_development_analysis_not_independent_human_blind_review',
           'status':'reading_complete','verdict':verdict,'input_quote':input_quote,
           'control_quote':control_quote,'experiment_quote':experiment_quote,'reason':reason,
           'reply_reviews':{},'sample_evidence':[],'cohort':'r3-real-provider240-v1'}
    assert verdict in ('win','tie','loss','unknown')
    for arm,quote in (('control',control_quote),('experiment',experiment_quote)):
        key=pair+'-'+arm;result=read(root/'results'/(key+'.json'))
        assert result['real'] and result['turn']['status']=='completed'
        assert quote and quote in result['turn']['reply'],(key,quote)
        manifest=next(m for m in result['manifests'] if m['task_type']=='dialogue')
        material=manifest['prompt_assembly']['material_selection']
        boundary,category,note=reviews[arm]
        assert category in ('natural_transfer','fitting_reuse','forced_template','fact_transplant','no_example')
        value['reply_reviews'][arm]={'key':key,'boundary_pass':boundary,'usage':category,'note':note,
            'reply_quote':quote,'result_sha256':sha(root/'results'/(key+'.json')),
            'final_input_hash':manifest['input_snapshot_hash'],'actual_material':material}
        for ident in material['included_example_ids']:
            sample=examples[ident]
            tag=next(r for r in labels if r['case_id']==case_id and r['role_id']==role and r['material_id']==ident)
            value['sample_evidence'].append({'arm':arm,'material_id':ident,'quote':sample['text'],
                'body_hash':tag['body_hash'],'allowed':tag['allowed'],'basis':tag.get('reason')})
    write_once(OUT/'reading-drafts'/(pair+'.json'),value)
    return value


def build():
    """Combine already-reviewed evidence; no quality score triggers generation."""
    assert not (OUT/'closure.json').exists(), 'sealed_package_read_only'
    from collections import Counter
    from scripts.r3_provider240 import Ledger
    from scripts.provider_usage_summary import usage_summary
    root=OUT/'real';metrics=read(root/'analysis/metrics.json')
    differences=read(root/'analysis/request-differences.json')
    old=read(OLD/'real/analysis/machine-review.json')
    readings={r['pair']:dict(r,cohort='r3-real-v1') for r in old['pairs'] if r.get('status')=='reading_complete'}
    for p in (OUT/'reading-drafts').glob('*.json'):
        value=read(p)
        assert value['pair'] not in readings
        readings[value['pair']]=value
    amendments=[]
    for p in (OUT/'reading-amendments').glob('*.json'):
        change=read(p);pair=change['pair']
        assert sha(OUT/'reading-drafts'/(pair+'.json'))==change['original_draft_sha256']
        assert readings[pair]['cohort']=='r3-real-provider240-v1'  # old scores stay frozen
        readings[pair]['reason']=change['reason']
        for arm,fields in change['reply_review_updates'].items():readings[pair]['reply_reviews'][arm].update(fields)
        amendments.append(change)
    complete={r['pair'] for r in differences if r['complete']}
    assert set(readings)<=complete
    reviewed=[r for r in differences if r['pair'] in readings]
    counts=lambda ds:dict(Counter(readings[d['pair']]['verdict'] for d in ds))
    eligible=[d for d in reviewed if d['eligible_for_directional_analysis']]
    regressions=[]
    for d in reviewed:
        r=readings[d['pair']]['reply_reviews']
        if r['control']['boundary_pass'] and not r['experiment']['boundary_pass']:
            regressions.append({'pair':d['pair'],'cohort':readings[d['pair']]['cohort'],
                'identical_request':d.get('main_request_equal'),'causal_attribution_allowed':d['eligible_for_directional_analysis']})
    rows=Ledger(root/'receipts').rows();old_rows=Ledger(OLD/'real/receipts').rows()
    usage={'old':usage_summary(old_rows),'new':usage_summary(rows),'combined':usage_summary(old_rows+rows),
           'supersedes':'初始报告继承的 reasoning_is_subset 固定假设；原始用量及total_tokens未改。',
           'note':'新网关已观察到total=prompt+completion+reasoning；旧网关可采用不同口径。只累加各自报告的total，不追加推理数。'}
    # Finalize the new package's provisional summaries, never its receipts or
    # any old sealed report. Keep the original summary hash for this correction.
    from persona_runtime.config import atomic_json
    prior_final=root/'analysis/final-report.json'
    if prior_final.exists() and not (root/'analysis-revisions/initial-final-report.json').exists():
        write_once(root/'analysis-revisions/initial-final-report.json',read(prior_final))
    corrections=[]
    for directory,cohort in (('analysis','combined'),('analysis-new','new')):
        path=root/directory/'metrics.json'
        if not path.exists():continue
        original=read(path);corrections.append({'path':str(path),'before_sha256':sha(path)})
        original['reasoning_is_subset']=None
        original['usage_accounting']=usage[cohort]
        original['usage_report_revision']='gateway-arithmetic-v2'
        original['usage_note']='各网关回执口径不同，不作统一子集假设；total_tokens只按原回执累加。'
        atomic_json(path,original)
    if not (root/'analysis/usage-summary-correction.json').exists():
        write_once(root/'analysis/usage-summary-correction.json',{'receipts_unchanged':True,'numeric_totals_unchanged':True,'corrections':corrections})
    full=metrics['complete'] and set(readings)==complete
    numerical=metrics['selection_direction']['numerical_conditions_met']
    preference=counts(eligible)
    positive=preference.get('win',0)>preference.get('loss',0) and preference.get('win',0)>0
    supported=full and numerical and not regressions and positive
    verdict=('支持继续保留v4实验方向' if supported else
             '真实验证未完成' if not full else
             '未满足保留v4的全部条件；按配对及供应商分别解释，不宣称自然度已改善')
    result={'status':'completed_authorized_supplement' if full else 'incomplete_authorized_supplement',
        'engineering_complete':full and read(root/'verification.json')['passed'] if (root/'verification.json').exists() else False,
        'old_batch_status':'blocked_unchanged','supplement_requests':metrics['new_complete_requests'],
        'combined_complete_requests':metrics['complete_requests'],'combined_complete_pairs':metrics['complete_pairs'],
        'combined_real_calls':metrics['calls'],'new_real_calls':len(rows),'old_real_calls':len(old_rows),
        'old_successes_replayed':0,'new_retry_calls':sum(r['attempt_index']>0 for r in rows),
        'all_preferences':counts(reviewed),'unknown_pairs':24-len(reviewed),
        'eligible_preferences':preference,'eligible_pairs':len(eligible),
        'identical_main_requests':sum(d.get('main_request_equal',False) for d in reviewed),
        'boundary_regressions':regressions,'selection_direction':metrics['selection_direction'],
        'supports_retaining_v4_experimental_direction':supported,'conclusion':verdict,
        'cohorts':{c:{'preferences':counts([d for d in reviewed if readings[d['pair']]['cohort']==c]),
            'eligible_preferences':counts([d for d in eligible if readings[d['pair']]['cohort']==c])}
            for c in ('r3-real-v1','r3-real-provider240-v1')},
        'usage':usage,'paid_judge_calls':0,'qq':0,'formal_mode':'Shadow','default_selector':'character-selector-v2',
        'native_visual_check':'not_performed','review_type':'machine_development_analysis_not_independent_human_blind_review',
        'next':['R4-A 复述式表达及样例机械套用','R4-B 情境缺口','R4-C 人格动作兑现']}
    result['selection_direction']=dict(result['selection_direction'],hard_boundary_reading='completed_with_regression' if regressions else 'completed_no_observed_regression')
    result['first_attempt_preferences']=counts([d for d in reviewed if d['first_attempt_pair']])
    result['retry_recovered_preferences']=counts([d for d in reviewed if not d['first_attempt_pair']])
    result['reply_usage_categories']=dict(Counter(r['usage'] for v in readings.values() for r in v['reply_reviews'].values()))
    final_metrics=read(root/'analysis/metrics.json')
    final_metrics['real_effect_status']='reviewed_conditions_met' if supported else 'reviewed_conditions_not_met' if full else 'incomplete'
    final_metrics['selection_direction']=result['selection_direction']
    final_metrics['final_assessment']='final-report.json'
    atomic_json(root/'analysis/metrics.json',final_metrics)
    atomic_json(root/'analysis/usage-accounting.json',usage)
    atomic_json(root/'analysis/machine-review.json',{'pairs':[readings[d['pair']] for d in reviewed],
        'old_reading_source':str(OLD/'real/analysis/machine-review.json'),'old_reading_sha256':sha(OLD/'real/analysis/machine-review.json'),
        'quotes_validated':True,'draft_reading_amendments':amendments,'review_type':result['review_type'],'paid_judge_calls':0})
    atomic_json(root/'analysis/final-report.json',result)
    return result
