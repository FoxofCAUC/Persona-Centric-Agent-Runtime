"""Offline evaluation contracts; no model or runtime state changes."""


def ranking_metrics(ids, required):
    required = set(required)
    if not required:
        return {'recall5': None, 'mrr5': None}
    ids = list(dict.fromkeys(ids))[:5]
    return {'recall5': len(set(ids) & required) / len(required),
            'mrr5': next((1 / (i + 1) for i, item in enumerate(ids) if item in required), 0)}


def selection_check(case, selected, *, degraded=False, contract_ok=True):
    actual = set(selected)
    required, allowed, forbidden = (set(case[k]) for k in
                                    ('required_ids', 'allowed_ids', 'forbidden_ids'))
    if not required <= allowed or allowed & forbidden:
        raise ValueError('invalid_evaluation_labels')
    unexpected = actual - allowed
    missing = required - actual
    return {'passed': not (unexpected or missing or degraded) and contract_ok,
            'unexpected_ids': sorted(unexpected), 'missing_ids': sorted(missing),
            'forbidden_ids': sorted(actual & forbidden), 'degraded': degraded,
            'hard_violation': bool(actual & forbidden) or
                              bool(case.get('hard_negative') and actual) or not contract_ok}


def choose_threshold(trials):
    feasible = [r for r in trials if r['violations'] == 0]
    return max(feasible, key=lambda r: (r['hits'], r['margin'], r['threshold'])) if feasible else None


def rejection_margins(rows, threshold, allowed, rule_excluded):
    """Only threshold-governed rejections contribute, not budget or ambiguity."""
    return [threshold - row['score'] for row in rows
            if row['item_id'] not in allowed and row['item_id'] not in rule_excluded]


def quality_gate(rows):
    positives = [r for r in rows if r['group'] == 'positive']
    negatives = [r for r in rows if r['group'] == 'negative']
    contracts = [r for r in rows if r['group'] == 'contract']
    return (len(positives) == 20 and len(negatives) == 20 and len(contracts) == 8
            and sum(r['filtered']['recall5'] for r in positives) / 20 >= .85
            and sum(r['filtered']['recall5'] - r['lexical_candidates']['recall5'] for r in positives) / 20 >= .1
            and sum(r['passed'] for r in positives) >= 17
            and sum(bool(r['selected']) for r in negatives) <= 1
            and not any(r['hard_violation'] or r['degraded'] for r in rows)
            and all(r['passed'] for r in contracts))
