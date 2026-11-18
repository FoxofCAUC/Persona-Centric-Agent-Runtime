"""Freeze human-selected development conversations from their original turn boundaries."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import groupby
import json
from pathlib import Path
import re
import unicodedata

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


VERSION = 'el4-fresh-development-scenes-v1'
LOCAL_TIME = timezone(timedelta(hours=8))
IDENTIFIER = re.compile(
    r'https?://|www\.|[a-z0-9+_./=\-]{14,}|[0-9]{7}|微信号|手机号|密钥|密码|身份证|'
    r'api.?key|转账|支付宝|(?:\[|【)[^\]\n】]{1,20}(?:\]|】)|<[^>]+>|引用|聊天记录|视频链接|看图',
    re.IGNORECASE)


def check(condition, reason):
    if not condition:
        raise CorpusError('fresh_scene_' + reason)


def text_key(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def previous_inputs(db, previous, view):
    positions = previous['positions']
    groups, used = set(), set()
    check(len(positions) == 16 and previous['view_payload_hash'] == view['payload_hash'], 'old_positions')
    for p in positions:
        row = db.execute("""
            SELECT f.group_id,m.text_hash FROM corpus_view_messages m
            JOIN corpus_view_fragments f ON f.view_id=m.view_id
                AND f.id=json_extract(m.data_json,'$.fragment_id') AND f.split='dev_review'
            WHERE m.view_id=? AND m.message_id=? AND m.split='dev_review'""",
            (view['id'], p['message_id'])).fetchone()
        check(row and row['group_id'] == p['group_id'] and p['group_id'] not in groups
              and row['text_hash'] == p['input_text_hash'] == digest(p['input_text'].encode())
              and p['view_id'] == view['id'] and p['view_payload_hash'] == view['payload_hash'], 'old_source')
        groups.add(p['group_id'])
        used.add(text_key(p['input_text']))
    return groups, used


def fragment_messages(db, view, first, seen_groups):
    anchor = db.execute("""
        SELECT f.id,f.group_id,f.data_json
        FROM corpus_view_messages m JOIN corpus_view_fragments f ON f.view_id=m.view_id
            AND f.id=json_extract(m.data_json,'$.fragment_id') AND f.split='dev_review'
        JOIN corpus_view_groups g ON g.view_id=f.view_id AND g.id=f.group_id AND g.split='dev_review'
        WHERE m.view_id=? AND m.ordinal=? AND m.split='dev_review'""", (view['id'], first)).fetchone()
    check(anchor and anchor['group_id'] not in seen_groups, 'source_group')
    fragment = json.loads(anchor['data_json'])
    check(fragment['id'] == anchor['id'] and fragment['group_id'] == anchor['group_id']
          and fragment['split'] == 'dev_review', 'fragment_binding')
    rows = db.execute("""
        SELECT m.data_json,m.message_id,m.text_hash,m.ordinal,m.target_candidate,
               r.created_epoch,r.role,r.text_hash AS original_hash,r.text AS original,r.kind,
               EXISTS(SELECT 1 FROM corpus_relations e WHERE e.parse_id=r.parse_id
                                                        AND e.source_id=r.id) AS has_edge
        FROM corpus_view_messages m JOIN corpus_messages r
            ON r.parse_id=? AND r.id=m.message_id AND r.ordinal=m.ordinal
        WHERE m.view_id=? AND m.split='dev_review' AND json_extract(m.data_json,'$.fragment_id')=?
        ORDER BY m.ordinal""", (view['parse_id'], view['id'], anchor['id']))
    messages = []
    for row in rows:
        m = json.loads(row['data_json'])
        check(m['message_id'] == row['message_id'] and m['ordinal'] == row['ordinal']
              and m['fragment_id'] == anchor['id'] and m['split'] == 'dev_review'
              and m['role'] == {'self': 'A', 'peer': 'B', 'system': 'SYS'}[row['role']]
              and m['kind'] == row['kind'] and m['target_candidate'] == bool(row['target_candidate'])
              and m['text_hash'] == row['text_hash'] == digest(m['text'].encode())
              and m['source_text_hash'] == row['original_hash'], 'message_binding')
        messages.append(m | {'epoch':row['created_epoch'], 'edge':bool(row['has_edge']),
                             'verbatim':m['text'] == row['original']
                             and m['text_hash'] == m['source_text_hash']})
    check([m['message_id'] for m in messages] == fragment['message_ids'], 'fragment_members')
    return fragment, messages


def clean_text(message):
    return (message['role'] in ('A', 'B') and message['kind'] == 'text' and message['verbatim']
            and not message['edge'] and not message['redactions'] and message['source_candidate']
            and 1 <= len(message['text'].strip()) <= 240 and not IDENTIFIER.search(message['text']))


def make_scene(db, view, spec, number, seen_groups, used_text):
    first, count, title = spec['first_ordinal'], spec['turn_count'], spec['title']
    check(type(first) is int and type(count) is int and 1 <= count <= 4
          and isinstance(title, str) and 0 < len(title) <= 80, 'spec')
    fragment, messages = fragment_messages(db, view, first, seen_groups)
    seen_groups.add(fragment['group_id'])
    runs = [list(run) for _, run in groupby(messages, key=lambda m: m['role'])]
    start = next((i for i, run in enumerate(runs) if run[0]['ordinal'] == first), None)
    check(start is not None, 'not_turn_start')
    window = runs[start:start + 2 * count - 1]
    check(len(window) == 2 * count - 1 and window[-1][-1]['epoch'] - window[0][0]['epoch'] <= 7200,
          'incomplete_conversation')
    speaker = window[0][0]['role']
    context = []
    for run in window[1::2]:
        check(all(m['role'] in ('A', 'B') and m['role'] != speaker and
                  (clean_text(m) or (m['kind'] == 'emoji' and not m['edge'] and not m['text']))
                  for m in run), 'intervening_media_or_quote')
        context.append({'message_ids':[m['message_id'] for m in run], 'kinds':[m['kind'] for m in run]})
    turns = []
    for i, run in enumerate(window[::2], 1):
        check(all(m['role'] == speaker and clean_text(m) and m['target_candidate'] and not m['flags']
                  for m in run), 'ineligible_input')
        for m in run:
            key = text_key(m['text'])
            check(key and key not in used_text, 'duplicate_input')
            used_text.add(key)
        text = '\n'.join(m['text'] for m in run)
        stamp = datetime.fromtimestamp(run[-1]['epoch'], LOCAL_TIME).isoformat()
        turns.append({'turn_number':i, 'text':text, 'accepted_at':stamp,
                      'message_ids':[m['message_id'] for m in run],
                      'message_hashes':[m['text_hash'] for m in run], 'text_hash':digest(text.encode())})
    return {'id':f'G{number:02}', 'title':title, 'speaker':speaker, 'group_id':fragment['group_id'],
            'fragment_id':fragment['id'], 'first_ordinal':first, 'turn_count':count, 'turns':turns,
            'omitted_counterpart_turns':context}


def build_materials(corpus, previous_plan, selection):
    """Use only the dev split. The previous frozen positions establish the excluded groups."""
    view = corpus.view_status(previous_plan['view_id'])
    check(corpus.root == Path(previous_plan['corpus_root']).resolve(), 'corpus_root')
    check(isinstance(selection, list) and selection, 'empty_selection')
    with corpus.connection() as db:
        old_groups, used = previous_inputs(db, previous_plan, view)
        seen = set(old_groups)
        scenes = [make_scene(db, view, spec, i, seen, used) for i, spec in enumerate(selection, 1)]
    packet = {'version':VERSION, 'split':'dev_review', 'view_id':view['id'],
              'view_payload_hash':view['payload_hash'], 'source_proof_hash':view['source_proof_hash'],
              'previous_positions_hash':fingerprint(previous_plan['positions']),
              'excluded_prior_groups':len(old_groups), 'scenes':scenes}
    return packet | {'materials_hash':fingerprint(packet)}


def verify_materials(corpus, material, previous_plan):
    """Rebuild every selected turn against live permissions, original texts and timestamps."""
    selection = [{k:s[k] for k in ('first_ordinal','turn_count','title')} for s in material['scenes']]
    check(build_materials(corpus, previous_plan, selection) == material, 'frozen_materials_changed')
    return {'scenes':len(selection), 'turns':sum(s['turn_count'] for s in selection),
            'source_authority_valid':True, 'materials_hash':material['materials_hash']}


def report(material):
    scenes = material['scenes']
    lengths = Counter(s['turn_count'] for s in scenes)
    speakers = Counter(t for s in scenes for t in [s['speaker']] * s['turn_count'])
    bubbles = sum(len(t['message_ids']) for s in scenes for t in s['turns'])
    emoji = sum(r['kinds'].count('emoji') for s in scenes for r in s['omitted_counterpart_turns'])
    return '\n'.join([
        '# E.L4 新场景选材记录', '',
        f'本轮取自同一份获准使用的微信私聊。{len(scenes)} 个场景来自不同的开发审阅分组，'
        f'共 {sum(speakers.values())} 个用户轮次、{bubbles} 条原始气泡。'
        f'真人 A 提供 {speakers["A"]} 轮，真人 B 提供 {speakers["B"]} 轮。',
        '', '按轮数分布为' + '、'.join(f'{n} 轮 {lengths[n]} 组' for n in sorted(lengths)) + '。',
        '', '已避开旧 16 个分组。选材仅查询开发审阅分区。学习分区和最终留出分区的正文未用于挑选。',
        '', '同一场景保留同一位真人连续的自然发言轮次。一次发言中连发的多条原始气泡按顺序用换行连接。'
        '两轮之间对方的原话只用来判断读者能否跟上，实际模拟由各条链写入自己的首次人物回复。'
        f'轮间有 {emoji} 条表情消息，所在位置可仅凭发言人的文字读通。'
        '时间取该次发言末条原消息的时间。标题仅供人阅读。',
        '', '这些场景反映这一份私聊所含的话题。单轮用于比较眼前一句话的回答，连续场景还可观察后续如何接话。'
        '真人发言在各链按同一脚本重放，后续轮次不会随模型回答调整。', '',
        '| 场景 | 话题 | 真人 | 轮数 |', '| --- | --- | --- | --- |',
        *(f"| {s['id']} | {s['title']} | {s['speaker']} | {s['turn_count']} |" for s in scenes), ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous-plan', required=True, type=Path)
    parser.add_argument('--selection', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    previous = read_private(args.previous_plan)
    corpus = DatasetCorpusStore(previous['corpus_root'])
    material = build_materials(corpus, previous, read_private(args.selection))
    output = outside_repository(args.output)
    save_once(output / 'materials.private.json', material)
    checked = verify_materials(corpus, read_private(output / 'materials.private.json'), previous)
    save_text_once(output / 'materials-report.md', report(material))
    print(json.dumps(checked))


if __name__ == '__main__':
    main()
