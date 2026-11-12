"""Live public evidence eligibility and rebuild-free lexical overlay reads."""

from datetime import datetime, timedelta, timezone
import time
import unicodedata
import re
import json
from urllib.parse import urlsplit

from .models import RuntimeErrorBase


def key(subject, predicate):
    from .archive import fingerprint
    from .knowledge_lexical import ChineseText
    normal = ChineseText()
    return fingerprint([normal.simplify(unicodedata.normalize('NFKC', item)).strip().casefold()
                        for item in (subject, predicate)])


def enabled(db, persona_id, channel, source_id):
    from .knowledge_service import read, POLICY, SOURCES
    role = db.execute('SELECT p.id,l.deleted_at FROM personas p LEFT JOIN persona_lifecycle l '
                      'ON l.persona_id=p.id WHERE p.id=?', (persona_id,)).fetchone()
    policy = read(db, 'policy.' + persona_id, POLICY)['data']
    source = read(db, 'sources', SOURCES)['data'].get(source_id, {})
    specific = read(db, 'source_policy.' + source_id, SOURCE_POLICY)['data']
    return bool(role and not role['deleted_at'] and policy['enabled'] and channel in policy['channels']
                and source.get('enabled') and specific['enabled'])


SOURCE_POLICY = {'enabled': True, 'freshness_seconds': 86400, 'min_interval_seconds': 1,
                 'store_body': True}


def origin(row):
    """Provenance is supplied by the source adapter, never parsed from body text."""
    metadata = json.loads(row['metadata_json'])
    declared = metadata.get('pcar_provenance')
    if declared is not None:
        if (set(declared) != {'kind', 'name', 'group', 'revision_url'}
                or declared['kind'] != 'controlled_http_fixture'
                or not isinstance(declared['name'], str) or not 1 <= len(declared['name']) <= 80
                or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', declared['group'])):
            raise ValueError('invalid_controlled_provenance')
        address, page = urlsplit(declared['revision_url']), urlsplit(row['url'])
        if (address.scheme != 'http' or address.hostname != '127.0.0.1'
                or address.username or address.password or not address.port
                or (address.scheme, address.hostname, address.port) != (page.scheme, page.hostname, page.port)):
            raise ValueError('invalid_controlled_revision_url')
        return {'source_name': declared['name'], 'source_group': declared['group'],
                'source_origin': declared['kind'], 'version_url': declared['revision_url']}
    version_url = ('https://zh.wikipedia.org/w/index.php?oldid=' + str(row['upstream_revision'])
        if row['source_id'] == 'wikipedia_zh' else 'https://www.wikidata.org/w/index.php?title='
        + row['upstream_id'] + '&oldid=' + str(row['upstream_revision']))
    return {'source_name': '中文Wikipedia' if row['source_id'] == 'wikipedia_zh' else 'Wikidata',
            'source_group': row.get('source_group', 'wikimedia'),
            'source_origin': 'public_wikimedia', 'version_url': version_url}


def instant(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def query_interval(query):
    match = re.search(r'(\d{4})年(?:(\d{1,2})月)?(?:(\d{1,2})日)?|(\d{4})-(\d{2})-(\d{2})', query)
    if not match:
        return None
    year, month, day = (match.group(1), match.group(2), match.group(3)) if match.group(1) else match.groups()[3:]
    try:
        start = datetime(int(year), int(month or 1), int(day or 1), tzinfo=timezone.utc)
        if day:
            end = start + timedelta(days=1)
        elif month:
            end = datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
    except ValueError:
        return None
    return [start.timestamp(), end.timestamp()]


def eligible(db, *, now=None, claim_keys=None, persona_id=None, channel='desktop', interval=None):
    now = time.time() if now is None else now
    rows = []
    query = '''SELECT c.*,d.sha256,d.upstream_revision,d.upstream_at,d.metadata_json,
        p.source_id,p.upstream_id,p.title,p.url,p.last_checked,p.latest_revision,p.state AS page_state,
        h.sha256 AS head_sha256,h.upstream_at AS head_upstream_at,s.source_group,e.start_character,e.end_character,e.quote
        FROM knowledge_claims c JOIN knowledge_batches b ON b.id=c.batch_id JOIN knowledge_documents d ON d.id=c.document_id
        JOIN knowledge_pages p ON p.id=c.page_id JOIN knowledge_sources s ON s.id=p.source_id
        JOIN knowledge_documents h ON h.id=p.head_document_id
        JOIN knowledge_evidence e ON e.claim_id=c.id WHERE c.state='active' AND p.state='active' '''
    args = []
    if persona_id is not None:
        query+=' AND b.owner_persona=?'
        args.append(persona_id)
    if claim_keys is not None:
        if not claim_keys:
            return []
        query += ' AND c.claim_key IN (' + ','.join('?' for _ in claim_keys) + ')'
        args.extend(claim_keys)
    for record in db.execute(query, args):
        row = dict(record)
        try:
            row.update(origin(row))
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if persona_id and row['source_id']=='wikipedia_zh':
            from .knowledge_web_maintenance import withdrawn_version
            if any(withdrawn_version(db,persona_id,row['page_id'],revision) for revision in {row['upstream_revision'],row['latest_revision']}):continue
        if persona_id and not enabled(db, persona_id, channel, row['source_id']):
            continue
        begin, end = instant(row['valid_from']), instant(row['valid_until'])
        lower, upper = interval if interval else (now, now)
        outside_start = begin is not None and (begin >= upper if interval else begin > upper)
        if (row['sha256'] != row['head_sha256'] or outside_start
                or end is not None and end <= lower):
            continue
        from .knowledge_output import RELATIVE_TIME
        if re.search(RELATIVE_TIME,row['quote']) and row['upstream_at'] != row['head_upstream_at']:
            continue
        rows.append(row)
    groups = {}
    for row in rows:
        if row['kind'] in ('fact', 'source_claim'):
            groups.setdefault(row['claim_key'], []).append(row)
    for row in rows:
        start, end = instant(row['valid_from']), instant(row['valid_until'])
        row['conflict'] = any(other['value'].strip() != row['value'].strip()
            and (end is None or instant(other['valid_from']) is None or instant(other['valid_from']) < end)
            and (start is None or instant(other['valid_until']) is None or instant(other['valid_until']) > start)
            for other in groups.get(row['claim_key'], []))
    return rows


def evidence_usable(db, persona_id, channel, item):
    target = db.execute('SELECT claim_key FROM knowledge_claims WHERE id=?', (item.get('claim_id'),)).fetchone()
    if not target:
        return False
    for row in eligible(db, claim_keys=[target['claim_key']], persona_id=persona_id, channel=channel,
                        interval=item.get('effective_interval')):
        if row['id'] != item.get('claim_id'):
            continue
        if (row['document_id'] != item.get('document_id') or row['sha256'] != item.get('document_sha256')
                or not enabled(db, persona_id, channel, row['source_id'])):
            return False
        for name in ('source_name', 'source_group', 'source_origin', 'version_url'):
            if name in item and item[name] != row[name]:
                return False
            if row['source_origin'] == 'controlled_http_fixture' and name not in item:
                return False
        if 'claim' in item and item['claim'] != {field: row[field] for field in ('subject', 'predicate', 'value')}:
            return False
        policy = source_policy(db, row['source_id'])
        if item.get('require_current') and time.time() - row['last_checked'] > policy['freshness_seconds']:
            return False
        return bool(row['conflict']) == (item.get('claim_state') == 'conflict')
    return False


def source_policy(db, source_id):
    from .knowledge_service import read
    return read(db, 'source_policy.' + source_id, SOURCE_POLICY)['data']


def search(db, persona_id, channel, query, *, current=False):
    from .chat import terms
    from .knowledge_lexical import ChineseText
    normalize=ChineseText().simplify
    db.create_function('knowledge_normalize',1,lambda value:normalize(value or ''))
    wanted = terms(normalize(query))
    interval = query_interval(query)
    candidates = []
    tokens = sorted(wanted)[:32]
    if not tokens:
        return []
    expression = '+'.join('(instr(knowledge_normalize(c.subject||c.predicate||c.value||p.title),?)>0)' for _ in tokens)
    keys = [row[0] for row in db.execute('SELECT c.claim_key,max(' + expression + ') AS score, '
        'max(c.created_epoch) AS recorded FROM knowledge_claims c JOIN knowledge_batches b ON b.id=c.batch_id JOIN knowledge_pages p ON p.id=c.page_id '
        "WHERE c.state='active' AND b.owner_persona=? GROUP BY c.claim_key HAVING score>0 ORDER BY score DESC,recorded DESC LIMIT 40", tokens+[persona_id])]
    for row in eligible(db, claim_keys=keys, persona_id=persona_id, channel=channel, interval=interval):
        if not enabled(db, persona_id, channel, row['source_id']):
            continue
        policy = source_policy(db, row['source_id'])
        fresh = time.time() - row['last_checked'] <= policy['freshness_seconds']
        if current and not fresh:
            continue
        score = len(wanted & terms(normalize(' '.join(row[key] for key in ('subject', 'predicate', 'value', 'title')))))
        if score:
            candidates.append((score, row['created_epoch'], row))
    result = []
    for _, _, row in sorted(candidates, key=lambda entry: (-entry[0], -entry[1], entry[2]['id']))[:6]:
        from .knowledge_service import read, POLICY
        result.append({'kind': 'knowledge_claim', 'claim_id': row['id'], 'document_id': row['document_id'],
            'document_sha256': row['sha256'], 'source_id': row['source_id'], 'source_group': row['source_group'],
            'article_id': row['page_id'], 'article_title': row['title'],
            'section_id': row['document_id'], 'section_title': row['predicate'],
            'start': row['start_character'], 'end': row['end_character'], 'quote': row['quote'],
            'section_sha256': row['sha256'],
            'claim_text': row['subject'] + ' ' + row['predicate'] + ' ' + row['value'],
            'claim': {field: row[field] for field in ('subject', 'predicate', 'value')},
            'claim_kind': row['kind'], 'claim_state': 'conflict' if row['conflict'] else 'source_checked',
            'upstream_revision': row['upstream_revision'], 'source_snapshot_at': row['upstream_at'],
            'observed_revision': row['latest_revision'],
            'source_policy_revision': read(db,'source_policy.'+row['source_id'],SOURCE_POLICY)['revision'],
            'reader_policy_revision': read(db,'policy.'+persona_id,POLICY)['revision'],
            'last_checked_epoch': row['last_checked'], 'valid_from': row['valid_from'],
            'valid_until': row['valid_until'], 'url': row['url'], 'require_current': current,
            'version_url': row['version_url'], 'source_name': row['source_name'],
            'source_origin': row['source_origin'],
            'effective_interval': interval,
            'time_state': 'dated' if interval else 'fresh' if time.time() - row['last_checked'] <= source_policy(db,row['source_id'])['freshness_seconds'] else 'snapshot'})
    return result


def merge(base, overlay):
    if not overlay:
        return base
    result = dict(base)
    selected, articles, characters = [], set(), 0
    for item in overlay + base.get('evidence', []):
        if len(selected) == 6 or characters + len(item['quote']) > 3000:
            continue
        identity = (item['kind'], item['article_id'])
        if identity not in articles and len(articles) == 3:
            continue
        articles.add(identity)
        characters += len(item['quote'])
        selected.append(item)
    result.update(state='ready', evidence=selected)
    candidates, seen = [], set()
    for item in selected:
        if item['kind'] != 'knowledge_claim' or item['article_id'] in seen:
            continue
        seen.add(item['article_id'])
        candidates.append({**{field: value for field, value in item.items() if field != 'quote'},
                           'title': item['article_title']})
    result['candidates'] = (candidates + base.get('candidates', []))[:20]
    return result


def check_document(db, payload):
    data = payload['external_knowledge']
    document = db.execute('SELECT d.*,p.head_document_id,p.state AS page_state,h.sha256 AS head_sha256 '
        'FROM knowledge_documents d JOIN knowledge_pages p ON p.id=d.page_id '
        'JOIN knowledge_documents h ON h.id=p.head_document_id WHERE d.id=?', (data['document_id'],)).fetchone()
    if (not document or document['page_state'] != 'active' or document['sha256'] != data['document_sha256']
            or document['sha256'] != document['head_sha256']):
        raise RuntimeErrorBase('source_expired')
    return dict(document)
