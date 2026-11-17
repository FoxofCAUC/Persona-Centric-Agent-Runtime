"""Parse Moect 1.3.0 quick-export private chats; quoted text stays separate.

Format reference: mofan6/Moect v1.3.0, core/exporting.py and wx4/reader.py.
This adapter reads exported files only; it has no WeChat process or network API.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

from .corpus_files import CorpusError, MAX_FILE_BYTES, digest
from .corpus_store import fingerprint


PARSER_VERSION = 'moect-1.3.0-private-quick-v1'
MAX_RECORDS = 250_000
MAX_NESTING = 8


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError('moect_duplicate_json_key')
        result[key] = value
    return result


def _string(value):
    if not isinstance(value, str):
        raise CorpusError('moect_text_field_invalid')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise CorpusError('moect_text_field_invalid') from None
    return value


def _display(node, depth=0):
    if depth > MAX_NESTING or not isinstance(node, dict):
        raise CorpusError('moect_nested_record_invalid')
    if node.get('resources'):
        raise CorpusError('moect_quick_export_required')
    body = [_string(node.get('text', ''))]
    quote = node.get('quote')
    if quote:
        quoted = _display(quote, depth + 1)
        body.append('\n> ' + _string(quote.get('sender') or '') + ': ' + quoted.replace('\n', '\n> '))
    forward = node.get('forward')
    if forward:
        if not isinstance(forward, dict) or not isinstance(forward.get('messages', []), list):
            raise CorpusError('moect_forward_invalid')
        body.append('\n【' + _string(forward.get('title', '')) + '】')
        for child in forward.get('messages', []):
            body.extend(('\n[', _string(child.get('time_str', '')), '] ',
                         _string(child.get('sender', '')), '\n', _display(child, depth + 1)))
        if forward.get('notice'):
            body.append('\n' + _string(forward['notice']))
    if node.get('resource_notice'):
        body.append('\n' + _string(node['resource_notice']))
    return ''.join(body)


def render_markdown(data):
    """Reconstruct the export layout for comparison, never a model prompt."""
    meta = data['metadata']
    chunks = ['# ', _string(meta['name']), '\n由 ', _string(meta['application']), ' 导出\n\n']
    for message in data['messages']:
        chunks.extend(('[', _string(message['time_str']), '] ', _string(message['sender']),
                       '\n', _display(message), '\n\n'))
    return ''.join(chunks)


def speaker_token(key, dataset_id, source_id):
    if not isinstance(key, bytes) or len(key) != 32:
        raise CorpusError('corpus_identity_key_invalid')
    payload = json.dumps([dataset_id, source_id], ensure_ascii=False).encode('utf-8')
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _target_class(message):
    if message['kind'] == 'system' or message['type'] in (10000, 10002):
        return False, 'system_message'
    if not message['text'].strip():
        return False, 'empty_text'
    if message['kind'] == 'text' and message['type'] == message['raw_type'] == 1:
        return True, None
    extra = message.get('extra', {})
    if (message['kind'] == 'quote' and message['type'] == 49
            and extra.get('appmsg_type') == 57 and extra.get('reply')
            and isinstance(extra.get('title'), str) and extra['title'].strip()
            and message['text'] == extra['title']):
        return True, None
    return False, 'application_text_unconfirmed' if message['kind'] == 'text' else 'non_original_text'


def parse_private_export(json_raw, md_raw, *, identity_key, dataset_id):
    if not json_raw or not md_raw or max(len(json_raw), len(md_raw)) > MAX_FILE_BYTES:
        raise CorpusError('moect_file_capacity_or_empty')
    try:
        data = json.loads(json_raw.decode('utf-8-sig'), object_pairs_hook=_object)
        markdown = md_raw.decode('utf-8-sig')
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CorpusError('moect_document_invalid') from None
    if not isinstance(data, dict) or set(data) != {'metadata', 'messages', 'count'}:
        raise CorpusError('moect_document_shape_invalid')
    meta, messages = data['metadata'], data['messages']
    if (not isinstance(meta, dict) or meta.get('application') != 'Moect' or meta.get('version') != '1.3.0'
            or meta.get('demo') is not False or meta.get('time_zone') != 'Asia/Shanghai'
            or not isinstance(meta.get('export_options'), dict)
            or meta['export_options'].get('quick_export') is not True):
        raise CorpusError('moect_export_contract_unsupported')
    session = _string(meta.get('session'))
    if not session or session.endswith('@chatroom'):
        raise CorpusError('moect_private_session_required')
    if (not isinstance(messages, list) or not 0 < len(messages) <= MAX_RECORDS
            or type(data['count']) is not int or data['count'] != len(messages)):
        raise CorpusError('moect_record_count_invalid')
    self_ids, peer_ids, native_ids = set(), set(), set()
    previous = -1
    for message in messages:
        if not isinstance(message, dict):
            raise CorpusError('moect_record_invalid')
        for name in ('server_id', 'time_str', 'sender_wxid', 'sender', 'session', 'text', 'kind'):
            _string(message.get(name))
        if (message['session'] != session or not message['server_id']
                or message['server_id'] in native_ids):
            raise CorpusError('moect_message_identity_conflict')
        native_ids.add(message['server_id'])
        for name in ('local_id', 'shard', 'type', 'raw_type', 'create_time'):
            if type(message.get(name)) is not int:
                raise CorpusError('moect_numeric_field_invalid')
        if type(message.get('is_sender')) is not bool or not isinstance(message.get('extra'), dict):
            raise CorpusError('moect_record_invalid')
        if message['create_time'] < previous:
            raise CorpusError('moect_record_order_changed')
        previous = message['create_time']
        if previous:
            try:
                expected = datetime.fromtimestamp(previous, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            except (OverflowError, OSError, ValueError):
                raise CorpusError('moect_timestamp_invalid') from None
            if message['time_str'] != expected:
                raise CorpusError('moect_timezone_mismatch')
        if message['kind'] != 'system' and message['type'] not in (10000, 10002):
            if not message['sender_wxid']:
                raise CorpusError('moect_private_speaker_unresolved')
            (self_ids if message['is_sender'] else peer_ids).add(message['sender_wxid'])
    if len(self_ids) != 1 or peer_ids != {session} or self_ids & peer_ids:
        raise CorpusError('moect_private_speakers_ambiguous')
    try:
        expected_md = render_markdown(data)
    except (KeyError, TypeError, AttributeError, RecursionError):
        raise CorpusError('moect_markdown_layout_invalid') from None
    if expected_md != markdown:
        raise CorpusError('moect_markdown_pair_mismatch')
    self_id = next(iter(self_ids))
    identity = {}
    for original, role in ((self_id, 'self'), (session, 'peer')):
        token = speaker_token(identity_key, dataset_id, original)
        identity[token] = {'source_id': original, 'role': role,
                           'names': sorted({m['sender'] for m in messages if m['sender_wxid'] == original})}
    rows, relations = [], []
    id_map = {m['server_id']: fingerprint([dataset_id, session, m['server_id']]) for m in messages}
    offset = len('# ' + meta['name'] + '\n由 ' + meta['application'] + ' 导出\n\n')
    counts, excluded, roles = Counter(), Counter(), Counter()
    for ordinal, message in enumerate(messages):
        is_system = message['kind'] == 'system' or message['type'] in (10000, 10002)
        role = 'system' if is_system else ('self' if message['is_sender'] else 'peer')
        token = None if is_system else speaker_token(identity_key, dataset_id, message['sender_wxid'])
        target, reason = _target_class(message)
        target = target and role in ('self', 'peer')
        if target:
            roles[role] += 1
        else:
            excluded[reason or 'speaker_unresolved'] += 1
        rendered = _display(message)
        header = '[' + message['time_str'] + '] ' + message['sender'] + '\n'
        rows.append({'id': id_map[message['server_id']], 'ordinal': ordinal,
                     'platform_message_id': message['server_id'],
                     'source_locator': '/messages/' + str(ordinal),
                     'local_id': message['local_id'], 'shard': message['shard'],
                     'speaker_token': token, 'role': role, 'kind': message['kind'],
                     'created_epoch': message['create_time'], 'time_str': message['time_str'],
                     'text': message['text'], 'text_hash': digest(message['text'].encode('utf-8')),
                     'record_hash': fingerprint(message), 'target_candidate': target,
                     'exclusion_code': reason,
                     'md_text_start': offset + len(header),
                     'md_text_end': offset + len(header) + len(message['text'])})
        offset += len(header) + len(rendered) + 2
        counts[message['kind']] += 1
        reply = message['extra'].get('reply')
        if message['kind'] == 'quote':
            if not isinstance(reply, dict) or not isinstance(reply.get('server_id'), str):
                raise CorpusError('moect_reply_identity_invalid')
            target_id = reply['server_id']
            quote = message.get('quote') or {}
            if quote.get('server_id') and quote['server_id'] != target_id:
                raise CorpusError('moect_reply_evidence_conflict')
            relations.append({'source_id': rows[-1]['id'], 'target_id': id_map.get(target_id),
                              'external_message_id': target_id, 'kind': 'reply',
                              'state': 'resolved' if target_id in id_map else 'unresolved',
                              'evidence_locator': '/messages/' + str(ordinal) + '/extra/reply'})
        if message.get('forward'):
            relations.append({'source_id': rows[-1]['id'], 'target_id': None,
                              'external_message_id': None, 'kind': 'forward', 'state': 'embedded',
                              'evidence_locator': '/messages/' + str(ordinal) + '/forward'})
    summary = {'parser': PARSER_VERSION, 'records': len(rows), 'kind_counts': dict(counts),
               'text_candidates': sum(roles.values()), 'candidate_speakers': dict(roles),
               'excluded_counts': dict(excluded), 'reply_edges': sum(r['kind'] == 'reply' for r in relations),
               'resolved_replies': sum(r['kind'] == 'reply' and r['state'] == 'resolved' for r in relations),
               'unresolved_replies': sum(r['state'] == 'unresolved' for r in relations),
               'forward_containers': counts['chatrecord'], 'markdown_exact_text_match': True,
               'source_hash': digest(json_raw), 'markdown_hash': digest(md_raw),
               'first_time': messages[0]['time_str'], 'last_time': messages[-1]['time_str'],
               'model_input_approved': False, 'learning_eligible': False}
    return {'summary': summary, 'rows': rows, 'relations': relations,
            'identity': identity, 'metadata': meta, 'original_messages': messages}
