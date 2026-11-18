"""Prepare or import the private E.N5 decision review. Both commands work offline."""
import argparse
from contextlib import closing
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.en5_display import read_receipts
from persona_runtime.references import prepare as prepare_references
from persona_runtime.turn_input import load_turn_input, render_input

from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_repair_feedback import save_raw, unpack
from .en5_decision_contract import (ACTIONS, DEADLINE_SECONDS, STATES, STUDY, UNDETERMINED,
                                    flash_base, flash_wire, persona_projection, pro_wire, require,
                                    target_list, validate_decision)
from .en5_decision_sources import COMMIT, catalog, material_text
from .en5_experience_setup import binding_at
from .en5_style_packet import SELECTED, assemble, file_hash, inputs_page


VERSION = 'en5-decision-review-v1'
FORM = 'en5-decision-author-selection-v1'
PAGE = Path(__file__).with_suffix('.html')


def baseline_records(prior):
    old = read_private(prior / 'input-contract.private.json')
    source = outside_repository(old['source'])
    records, replayed = assemble(source, prior)
    replayed['preview_sha256'] = digest(inputs_page(replayed).encode())
    require(replayed == old and records == read_private(prior / 'original-baselines.private.json'),
            'baseline_replay_changed')
    return source, records


def visible_turns(db, paths, binding, report, records):
    """Recheck the physical display receipts and map actual event IDs to the frozen turns."""
    by_id = {r['turn_id']: r for r in records}
    turns = {}
    for segment in report['segments']:
        sid = segment['session_id']
        shown = read_receipts(paths, binding['study_id'], binding['persona_id'], sid)
        for actual in segment['turns']:
            tid = actual['turn_id']
            original = by_id[tid]
            row = db.execute('SELECT * FROM turns WHERE id=?', (tid,)).fetchone()
            receipt = shown.get(tid, {}).get('main')
            require(row is not None and receipt is not None, 'visible_source_missing')
            turn = dict(row)
            at = datetime.fromisoformat(receipt['first_visible_at']).timestamp()
            require((turn['persona_id'], turn['session_id'], turn['reply_event_id']) ==
                    (binding['persona_id'], sid, receipt['source_id'])
                    and receipt['body_sha256'] == original['body_sha256']
                    and at == actual['main_visible_epoch'], 'visible_receipt_changed')
            members = load_turn_input(db, turn)
            require(render_input(members, bool(turn['input_batch_id'])) ==
                    original['wire']['messages'][-1]['content'], 'original_members_changed')
            turns[tid] = {'turn': turn, 'members': members, 'shown': at, 'accepted': actual['accepted_epoch'],
                          'reply': {'id': turn['reply_event_id'], 'role': 'assistant', 'text': original['body']}}
    require(len(turns) == len(by_id) == 30, 'original_count')
    return turns


def position_targets(db, record, turns):
    tid = record['turn_id']
    current = turns[tid]
    turn = current['turn']
    messages = record['wire']['messages']
    contexts = db.execute("""SELECT c.details_json, m.manifest_json FROM context_uses c
        JOIN attempts a ON a.id=c.attempt_id JOIN context_manifests m ON m.attempt_id=a.id
        WHERE c.turn_id=? AND a.status='completed' AND m.task_type='dialogue'""", (tid,)).fetchall()
    require(len(contexts) == 1 and json.loads(contexts[0]['manifest_json'])['input_snapshot_hash'] == record['input_sha256'],
            'original_context_binding')
    details = json.loads(contexts[0]['details_json'])
    entries = []

    def add(event, origin, source_turn, location):
        row = db.execute('SELECT seq,persona_id,session_id,role,text FROM events WHERE id=?',
                         (event['id'],)).fetchone()
        require(row and {'persona': 'assistant', 'user': 'user'}.get(row['role']) == event['role']
                and row['text'] == event['text'], 'event_changed')
        displayed = (event['role'] == 'user' or
                     (source_turn['reply']['id'] == event['id'] and source_turn['shown'] <= current['accepted']))
        entries.append({'event_id': event['id'], 'sequence': row['seq'], 'persona_id': row['persona_id'],
                        'session_id': row['session_id'], 'role': event['role'], 'text': row['text'],
                        'source': origin, 'pro_target': location, 'displayed': displayed})

    reference_block, manifest = prepare_references(db, turn)
    require(manifest == details['references'], 'reference_binding_changed')
    if reference_block:
        # This is the validated, accepted reference projection that actually went to v8.
        require(messages[0]['role'] == 'system' and reference_block in messages[0]['content'], 'reference_block_missing')
        reference_data = json.loads(reference_block.split('\n', 2)[2])
        for group in reference_data['turns']:
            require(group['turn_id'] in turns, 'reference_outside_study')
            source = turns[group['turn_id']]
            for e in [*group['members'], *group['replies']]:
                if not any(ref['kind'] == 'turn' or ref['id'] == e['id'] for ref in group['targets']):
                    continue
                role = e.get('role', 'assistant')
                add(e | {'role': role}, 'reference', source,
                    {'message_index': 0, 'reference_event': e['id'], 'role': role})

    groups = details['history_groups']
    recent = [g for g in groups if g['kind'] == 'recent']
    require(len(recent) <= 2 and len(messages) == 2 + 2 * len(groups), 'history_layout')
    for offset, group in enumerate(groups):
        if group['kind'] != 'recent':
            continue
        source = turns[group['turn_id']]
        index = 1 + 2 * offset
        members = source['members']
        require(messages[index] == {'role': 'user', 'content': render_input(members, bool(source['turn']['input_batch_id']))}
                and messages[index + 1] == {'role': 'assistant', 'content': source['reply']['text']},
                'recent_history_changed')
        for n, event in enumerate(members, 1):
            add(event, 'recent', source, {'message_index': index, 'role': 'user', 'member': n})
        add(source['reply'], 'recent', source, {'message_index': index + 1, 'role': 'assistant'})
    for n, event in enumerate(current['members'], 1):
        add(event, 'current', current, {'message_index': len(messages) - 1, 'role': 'user', 'member': n})
    return target_list(turn['persona_id'], turn['session_id'], entries)


def build(prior, upstream):
    prior, upstream = outside_repository(prior), Path(upstream).resolve()
    source, records = baseline_records(prior)
    paths, binding = binding_at(source)
    report = read_private(paths.home / 'exports/en5/report.private.json')
    selected = {f'{part}-{n:02d}' for part, row in enumerate(SELECTED, 1) for n in row}
    cases = []
    with closing(sqlite3.connect((paths.home/'data/database/runtime.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        turns = visible_turns(db, paths, binding, report, records)
        for record in records:
            if record['position'] not in selected:
                continue
            turn = turns[record['turn_id']]['turn']
            profile = json.loads(turn['profile_snapshot'])
            require(fingerprint(profile) == record['profile_sha256'] and
                    all(profile[k]['persona_id'] == turn['persona_id'] for k in ('core', 'expression')), 'persona_changed')
            persona = persona_projection(profile)
            targets = position_targets(db, record, turns)
            case = {'position': record['position'], 'turn_id': turn['id'], 'session_id': turn['session_id'],
                    'character_version': record['character_version'], 'profile_sha256': record['profile_sha256'],
                    'source_attempt': record['attempt_id'], 'original_sha256': record['request_sha256'],
                    'body_sha256': record['body_sha256'], 'original': record['wire'], 'body': record['body'],
                    'targets': targets, 'flash_base': flash_base(record['model_public'], persona, targets),
                    'capacity': record['model_public']['max_input_chars']}
            case['scope'] = fingerprint([STUDY, turn['persona_id'], turn['id'], turn['session_id'],
                                         case['character_version'], case['original_sha256'], targets])
            cases.append(case)
    require(len(cases) == len(selected) == 16, 'position_count')
    evidence = [{k: r[k] for k in ('position', 'turn_id', 'input_sha256', 'request_sha256', 'body_sha256',
                                    'attempt_id')} for r in records]
    code = [Path(__file__), PAGE, Path(__file__).with_name('en5_decision_sources.py'),
            Path(__file__).with_name('en5_decision_contract.py')]
    return {'version': VERSION, 'study': STUDY, 'prior': str(prior), 'upstream': str(upstream),
            'commit': COMMIT, 'sources': catalog(upstream), 'baseline_audit': evidence, 'cases': cases,
            'prior_contract_sha256': file_hash(prior/'input-contract.private.json'),
            'desktop_deadline_seconds': DEADLINE_SECONDS, 'actions': ACTIONS, 'states': STATES,
            'undetermined': UNDETERMINED, 'official_endpoint': records[0]['model_public']['base_url'],
            'implementation': {path.name: digest(path.read_bytes().replace(b'\r\n', b'\n')) for path in code}}


def prepare(prior, upstream, root):
    root, prior = outside_repository(root), outside_repository(prior)
    require(not root.is_relative_to(prior) and not prior.is_relative_to(root), 'separate_destination')
    packet = build(prior, upstream)
    checksum = save_once(root/'material.private.json', packet)
    page = review_page(packet, checksum)
    save_text_once(root/'index.html', page)
    return checksum


def review_page(packet, checksum):
    public = {k: packet[k] for k in ('study', 'sources', 'cases', 'commit', 'actions', 'states',
                                      'undetermined', 'official_endpoint')}
    public.update(binding=checksum, form=FORM)
    data = json.dumps(public, ensure_ascii=False).replace('<', r'\u003c')
    return PAGE.read_text(encoding='utf-8').replace('@@DATA@@', data)


def reviewed_inputs(packet, form, checksum):
    require(isinstance(form, dict) and set(form) == {'version', 'study', 'binding', 'kind', 'material',
                                                     'decisions', 'previews'} and form['kind'] == 'selection'
            and form['version'] == FORM and form['study'] == STUDY and form['binding'] == checksum, 'export_binding')
    require(isinstance(form['material'], dict) and set(form['material']) == {'flash', 'pro'}, 'material_fields')
    notes = {}
    pending = {}
    for stage in ('flash', 'pro'):
        text, slots = material_text(packet['sources'], stage, form['material'][stage])
        notes[stage] = text
        pending[stage] = {'text_supplied': bool(text.strip()), 'unbound_slots': slots}
    cases = {p['position']: p for p in packet['cases']}
    require(isinstance(form['decisions'], dict) and set(form['decisions']) == set(cases) and
            isinstance(form['previews'], dict) and set(form['previews']) == set(cases), 'position_set')
    rows = []
    for pos, case in cases.items():
        choice = validate_decision(form['decisions'][pos], case['targets'])
        flash = flash_wire(case['flash_base'], notes['flash'])
        pro = pro_wire(case['original'], notes['pro'], choice, case['targets'])
        previews = form['previews'][pos]
        require(isinstance(previews, dict) and set(previews) == {'flash', 'pro'}, 'preview_fields')
        for stage, wire in (('flash', flash), ('pro', pro)):
            supplied = previews[stage]
            require((supplied == fingerprint(wire)) if isinstance(supplied, str) else (supplied == wire),
                    'preview_changed')
            require(sum(len(m['content']) for m in wire['messages']) <= min(case['capacity'], 60000), 'request_capacity')
        rows.append({'position': pos, 'scope': case['scope'], 'decision': choice, 'original': case['original_sha256'],
                     'flash': flash, 'flash_sha256': fingerprint(flash), 'pro': pro, 'pro_sha256': fingerprint(pro)})
    changed = {r['pro_sha256'] for r in rows if r['pro_sha256'] != r['original']}
    reused = sum(r['pro_sha256'] == r['original'] for r in rows)
    return {'study': STUDY, 'material_sha256': checksum, 'author_text': notes, 'material_status': pending,
            'd1_unique_changed': len(changed), 'd1_reused_original': reused, 'positions': rows,
            'use': 'awaiting_specific_input_review'}


def import_selection(root, feedback):
    root = outside_repository(root)
    path = root/'material.private.json'
    packet = read_private(path)
    checksum = file_hash(path)
    rebuilt = build(packet['prior'], packet['upstream'])
    require(fingerprint(packet) == fingerprint(rebuilt)
            and (root/'index.html').read_bytes() == review_page(packet, checksum).encode(), 'packet_changed')
    raw = outside_repository(feedback).read_bytes()
    final = reviewed_inputs(packet, unpack(raw), checksum)
    folder = root/'author-selection'/digest(raw)
    folder.mkdir(parents=True, exist_ok=True)
    save_raw(folder/'submitted.private.json', raw)
    save_once(folder/'requests.private.json', final)
    page = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
            '<title>作者选择后的完整请求</title><style>body{max-width:62rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
            '<h1>作者选择后的完整请求</h1><p>当前用途为具体输入审阅。D1 使用人工判断与 Pro。D2 待 D1 正文审阅后接续。</p>',
            f'<p>D1 共 {final["d1_unique_changed"]} 份不同的新 Pro 请求，{final["d1_reused_original"]} 个位置直接复用原 v8。</p>']
    for stage, status in final['material_status'].items():
        state = '已提供' if status['text_supplied'] else '待补充'
        slots = '、'.join(status['unbound_slots']) or '无'
        page.append(f'<p>{stage}　文字{state}　待绑定插槽　{escape(slots)}</p>')
    for row in final['positions']:
        page.append(f'<h2>{escape(row["position"])}</h2>')
        page.append('<p>作者判断</p><pre>'+escape(json.dumps(row['decision'],ensure_ascii=False,indent=2))+'</pre>')
        for stage in ('pro', 'flash'):
            page.append(f'<details><summary>{stage.upper()} 完整请求</summary><pre>'
                        + escape(json.dumps(row[stage], ensure_ascii=False, indent=2)) + '</pre></details>')
    save_text_once(folder/'inputs.html', ''.join(page) + '</html>')
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ops = parser.add_subparsers(dest='operation', required=True)
    prepare_parser = ops.add_parser('prepare')
    for name in ('prior', 'upstream', 'root'):
        prepare_parser.add_argument('--' + name, required=True, type=Path)
    import_parser = ops.add_parser('import-selection')
    for name in ('root', 'feedback'):
        import_parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    if args.operation == 'prepare':
        checksum = prepare(args.prior, args.upstream, args.root)
        print(f'{len(SELECTED[0]) + len(SELECTED[1])} positions / 30 baselines / {checksum}')
    else:
        print(import_selection(args.root, args.feedback))


if __name__ == '__main__':
    main()
