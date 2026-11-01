"""Pre-call reporting strata over unchanged R3 labels; never model input."""
from copy import deepcopy

# Explicit case/role/body references, authored from the R3 textual reasons.
# Other excluded examples remain in overall inapplicability counts.
PREMISE_CONFLICTS={
 'P01':{'detail':['e2','e3','e4','e5'],'stance':['e2','e3','e5','e6']},
 'P02':{'detail':['e4','e5'],'stance':['e1','e6']},
 'P03':{'detail':['e2','e3','e4','e5'],'stance':['e2','e3','e5','e6']},
 'P04':{'detail':['e4','e5'],'stance':['e1','e6']},
 'P05':{'detail':['e5'],'stance':['e1','e6']},
 'P06':{'detail':['e3','e4'],'stance':['e1','e2','e4','e5','e6']},
 'P07':{'detail':['e3','e4','e5'],'stance':['e2','e6']},
 'P08':{'detail':['e2','e3','e4','e5'],'stance':['e2','e3','e5','e6']},
 'P09':{'detail':['e3','e4','e5'],'stance':['e2','e5']},
 'P10':{'detail':['e2','e3','e4','e5'],'stance':['e1','e2','e3','e5','e6']},
 'P11':{'detail':['e3','e4'],'stance':['e1','e6']},
 'P12':{'detail':['e2','e3','e4','e5'],'stance':['e2','e3','e5']}}

PRESERVATION={
 'process_transfer':{'P01-detail':['e1'],'P01-stance':['e1'],'P02-detail':['e1'],
   'P03-detail':['e1'],'P03-stance':['e1'],'P04-detail':['e1'],'P08-detail':['e1'],
   'P08-stance':['e1'],'P09-detail':['e1'],'P09-stance':['e1'],'P12-detail':['e1'],'P12-stance':['e1']},
 'local_progress':{'P07-detail':['e2'],'P07-stance':['e3'],'P09-detail':['e2'],'P09-stance':['e3']}}

CASE_BOUNDARIES={
 'P01':'路线未通，不补写已经核准的局部或整体成果。',
 'P02':'整段已通且心里亮堂，不反过来说还未完成，不虚构共同找路经历。',
 'P03':'星图仍没连全，承接找错又找的过程，不提前认定成功。',
 'P04':'整串已认全，允许回看过程，不把回看理解成现在仍未完成。',
 'P05':'尊重主动庆祝，不转成未请求的困难复盘。',
 'P06':'尊重拒绝合影/庆功和想安静，不强行高兴或把负担放到以后。',
 'P07':'认可已排好的一盒，不宣布整个相册完成；不把局部认可一概排除。',
 'P08':'这一盒尚未弄好，陪定位卡点，不先庆祝完成。',
 'P09':'末条更正优先：底漆已干、字未描、决定继续、今晚不挂。',
 'P10':'末条意愿优先：新招牌搁置、使用原来的，不沿首条推动补字。',
 'P11':'有效引用的完成状态与当前残留核对冲动同时成立，不强迫开心。',
 'P12':'旧引用已被当前标签洇糊和重做状态更新，不沿旧完成或拒绝明确继续意愿。'}


def evaluation(data,labels):
    rules={(r['case_id'],r['role_id'],r['material_id']):r for r in labels['rows']}
    groups=[]
    for case,roles in PREMISE_CONFLICTS.items():
        for role,ids in roles.items():
            for material in ids:
                row=rules[case,role,material];assert row['allowed'] is False
                groups.append({k:deepcopy(row[k]) for k in ('case_id','role_id','material_id','body_hash','required_premise','reason','sample_quote','input_quotes','reference_quote')})
    for group,pairs in PRESERVATION.items():
        for pair,ids in pairs.items():
            case,role=pair.split('-')
            assert all(rules[case,role,key]['allowed'] for key in ids)
    assert set(CASE_BOUNDARIES)=={c['id'] for c in data['cases']}
    return {'version':'r3-real-evaluation-v1','labels_changed':False,'premise_conflicts':groups,
      'preservation_targets':deepcopy(PRESERVATION),'case_boundaries':deepcopy(CASE_BOUNDARIES),
      'denominators_per_arm':{'requests':24,'applicable_candidates':22,'candidate_missing_only_applicable':1,'no_applicable_material':1},
      'preservation_rule':'Compare any applicable included example in each preservation case; list exact target replacements separately. An alternative must be judged from actual context, never force a particular ID.',
      'selection_gate':{'complete_pairs':24,'premise_conflict_requests_reduction_at_least':1,
         'all_inapplicable_requests_must_not_increase':True,'applicable_included_requests_must_not_decrease':True,
         'preservation_any_applicable_pair_losses_allowed':0,'hard_boundary_regressions_allowed':0},
      'reading_categories':['natural_transfer','fitting_reuse','forced_template','fact_transplant','no_example'],
      'reading_rule':'Quote input, selected example and reply; lexical overlap is not a failure by itself. Judge concrete reaction, extra interpretation, rigid reuse and invented facts. No length, punctuation or banned-word proxies.',
      'quality_gate':'Only complete, correctly versioned pairs with examples-only main differences and known equal selector/main response model and backend may support directional analysis. Wins must exceed losses with quoted positive evidence; equal fingerprints do not imply determinism.',
      'review_type':'machine_development_analysis_not_independent_human_blind_review','paid_judge_calls':0}
