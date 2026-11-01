"""Describe reported usage without assuming gateways count reasoning alike."""
from collections import Counter


def usage_summary(rows):
    sums=Counter();accounting=Counter();unknown=0
    for row in rows:
        usage=row.get('usage')
        if not usage:
            unknown+=1
            continue
        for name in ('prompt_tokens','completion_tokens','total_tokens'):
            if type(usage.get(name)) is int:sums[name]+=usage[name]
        reasoning=usage.get('completion_tokens_details',{}).get('reasoning_tokens')
        if type(reasoning) is int:sums['reasoning_tokens']+=reasoning
        parts=[usage.get(name) for name in ('prompt_tokens','completion_tokens','total_tokens')]
        if any(type(v) is not int for v in parts) or type(reasoning) is not int:
            accounting['unknown']+=1
        elif reasoning==0:
            accounting['zero_reasoning_not_distinguishable']+=1
        elif parts[2]==parts[0]+parts[1]:
            accounting['reported_total_includes_reasoning_in_completion']+=1
        elif parts[2]==parts[0]+parts[1]+reasoning:
            accounting['reported_total_adds_reasoning_separately']+=1
        else:
            accounting['other_or_inconsistent']+=1
    return {'known_usage':dict(sums),'unknown_usage_calls':unknown,
            'reported_arithmetic':dict(accounting),'total_policy':'sum_reported_total_tokens_only',
            'reasoning_never_added_twice':True,'amount':'未知，以各网关账单为准'}
