"""Freeze an intact private holdout window using the desktop rolling input policy."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.ingress import ROLLING_PARAMS
from .corpus_reply_study import read_private, save_once
from .fresh_scene_material import LOCAL_TIME, clean_text, previous_inputs, text_key


VERSION = 'en3-ling-final-holdout-v1'
SPLIT = 'final_holdout'
TURN_COUNT = 6


def require(condition, reason):
    if not condition:
        raise CorpusError('en3_holdout_' + reason)


def stamp(epoch):
    return datetime.fromtimestamp(epoch, LOCAL_TIME).isoformat()


def excluded_inputs(db, view, previous_plan, fresh):
    groups, texts = previous_inputs(db, previous_plan, view)
    require(fresh['materials_hash'] == fingerprint({k:v for k,v in fresh.items() if k != 'materials_hash'})
            and fresh['previous_positions_hash'] == fingerprint(previous_plan['positions'])
            and fresh['view_payload_hash'] == view['payload_hash'] and fresh['view_id'] == view['id']
            and fresh['split'] == 'dev_review', 'development_snapshot_changed')
    for scene in fresh['scenes']:
        require(scene['group_id'] not in groups, 'development_group_reused')
        groups.add(scene['group_id'])
        for turn in scene['turns']:
            parts = []
            for ident, expected in zip(turn['message_ids'], turn['message_hashes'], strict=True):
                row = db.execute("""SELECT m.data_json,m.text_hash,f.group_id FROM corpus_view_messages m
                    JOIN corpus_view_fragments f ON f.view_id=m.view_id AND f.id=json_extract(m.data_json,'$.fragment_id')
                    WHERE m.view_id=? AND m.message_id=? AND m.split='dev_review' AND f.split='dev_review'""",
                    (view['id'], ident)).fetchone()
                require(row and row['group_id'] == scene['group_id'] and row['text_hash'] == expected, 'development_binding')
                part = json.loads(row['data_json'])['text']
                require(digest(part.encode()) == expected, 'development_text_changed')
                parts.append(part)
                texts.add(text_key(part))
            require('\n'.join(parts) == turn['text'], 'development_turn_changed')
    return groups, texts


def load_fragment(db, view, first):
    anchor = db.execute("""SELECT f.id,f.data_json,f.group_id FROM corpus_view_fragments f
        JOIN corpus_view_messages m ON m.view_id=f.view_id AND json_extract(m.data_json,'$.fragment_id')=f.id
        JOIN corpus_view_groups g ON g.view_id=f.view_id AND g.id=f.group_id
        WHERE m.view_id=? AND m.ordinal=? AND m.split=? AND f.split=? AND g.split=?""",
        (view['id'], first, SPLIT, SPLIT, SPLIT)).fetchone()
    require(anchor, 'fragment_not_held_out')
    fragment = json.loads(anchor['data_json'])
    require((fragment['id'],fragment['group_id'],fragment['split']) == (anchor['id'],anchor['group_id'],SPLIT),
            'fragment_changed')
    messages = []
    for row in db.execute("""SELECT m.message_id,m.text_hash,m.ordinal,m.target_candidate,m.data_json,
        r.created_epoch,r.role,r.kind,r.text AS original,r.text_hash AS source_hash,
        EXISTS(SELECT 1 FROM corpus_relations e WHERE e.parse_id=r.parse_id AND e.source_id=r.id) AS edge
        FROM corpus_view_messages m JOIN corpus_messages r ON r.parse_id=? AND r.id=m.message_id AND r.ordinal=m.ordinal
        WHERE m.view_id=? AND m.split=? AND json_extract(m.data_json,'$.fragment_id')=? ORDER BY m.ordinal""",
        (view['parse_id'], view['id'], SPLIT, anchor['id'])):
        m = json.loads(row['data_json'])
        require(m['message_id'] == row['message_id'] and m['ordinal'] == row['ordinal']
                and m['kind'] == row['kind'] and m['role'] == {'self':'A','peer':'B','system':'SYS'}[row['role']]
                and m['target_candidate'] == bool(row['target_candidate']) and m['split'] == SPLIT
                and m['fragment_id'] == fragment['id'] and m['source_text_hash'] == row['source_hash']
                and m['text_hash'] == row['text_hash'] == digest(m['text'].encode()), 'message_binding')
        messages.append(m | {'epoch':row['created_epoch'], 'edge':bool(row['edge']),
                             'verbatim':m['text'] == row['original'] and m['source_text_hash'] == m['text_hash']})
    require(fragment['message_ids'] == [m['message_id'] for m in messages], 'fragment_incomplete')
    return fragment, messages


def user_batches(messages, speaker):
    """Reconstruct acceptance using the source clock and the rolling desktop collect limits."""
    batches, chars = [], 0
    for index, m in enumerate(messages):
        if m['role'] != speaker:
            continue
        if batches:
            last = messages[batches[-1][-1]]['epoch']
            require(m['epoch'] >= last, 'source_clock_order')
        if (not batches or (m['epoch'] - last) * 1000 >= ROLLING_PARAMS['quiet_ms']
                or len(batches[-1]) >= ROLLING_PARAMS['max_messages']
                or chars + len(m['text']) > ROLLING_PARAMS['max_chars']):
            batches.append([])
            chars = 0
        batches[-1].append(index)
        chars += len(m['text'])
    return batches


def scene_window(fragment, messages, spec, number, used):
    first = spec['first_ordinal']
    require(type(first) is int and isinstance(spec['title'], str) and 0 < len(spec['title']) <= 80, 'invalid_selection')
    anchor = next((m for m in messages if m['ordinal'] == first), None)
    require(anchor and anchor['role'] in ('A','B'), 'anchor_invalid')
    speaker = anchor['role']
    batches = user_batches(messages, speaker)
    index = next((i for i,b in enumerate(batches) if messages[b[0]]['ordinal'] == first), None)
    require(index is not None, 'partial_batch_start')
    six = batches[index:index+TURN_COUNT]
    require(len(six) == TURN_COUNT, 'incomplete_window')
    turns, gaps = [], []
    for n, batch in enumerate(six):
        require(len(batch) == batch[-1]-batch[0]+1, 'other_speaker_inside_batch')
        bubbles = []
        for k in batch:
            m = messages[k]
            key = text_key(m['text'])
            require(clean_text(m) and m['target_candidate'] and not m['flags'] and key and key not in used,
                    'ineligible_or_repeated_input')
            used.add(key)
            bubbles.append({'message_id':m['message_id'], 'text':m['text'], 'ordinal':m['ordinal'],
                            'text_hash':m['text_hash'], 'accepted_at':stamp(m['epoch'])})
        body = '\n'.join(b['text'] for b in bubbles)
        turns.append({'turn_number':n+1, 'text':body, 'accepted_at':bubbles[-1]['accepted_at'],
                      'message_ids':[b['message_id'] for b in bubbles], 'message_hashes':[b['text_hash'] for b in bubbles],
                      'text_hash':digest(body.encode()), 'bubbles':bubbles})
        if n:
            previous = six[n-1][-1]
            between = messages[previous+1:batch[0]]
            require(all(m['role'] in ('A','B') and m['role'] != speaker and
                        (clean_text(m) or (m['kind']=='emoji' and not m['edge'] and not m['text']))
                        for m in between), 'intervening_attachment_or_reference')
            gap = messages[batch[0]]['epoch'] - messages[previous]['epoch']
            require(gap*1000 >= ROLLING_PARAMS['quiet_ms'], 'non_quiet_boundary')
            gaps.append({'after_turn':n, 'seconds':gap, 'reason':'quiet',
                         'message_ids':[m['message_id'] for m in between], 'kinds':[m['kind'] for m in between]})
    require(any('text' in g['kinds'] for g in gaps), 'no_counterpart_turn')
    return {'id':f'G{number:02}', 'title':spec['title'], 'speaker':speaker, 'group_id':fragment['group_id'],
            'fragment_id':fragment['id'], 'first_ordinal':first, 'turn_count':len(turns), 'turns':turns,
            'omitted_counterpart_turns':gaps}


def build_materials(corpus, previous_plan, fresh, selection):
    require(corpus.root == Path(previous_plan['corpus_root']).resolve(), 'corpus_root')
    require(len(selection) == 2, 'two_conversations_required')
    with corpus.connection() as db:
        db.execute('BEGIN')
        view = corpus._view_status(db, previous_plan['view_id'])
        require(previous_plan['view_payload_hash'] == view['payload_hash'] and fresh['source_proof_hash'] == view['source_proof_hash'],
                'view_changed')
        groups, used = excluded_inputs(db, view, previous_plan, fresh)
        excluded_count = len(groups)
        scenes = []
        for number, spec in enumerate(selection, 1):
            fragment, messages = load_fragment(db, view, spec['first_ordinal'])
            require(fragment['group_id'] not in groups, 'duplicate_or_development_group')
            groups.add(fragment['group_id'])
            scenes.append(scene_window(fragment,messages,spec,number,used))
        binding = db.execute('SELECT source_proof_json FROM corpus_dataset_versions WHERE id=?',(view['id'],)).fetchone()
    packet = {'version':VERSION, 'split':SPLIT, 'view_id':view['id'], 'view_payload_hash':view['payload_hash'],
              'source_proof_hash':view['source_proof_hash'], 'original_parse_id':view['parse_id'],
              'source_authority':json.loads(binding[0]), 'previous_positions_hash':fresh['previous_positions_hash'],
              'dev_materials_hash':fresh['materials_hash'], 'excluded_prior_groups':excluded_count,
              'boundary_policy':dict(ROLLING_PARAMS), 'scenes':scenes}
    return packet | {'materials_hash':fingerprint(packet)}


def verify_materials(corpus, material, previous_plan, fresh_material):
    selection = [{'first_ordinal':s['first_ordinal'],'title':s['title']} for s in material['scenes']]
    require(build_materials(corpus,previous_plan,fresh_material,selection) == material, 'frozen_material_changed')
    turns = [t for s in material['scenes'] for t in s['turns']]
    return {'scenes':len(selection), 'turns':len(turns), 'bubbles':sum(len(t['bubbles']) for t in turns),
            'source_authority_valid':True, 'materials_hash':material['materials_hash']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('previous-plan','development-materials','selection','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    old, fresh, selected = (read_private(p) for p in (args.previous_plan,args.development_materials,args.selection))
    corpus=DatasetCorpusStore(old['corpus_root'])
    material=build_materials(corpus,old,fresh,selected)
    output=outside_repository(args.output)
    save_once(output/'materials.private.json',material)
    print(json.dumps(verify_materials(corpus,read_private(output/'materials.private.json'),old,fresh)))


if __name__ == '__main__':
    main()
