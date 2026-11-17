"""Traceable privacy views and conversation partitions, without language rewriting."""
from collections import Counter, defaultdict
import hashlib
import heapq
import hmac
import re
import unicodedata

from .corpus_files import CorpusError, digest
from .corpus_store import fingerprint


VIEW_VERSION = 'corpus-private-view-v1'
DEFAULTS = {'version': VIEW_VERSION, 'episode_gap_seconds': 1800,
            'distinctive_duplicate_chars': 40, 'near_duplicate_chars': 64,
            'shingle_chars': 5, 'sketch_size': 64, 'max_fingerprint_postings': 64,
            'min_shared_fingerprints': 4, 'near_jaccard': .9, 'max_duplicate_pairs': 250000,
            'learn_percent': 70, 'dev_percent': 15, 'excluded_message_ids': []}
TOKEN = re.compile(r'\[(?:URL|EMAIL|PHONE|ACCOUNT|PERSON|IDENTIFIER)_[0-9a-f]{12}\]')
PATTERNS = (
    ('url', re.compile(r'''https?://[^\s<>"'`，。！？；、【】（）]+''', re.I)),
    ('email', re.compile(r'(?<![\w@])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}')),
    ('phone', re.compile(r'(?<![A-Za-z0-9])(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)')),
    ('account', re.compile(r'(?<![A-Za-z0-9_])wxid_[A-Za-z0-9_-]+')),
    ('identifier', re.compile(r'(?<![A-Za-z0-9])\d{15,19}[Xx]?(?![A-Za-z0-9])')),
)


def recipe_checked(changes=None):
    if changes is not None and (not isinstance(changes, dict) or set(changes) - set(DEFAULTS)):
        raise CorpusError('corpus_view_recipe_invalid')
    recipe = DEFAULTS | (changes or {})
    if not isinstance(recipe['excluded_message_ids'], list):
        raise CorpusError('corpus_view_recipe_invalid')
    recipe['excluded_message_ids'] = list(recipe['excluded_message_ids'])
    for name, value in recipe.items():
        if name in ('version', 'excluded_message_ids', 'near_jaccard'):
            continue
        if type(value) is not int or not 0 < value <= 1000000:
            raise CorpusError('corpus_view_recipe_invalid')
    if (recipe['version'] != VIEW_VERSION or type(recipe['near_jaccard']) not in (int, float)
            or not .8 <= recipe['near_jaccard'] <= 1 or recipe['learn_percent'] + recipe['dev_percent'] >= 100
            or any(not isinstance(v, str) for v in recipe['excluded_message_ids'])
            or len(set(recipe['excluded_message_ids'])) != len(recipe['excluded_message_ids'])):
        raise CorpusError('corpus_view_recipe_invalid')
    return recipe


def redact_text(text, *, key, namespace, identities):
    if not isinstance(key, bytes) or len(key) != 32 or not isinstance(namespace, str) or not namespace:
        raise CorpusError('corpus_view_identity_scope_invalid')
    matches = []
    flags = set()
    for priority, (kind, pattern) in enumerate(PATTERNS):
        for match in pattern.finditer(text):
            end = match.end()
            if kind == 'url':
                while end > match.start() and text[end - 1] in '.,;:!?)]}':
                    end -= 1
            if end > match.start():
                matches.append((priority, match.start(), end, kind))
    for item in identities.values():
        for value, kind in [(item['source_id'], 'account'), *((name, 'person') for name in item['names'])]:
            if not value:
                continue
            start = 0
            while True:
                pos = text.find(value, start)
                if pos < 0:
                    break
                start = pos + len(value)
                if kind == 'person' and len(value) == 1:
                    left = text[pos - 1] if pos else ''
                    right = text[start] if start < len(text) else ''
                    if not (left and (left.isalnum() or left == '_')) and not (right and (right.isalnum() or right == '_')):
                        flags.add('short_name_match_pending')
                else:
                    matches.append((6 if kind == 'person' else 3, pos, start, kind))
    selected = []
    for priority, start, end, kind in sorted(matches, key=lambda m: (m[0], -(m[2] - m[1]), m[1])):
        if not any(start < old[1] and end > old[0] for old in selected):
            selected.append((start, end, kind))
    result, mapping, source_cursor, view_cursor = [], [], 0, 0
    for start, end, kind in sorted(selected):
        original = text[start:end]
        token_hash = hmac.new(key, (namespace + '\0' + kind + '\0' + original).encode('utf-8'), hashlib.sha256).hexdigest()[:12]
        replacement = '[' + kind.upper() + '_' + token_hash + ']'
        prefix = text[source_cursor:start]
        result.extend((prefix, replacement))
        view_cursor += len(prefix)
        mapping.append({'source_start': start, 'source_end': end, 'view_start': view_cursor,
                        'view_end': view_cursor + len(replacement), 'kind': kind,
                        'replacement': replacement, 'original_hash': digest(original.encode('utf-8'))})
        source_cursor = end
        view_cursor += len(replacement)
        if kind == 'identifier':
            flags.add('identifier_pattern_unconfirmed')
    result.append(text[source_cursor:])
    redacted = ''.join(result)
    if mapping and not any(c.isalnum() for c in TOKEN.sub('', redacted)):
        flags.add('redaction_only')
    return redacted, mapping, sorted(flags)


def verify_redactions(original, text, mapping):
    source_cursor, view_cursor = 0, 0
    for item in mapping:
        start, end = item['source_start'], item['source_end']
        a, b = item['view_start'], item['view_end']
        if not (source_cursor <= start < end <= len(original) and view_cursor <= a < b <= len(text)):
            raise CorpusError('corpus_redaction_span_invalid')
        if (original[source_cursor:start] != text[view_cursor:a]
                or text[a:b] != item['replacement']
                or digest(original[start:end].encode('utf-8')) != item['original_hash']):
            raise CorpusError('corpus_redaction_evidence_changed')
        source_cursor, view_cursor = end, b
    if original[source_cursor:] != text[view_cursor:]:
        raise CorpusError('corpus_redaction_unmapped_change')


def overlap_text(text):
    # Normalization is only an index for duplicate detection; never emit it as speech.
    text = TOKEN.sub(lambda m: '[' + m.group()[1:].split('_', 1)[0] + ']', text)
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


class Groups:
    def __init__(self, size):
        self.parents = list(range(size))

    def find(self, value):
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def join(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parents[max(a, b)] = min(a, b)


def near_pairs(items, recipe):
    """Bottom-k blocking followed by exact shingle Jaccard; no embedding model."""
    tokens, postings = [], defaultdict(list)
    width = recipe['shingle_chars']
    for index, (_, text, _) in enumerate(items):
        grams = {text[i:i + width] for i in range(max(0, len(text) - width + 1))}
        tokens.append(grams)
        sketch = heapq.nsmallest(recipe['sketch_size'],
                                {hashlib.blake2b(g.encode('utf-8'), digest_size=8).digest() for g in grams})
        for token in sketch:
            postings[token].append(index)
    candidates = Counter()
    for values in postings.values():
        if len(values) > recipe['max_fingerprint_postings']:
            continue
        for n, left in enumerate(values):
            for right in values[n + 1:]:
                if items[left][0] == items[right][0]:
                    continue
                candidates[left, right] += 1
                if len(candidates) > recipe['max_duplicate_pairs']:
                    raise CorpusError('corpus_duplicate_candidate_capacity')
    found = []
    for (left, right), shared in sorted(candidates.items()):
        if shared < recipe['min_shared_fingerprints']:
            continue
        a, b = tokens[left], tokens[right]
        if not a or not b or min(len(a), len(b)) / max(len(a), len(b)) < recipe['near_jaccard']:
            continue
        score = len(a & b) / len(a | b)
        if score >= recipe['near_jaccard']:
            found.append((left, right, round(score, 6)))
    return found


def build_dataset(rows, relations, identities, *, key, namespace, parse_id, changes=None):
    recipe = recipe_checked(changes)
    if not rows or [r['ordinal'] for r in rows] != list(range(len(rows))):
        raise CorpusError('corpus_source_order_invalid')
    if set(recipe['excluded_message_ids']) - {r['id'] for r in rows}:
        raise CorpusError('corpus_view_excluded_source_missing')
    excluded = set(recipe['excluded_message_ids'])
    missing_refs = {r['source_id'] for r in relations if r['kind'] == 'reply' and r['state'] == 'unresolved'}
    views = []
    for source in rows:
        flags = set()
        candidate = bool(source['target_candidate'])
        if candidate and source['id'] not in excluded:
            text, mapping, privacy_flags = redact_text(source['text'], key=key, namespace=namespace, identities=identities)
            flags.update(privacy_flags)
        else:
            text, mapping = '', []
            flags.add('source_excluded' if source['id'] in excluded else 'nontext_context')
        if source['id'] in missing_refs:
            flags.add('unresolved_reference')
        target = candidate and not flags.intersection({'source_excluded','short_name_match_pending',
                   'identifier_pattern_unconfirmed','redaction_only','unresolved_reference'})
        views.append({'message_id': source['id'], 'ordinal': source['ordinal'],
                      'role': {'self': 'A', 'peer': 'B', 'system': 'SYS'}[source['role']],
                      'kind': source['kind'], 'text': text, 'source_text_hash': source['text_hash'],
                      'text_hash': digest(text.encode('utf-8')), 'redactions': mapping,
                      'flags': sorted(flags), 'source_candidate': candidate, 'target_candidate': target})
    fragments, episode = [], []
    previous = None
    for source, view in zip(rows, views):
        if previous is not None and source['created_epoch'] - previous > recipe['episode_gap_seconds']:
            fragments.append(episode)
            episode = []
        episode.append(view['ordinal'])
        previous = source['created_epoch']
    if episode:
        fragments.append(episode)
    groups, edges, by_message = Groups(len(fragments)), [], {}
    fragment_rows = []
    normalized = []
    for n, members in enumerate(fragments):
        ident = fingerprint([parse_id, 'time-fragment-v1', views[members[0]]['message_id'], views[members[-1]]['message_id']])
        for index in members:
            by_message[views[index]['message_id']] = n
            views[index]['fragment_id'] = ident
        shape = '\n'.join(v['role'] + ':' + (overlap_text(v['text']) if v['text'] else '<' + v['kind'] + '>') for v in (views[i] for i in members))
        normalized.append(shape)
        fragment_rows.append({'id': ident, 'ordinal': n, 'first_epoch': rows[members[0]]['created_epoch'],
                              'last_epoch': rows[members[-1]]['created_epoch'],
                              'message_ids': [views[i]['message_id'] for i in members],
                              'target_ids': [views[i]['message_id'] for i in members if views[i]['target_candidate']],
                              'flags': sorted({flag for i in members for flag in views[i]['flags']}),
                              'canonical_hash': digest(shape.encode('utf-8'))})

    def connect(left, right, reason, evidence, score=1.0):
        if left == right:
            return
        groups.join(left, right)
        edges.append({'left': fragment_rows[left]['id'], 'right': fragment_rows[right]['id'],
                      'reason': reason, 'evidence': evidence, 'similarity': score})

    for relation in relations:
        if relation['kind'] == 'reply' and relation['state'] == 'resolved':
            if relation['source_id'] not in by_message or relation['target_id'] not in by_message:
                raise CorpusError('corpus_reply_source_missing')
            connect(by_message[relation['source_id']], by_message[relation['target_id']],
                    'reply', [relation['source_id'], relation['target_id']])
    exact, long_text = {}, {}
    for n, fragment in enumerate(fragment_rows):
        if fragment['target_ids']:
            old = exact.setdefault(fragment['canonical_hash'], n)
            connect(old, n, 'fragment_exact', [fragment['canonical_hash']])
    near_fragments = [(n, normalized[n], f['id']) for n, f in enumerate(fragment_rows)
                      if f['target_ids'] and len(normalized[n]) >= recipe['near_duplicate_chars']]
    near_targets = []
    for view in views:
        if not view['target_candidate']:
            continue
        normal = overlap_text(view['text'])
        n = by_message[view['message_id']]
        if len(normal) >= recipe['distinctive_duplicate_chars']:
            hashed = digest(normal.encode('utf-8'))
            old = long_text.setdefault(hashed, n)
            connect(old, n, 'target_exact', [hashed])
        if len(normal) >= recipe['near_duplicate_chars']:
            near_targets.append((n, normal, view['message_id']))
    for items, reason in ((near_fragments, 'near_fragment'), (near_targets, 'near_target')):
        for left, right, score in near_pairs(items, recipe):
            connect(items[left][0], items[right][0], reason, [items[left][2], items[right][2]], score)
    members = defaultdict(list)
    for n in range(len(fragment_rows)):
        members[groups.find(n)].append(n)
    units = []
    for indices in members.values():
        unit = {'id': fingerprint(sorted(fragment_rows[n]['id'] for n in indices)),
                'fragment_ids': [fragment_rows[n]['id'] for n in indices],
                'first_epoch': min(fragment_rows[n]['first_epoch'] for n in indices),
                'source_target_count': sum(len(fragment_rows[n]['target_ids']) for n in indices)}
        units.append((unit, indices))
    units.sort(key=lambda item: (item[0]['first_epoch'], item[0]['id']))
    total = sum(item[0]['source_target_count'] for item in units)
    cumulative = 0
    split_units = []
    for unit, indices in units:
        weight = unit['source_target_count']
        position = 100 * (cumulative + weight / 2) / max(1, total)
        partition = ('excluded' if not weight else 'learn' if position < recipe['learn_percent'] else
                     'dev_review' if position < recipe['learn_percent'] + recipe['dev_percent'] else 'final_holdout')
        cumulative += weight
        unit['split'] = partition
        for n in indices:
            fragment_rows[n].update(group_id=unit['id'], split=partition)
            for index in fragments[n]:
                views[index]['split'] = partition
        split_units.append(unit)
    # Common short utterances remain valid context, but cannot be reused as
    # an independent held-out reference after appearing in an earlier pool.
    pools = defaultdict(set)
    for view in views:
        if view['target_candidate']:
            pools[overlap_text(view['text'])].add(view['split'])
    order = {'learn': 0, 'dev_review': 1, 'final_holdout': 2, 'excluded': 3}
    overlap_count = 0
    for view in views:
        normal = overlap_text(view['text'])
        if view['target_candidate'] and len(pools[normal]) > 1:
            if len(normal) >= recipe['distinctive_duplicate_chars']:
                raise CorpusError('corpus_distinctive_target_split_leak')
            if view['split'] != min(pools[normal], key=order.get):
                view['target_candidate'] = False
                view['flags'] = sorted(set(view['flags']) | {'shared_short_text_context_only'})
                overlap_count += 1
    by_id = {v['message_id']: v for v in views}
    for fragment in fragment_rows:
        fragment['target_ids'] = [i for i in fragment['message_ids'] if by_id[i]['target_candidate']]
        fragment['flags'] = sorted({flag for i in fragment['message_ids'] for flag in by_id[i]['flags']})
    split_counts = {}
    for partition in ('learn','dev_review','final_holdout','excluded'):
        selected = [v for v in views if v['split'] == partition]
        split_counts[partition] = {'messages': len(selected), 'targets': sum(v['target_candidate'] for v in selected),
                                   'fragments': sum(f['split'] == partition for f in fragment_rows),
                                   'groups': sum(g['split'] == partition for g in split_units),
                                   'source_targets': sum(g['source_target_count'] for g in split_units if g['split'] == partition)}
    summary = {'version': VIEW_VERSION, 'source_messages': len(rows),
               'source_text_candidates': sum(v['source_candidate'] for v in views),
               'default_target_candidates': sum(v['target_candidate'] for v in views),
               'redacted_messages': sum(bool(v['redactions']) for v in views),
               'redaction_spans': sum(len(v['redactions']) for v in views),
               'redaction_kinds': dict(Counter(m['kind'] for v in views for m in v['redactions'])),
               'review_flags': dict(Counter(flag for v in views for flag in v['flags'])),
               'fragments': len(fragment_rows), 'split_groups': len(split_units),
               'group_edge_reasons': dict(Counter(e['reason'] for e in edges)),
               'short_shared_heldout_context_only': overlap_count, 'splits': split_counts,
               'three_way_split_available': all(split_counts[p]['targets'] > 0 for p in ('learn','dev_review','final_holdout')),
               'manual_privacy_review_required': True, 'model_input_approved': False, 'model_calls': 0}
    result = {'recipe': recipe, 'messages': views, 'fragments': fragment_rows,
              'groups': split_units, 'edges': edges, 'summary': summary}
    validate_dataset(result, rows, relations)
    return result


def validate_dataset(result, source_rows, relations):
    views, fragments, groups = result['messages'], result['fragments'], result['groups']
    if len(views) != len(source_rows):
        raise CorpusError('corpus_view_source_count_changed')
    by_message = {v['message_id']: v for v in views}
    by_fragment = {f['id']: f for f in fragments}
    by_group = {g['id']: g for g in groups}
    for view, source in zip(views, source_rows):
        if (view['message_id'] != source['id'] or view['ordinal'] != source['ordinal']
                or view['source_candidate'] != bool(source['target_candidate'])
                or view['source_text_hash'] != source['text_hash'] or digest(view['text'].encode('utf-8')) != view['text_hash']):
            raise CorpusError('corpus_view_source_evidence_changed')
        if view['source_candidate'] and 'source_excluded' not in view['flags']:
            verify_redactions(source['text'], view['text'], view['redactions'])
        elif view['text'] or view['redactions'] or view['target_candidate']:
            raise CorpusError('corpus_excluded_source_projected')
    membership = [i for fragment in fragments for i in fragment['message_ids']]
    if membership != [v['message_id'] for v in views]:
        raise CorpusError('corpus_fragment_membership_changed')
    for fragment in fragments:
        group = by_group[fragment['group_id']]
        if fragment['id'] not in group['fragment_ids'] or fragment['split'] != group['split']:
            raise CorpusError('corpus_fragment_group_changed')
        if fragment['target_ids'] != [i for i in fragment['message_ids'] if by_message[i]['target_candidate']]:
            raise CorpusError('corpus_fragment_targets_changed')
        for ident in fragment['message_ids']:
            if by_message[ident]['fragment_id'] != fragment['id'] or by_message[ident]['split'] != fragment['split']:
                raise CorpusError('corpus_message_split_changed')
    for relation in relations:
        if relation['kind'] == 'reply' and relation['state'] == 'resolved':
            a = by_fragment[by_message[relation['source_id']]['fragment_id']]
            b = by_fragment[by_message[relation['target_id']]['fragment_id']]
            if a['group_id'] != b['group_id'] or a['split'] != b['split']:
                raise CorpusError('corpus_reply_split_leak')
    for edge in result['edges']:
        if by_fragment[edge['left']]['group_id'] != by_fragment[edge['right']]['group_id']:
            raise CorpusError('corpus_duplicate_split_leak')
    references = {}
    for view in views:
        if view['target_candidate']:
            normal = overlap_text(view['text'])
            previous = references.setdefault(normal, view['split'])
            if previous != view['split']:
                raise CorpusError('corpus_reference_target_split_leak')
    return {'passed': True, 'messages_checked': len(views), 'reply_edges_checked': sum(r['kind']=='reply' for r in relations)}
