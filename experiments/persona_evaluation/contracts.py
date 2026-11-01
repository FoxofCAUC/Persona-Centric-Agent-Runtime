"""Pure readiness, finite-label ranking and frozen metric denominators."""
import math

from persona_runtime.disposition_core.contracts import CATEGORIES

RANKING_VERSION = 'situation-ranking-v1'
METRIC_VERSION = 'm763-metrics-v1'
PRIMARY_COVERAGE = {
    'relationship': ('neutral', 'close'),
    'information_gap': (False, True),
}


def ranked_situations(frame, members, policy):
    """Rank observed categories, not action scores or psychological confidence."""
    order = {member.id: i for i, member in enumerate(members)}
    explicit_categories = {'support': 'support_request', 'acknowledge': 'acknowledgement',
                           'explain': 'information_request'}
    explicit = {rule: explicit_categories[action] for rule, action, _ in policy.explicit
                if action in explicit_categories}
    ranked = []
    for category in frame.categories:
        evidence = []
        for item in frame.evidence:
            if explicit.get(item.rule_id) == category:
                evidence.append((0, order[item.member_id], item.start, item.end, item.member_id))
            elif item.rule_id == 'category_' + category:
                evidence.append((1, order[item.member_id], item.start, item.end, item.member_id))
        tier, index, start, end, member = min(evidence) if evidence else (2, len(members), 0, 0, None)
        ranked.append((tier, index, start, CATEGORIES.index(category), {
            'category': category, 'origin': 'explicit' if tier == 0 else 'rule' if tier == 1 else 'context_or_unknown',
            'member_id': member, 'start': start, 'end': end}))
    ranked.sort(key=lambda r: r[:4])
    return {'version': RANKING_VERSION, 'frame_origin': frame.frame_origin,
            'candidates': [row[-1] for row in ranked[:2]], 'confidence': None}


def readiness(capabilities, required=PRIMARY_COVERAGE):
    """Missing requirements remain visible; fixture booleans are not adapters."""
    gaps = []
    for field, needed in required.items():
        supplied = capabilities.get(field, ())
        missing = [v for v in needed if not any(type(v) is type(x) and v == x for x in supplied)]
        if missing:
            gaps.append({'field': field, 'required_values': list(needed),
                         'observed_values': list(supplied), 'unavailable_values': missing})
    return {'ready': not gaps, 'gaps': gaps, 'allow_paid_generation': not gaps}


def wilson_lower(wins, n=100):
    if type(wins) is not int or type(n) is not int or n != 100 or not 0 <= wins <= n:
        raise ValueError('invalid_review_denominator')
    z = 1.96
    p = wins / n
    return (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)


def metric_gate(name, value):
    """Counts use the adopted fixed denominators, never a surviving subset."""
    bounds = {'situation_top1': (0, 40), 'situation_top2': (0, 40),
              'implicit_gain': (-40, 40), 'reasonable_pairs': (0, 20),
              'unnecessary_cases': (0, 20), 'action_implementation': (0, 100),
              'natural_wins': (0, 100)}
    if name not in bounds:
        raise ValueError('unknown_metric')
    low, high = bounds[name]
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_metric_count')
    if name == 'situation_top1': return value >= 34
    if name == 'situation_top2': return value >= 38
    if name == 'implicit_gain': return value >= 6
    if name == 'reasonable_pairs': return value >= 16
    if name == 'unnecessary_cases': return value <= 2
    if name == 'action_implementation': return value >= 95
    if name == 'natural_wins': return value >= 60 and wilson_lower(value) > .5
    raise ValueError('unknown_metric')
