"""One explicit companion consumer, applied after the existing material budgets."""
from .archive import fingerprint

VERSION = 'companion-context-rules-off-v1'
FIELD = 'social.context_rules'
REASON = 'context_rules_consumer_excluded'


def consume(parts):
    kept, excluded = [], []
    for part in parts:
        if part['kind'] in ('anchor', 'fact') and part['id'] == FIELD:
            excluded.append({'kind': part['kind'], 'id': part['id'], 'reason': REASON,
                             'characters': len(part['text']), 'content_hash': fingerprint(part['text'])})
        else:
            kept.append(part)
    return kept, {'version': VERSION, 'field_id': FIELD,
                  'status': 'excluded' if excluded else 'no_consumption_difference',
                  'budget_retained_parts': [{'kind': p['kind'], 'id': p['id']} for p in parts],
                  'included_parts': [{'kind': p['kind'], 'id': p['id']} for p in kept],
                  'excluded_parts': excluded}
