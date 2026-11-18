"""Run the approved D1 Pro comparison with frozen history and first receipts."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing, contextmanager
from html import escape
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import ReplySchemaError, validate_reply
from persona_runtime.storage import Store

from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_trial import FirstReceipts
from .en5_decision_contract import STUDY, require
from .en5_decision_packet import baseline_records, build, file_hash, reviewed_inputs


STAGE = STUDY + '-d1'


def approved(root):
    state = read_private(root.parent / 'prompt-review-state.json')
    folder = outside_repository(state['en5_decision_confirmed_requests']).parent
    require(folder.parent == root / 'author-selection', 'd1_selection_scope')
    material = root / 'material.private.json'
    approval = folder / 'author-review.private.json'
    requests = folder / 'requests.private.json'
    reviewed = read_private(approval)
    packet = read_private(material)
    selection = folder / 'submitted.private.json'
    checksum = file_hash(requests)
    require(file_hash(approval) == state['en5_decision_author_review_sha256']
            and checksum == state['en5_decision_confirmed_requests_sha256']
            and checksum == reviewed['frozen_requests_sha256']
            and file_hash(material) == reviewed['material_sha256'] == state['en5_decision_material_sha256']
            and file_hash(selection) == reviewed['selection_file_sha256']
            and reviewed['study'] == packet['study'] == STUDY
            and reviewed['scope'] == 'en5_ling_fixed_16'
            and reviewed['decision'] == 'approved_as_proposed'
            and reviewed['approved_models']['pro'] == 'deepseek-v4-pro', 'd1_author_scope')
    combined = read_private(requests)
    require(combined == reviewed_inputs(packet, read_private(selection), reviewed['material_sha256'])
            and [x['position'] for x in combined['positions']] == reviewed['approved_positions'], 'd1_request_scope')
    return packet, combined, checksum, file_hash(approval)


def use_scope(root):
    _, requests, checksum, approval = approved(root)
    return {'stage': STAGE, 'approved': True, 'author_message': '开始D1',
            'confirmed_requests_sha256': checksum, 'decision_approval_sha256': approval,
            'pro_requests': {r['position']: r['pro_sha256'] for r in requests['positions']}}


def comparison(packet, requests, originals):
    base = {b['position']: b for b in originals}
    rows = []
    dispatched = set()
    for case, row in zip(packet['cases'], requests['positions'], strict=True):
        pos = case['position']
        b = base[pos]
        wire = row['pro']
        key = row['pro_sha256']
        require(row['position'] == pos and b['wire'] == case['original']
                and b['request_sha256'] == row['original'] == fingerprint(b['wire'])
                and key == fingerprint(wire) and wire['model'] == b['wire']['model'] == 'deepseek-v4-pro'
                and wire['messages'][1:] == b['wire']['messages'][1:]
                and wire | {'messages': b['wire']['messages']} == b['wire'], 'd1_comparison_drift')
        if key != row['original']:
            require(key not in dispatched, 'd1_duplicate_pro')
            dispatched.add(key)
        rows.append((pos, b, wire, key))
    require(len(dispatched) == requests['d1_unique_changed'], 'd1_count_drift')
    return rows


def original_outcome(record):
    return {'state': 'completed', 'body': record['body'], 'attempt_id': record['attempt_id'],
            'request_hash': record['request_sha256'], 'body_sha256': record['body_sha256'],
            'origin': 'original_v8'}


async def run(root):
    root = outside_repository(root)
    run_root = root / 'd1'
    run_root.mkdir(exist_ok=True)
    with closing(InstanceLock(run_root / 'run.lock')):
        permit = run_root / 'author-use.private.json'
        require(permit.is_file() and read_private(permit) == use_scope(root), 'd1_dispatch_authorization')
        permit_hash = file_hash(permit)
        packet, requests, checksum, approval = approved(root)
        prior = Path(packet['prior'])
        require(fingerprint(build(prior, packet['upstream'])) == fingerprint(packet), 'd1_source_changed')
        source, originals = baseline_records(prior)
        plan = comparison(packet, requests, originals)
        binding = fingerprint([checksum, approval, permit_hash])
        end = run_root / 'results.private.json'
        if end.is_file():
            results = read_private(end)
            require(results['binding'] == binding, 'd1_results_binding')
            return export(run_root, results, plan, originals)
        paths = RuntimePaths(source / 'home')
        store = Store(paths)
        corpus = DatasetCorpusStore(read_private(source / 'selection.private.json')['corpus_root'])
        credentials = ConfigStore(paths).load()['model']
        by_slot = {pos + '.pro.body': b for pos, b, _, _ in plan}

        @contextmanager
        def window():
            with corpus.connection(write=True), store.connection(write=True):
                yield

        def guard(_):
            require(file_hash(permit) == permit_hash and approved(root)[2:] == (checksum, approval),
                    'd1_authorization_changed')
            require(baseline_records(prior) == (source, originals), 'd1_source_changed')

        def route(slot, stage, model):
            b = by_slot[slot]
            require(stage == 'body' and model == b['model_public']
                    and provider_identity(model) == provider_identity(credentials), 'd1_provider_changed')
            guard_cloud_dispatch(store, b['persona_id'], 'main', model)

        journal = FirstReceipts(run_root, binding, client=GenerationHTTPClient(), guard=guard,
                                authority_window=window, guard_route=route)
        outcomes = {}
        for pos, b, wire, key in plan:
            original = original_outcome(b)
            change = original
            if key != b['request_sha256']:
                r = await journal.send(pos + '.pro.body', 'body', (b['model_public'], wire),
                                       [b['input_sha256'], b['request_sha256'], b['body_sha256']], credentials)
                change = r | {'origin': 'decision_pro', 'body': None}
                raw_body = change.pop('content')
                if r['state'] == 'completed':
                    try:
                        change['body'] = validate_reply(
                            raw_body, 'context_full', b['model_public'].get('_expression_snapshot')).text
                    except (ValueError, TypeError, ReplySchemaError):
                        change.update(state='failed', error_code='body_validation_failed')
                print(pos, change['state'], flush=True)
            outcomes[pos] = {'A': original, 'B': change}
        guard(None)
        with closing(journal.open()) as db:
            for pos, b, wire, key in plan:
                change = outcomes[pos]['B']
                if change['origin'] == 'original_v8' or not change.get('attempt_id'):
                    continue
                stored = db.execute('SELECT * FROM requests WHERE attempt_id=?', (change['attempt_id'],)).fetchone()
                require(stored and json.loads(stored['wire']) == wire and change['request_hash'] == key, 'd1_wire_receipt')
                if stored['raw'] is not None:
                    require(digest(stored['raw']) == stored['raw_hash'] == change['raw_hash'], 'd1_raw_receipt')
                if change['state'] == 'completed':
                    parsed = parse_receipt(stored['raw'], wire.get('stream', False))
                    require(stored['http'] == 200 and parsed['valid'] and parsed['model'] == wire['model']
                            and parsed['usage'] == change['usage']
                            and validate_reply(parsed['body'], 'context_full',
                                               b['model_public'].get('_expression_snapshot')).text == change['body'],
                            'd1_body_receipt')
            physical = dict(db.execute('SELECT state,count(*) FROM requests GROUP BY state'))
        fresh = [r['B'] for r in outcomes.values() if r['B']['origin'] == 'decision_pro']
        actual = [r for r in fresh if r.get('attempt_id')]
        usage = [(r.get('usage') or {}).get('total_tokens') for r in actual]
        counts = {'physical_requests': sum(physical.values()), 'first_receipt_states': physical,
                  'candidate_states': dict(Counter(r['state'] for r in fresh)),
                  'original_replies_reused': len(plan), 'known_tokens': sum(n for n in usage if type(n) is int),
                  'unknown_usage': sum(type(n) is not int for n in usage), 'actual_charge': None}
        results = {'study_id': STAGE, 'binding': binding, 'input_contract_sha256': checksum,
                   'results': outcomes, 'accounting': counts}
        save_once(end, results)
        return export(run_root, results, plan, originals)


def export(root, results, plan, originals):
    mapping_path = root / 'blind-key.private.json'
    if not mapping_path.exists():
        rng = SystemRandom()
        save_once(mapping_path, dict(binding=results['binding'],
                                     labels={pos: dict(zip('甲乙', rng.sample('AB', 2))) for pos, *_ in plan}))
    mapping = read_private(mapping_path)
    require(mapping['binding'] == results['binding'] and set(mapping['labels']) == set(results['results']),
            'd1_blind_binding')
    history = {b['position']: b for b in originals}
    cases = []
    evidence = ['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
                '<title>E.N5 D1 回执</title><h1>D1 身份与首次回执</h1><pre>',
                escape(json.dumps(results['accounting'], ensure_ascii=False, indent=2)), '</pre>']
    for pos, b, _, _ in plan:
        labels = mapping['labels'][pos]
        require(set(labels) == set('甲乙') and set(labels.values()) == set('AB'), 'd1_blind_labels')
        options = {}
        for label, arm in labels.items():
            reply = results['results'][pos][arm]
            options[label] = {k: reply[k] for k in ('body', 'state')}
            title = pos + ' · ' + label + ' · ' + ('原 v8' if arm == 'A' else '人工决策＋Pro')
            evidence.extend(('<details><summary>', escape(title), '</summary><pre>',
                             escape(json.dumps({k:v for k,v in reply.items() if k!='body'},
                                               ensure_ascii=False, indent=2)), '</pre></details>'))
        before = []
        for entry in b['history']:
            prior = history[f'{pos.split("-")[0]}-{entry["turn"]:02d}']
            before.append({'turn': entry['turn'], 'user': prior['wire']['messages'][-1]['content'],
                           'reply': prior['body']})
        cases.append({'position': pos, 'user': b['wire']['messages'][-1]['content'],
                      'before': before, 'options': options})
    view = {'study_id': STAGE, 'input_contract_sha256': results['input_contract_sha256'],
            'results_sha256': file_hash(root / 'results.private.json'),
            'blind_key_sha256': file_hash(mapping_path), 'cases': cases}
    template = Path(__file__).with_name('en5_style_review.html').read_text(encoding='utf-8')
    page = template.replace('接话修复16位审阅意见.json', '接话决策D1审阅意见.json')
    page = page.replace('角色的接话方式盲审', 'E.N5 D1 · 角色的接话方式盲审')
    target = root / 'review/index.html'
    save_text_once(target, page.replace('@@DATA@@', json.dumps(view, ensure_ascii=False).replace('<', r'\u003c')))
    save_text_once(target.with_name('evidence.html'), ''.join(evidence))
    return {'review': str(target), **results['accounting']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    print(json.dumps(asyncio.run(run(parser.parse_args().root)), ensure_ascii=False))
