"""Offline review of four private-chat routes. No model or tool dispatch."""
import argparse
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.character_embedding import file_hash
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


ARMS = ('installed_pcar', 'candidate', 'direct_roleplay', 'native_maibot')
LABELS = ('甲', '乙', '丙', '丁')
KEY = 'blind-key.private.json'
STUDY = 'study.private.json'
CONTRACT = 'input-contract.private.json'
APPROVAL = 'author-approval.private.json'
ISSUES = ('助手式邀约', '擅自代办', '答非所问', '单泡超过 75 字符')


def require(ok, code):
    if not ok:
        raise ValueError(code)


def anonymous_order(root, study):
    identity = fingerprint(study)
    target = root / KEY
    episodes = [e['id'] for e in study['episodes']]
    if not target.is_file():
        require(not any((root / name).exists() for name in ('results.private.json','review/index.html')), 'original_blind_key_missing')
        random = SystemRandom()
        base = random.sample(ARMS, len(ARMS))
        offsets = list(range(len(ARMS)))
        permuted = {}
        for index, episode in enumerate(episodes):
            if index % len(ARMS) == 0:
                random.shuffle(offsets)
            offset = offsets[index % len(ARMS)]
            permuted[episode] = base[offset:] + base[:offset]
        save_once(target, {'study': identity, 'episodes': permuted})
    key = read_private(target)
    require(key['study'] == identity and list(key['episodes']) == episodes, 'blind_key_scope_changed')
    require(all(sorted(order) == sorted(ARMS) for order in key['episodes'].values()), 'blind_key_invalid')
    return key['episodes']


def make_packet(root, study, results):
    study_hash = file_hash(root / STUDY)
    require(results['study_sha256'] == study_hash, 'study_changed')
    contract, approval = (read_private(root / name) for name in (CONTRACT, APPROVAL))
    contract_hash = file_hash(root / CONTRACT)
    require(approval['decision'] == 'approved' and approval['input_contract_sha256'] == contract_hash
            and results['input_contract_sha256'] == contract_hash and contract['study_sha256'] == study_hash, 'approval_scope_changed')
    require(set(contract['recipients']) == set(ARMS) and set(contract['assembly_sha256']) == set(ARMS), 'input_contract_incomplete')
    require(contract['writer_controls'] == results['writer_controls'], 'writer_controls_changed')
    order = anonymous_order(root, study)
    require(results['blind_key_sha256'] == file_hash(root / KEY), 'blind_key_changed')
    require(results['episodes'].keys() == order.keys(), 'episode_coverage')
    result_episodes = []
    receipt_ids = set()
    for episode in study['episodes']:
        eid = episode['id']
        rows = results['episodes'][eid]
        slots = episode['positions']
        require(len(rows) == len(slots), 'turn_coverage')
        turns = []
        previous = {arm: [] for arm in ARMS}
        for index, (slot, row) in enumerate(zip(slots, rows), 1):
            require(row['source_turn'] == slot['turn_id'], 'source_turn_changed')
            require([digest(text.encode('utf-8')) for text in row['inputs']] == [m['sha256'] for m in slot['input']], 'user_input_changed')
            require(row.get('material_sha256') == episode.get('material_sha256'), 'shared_material_changed')
            require(set(row['arms']) == set(ARMS), 'arm_coverage')
            options = []
            for label, arm in zip(LABELS, order[eid]):
                result = row['arms'][arm]
                require(result['writer_controls'] == results['writer_controls'], 'writer_controls_changed')
                require(result['starting_context_sha256'] == contract['starting_context_sha256'][eid]
                        and result['assembly_sha256'] == contract['assembly_sha256'][arm], 'input_assembly_changed')
                require(result['previous_visible_sha256'] == fingerprint(previous[arm]), 'transcript_chain_changed')
                state, bubbles = result['state'], result['visible']
                require(state in ('completed', 'wait', 'interrupted') and (state != 'completed' or bubbles)
                        and (state != 'wait' or not bubbles), 'presentation_state_invalid')
                text_parts = []
                for bubble in bubbles:
                    text = bubble['text']
                    receipt = bubble['receipt_id']
                    phase = bubble['phase']
                    require(receipt and receipt not in receipt_ids and text and phase in ('search_notice','main')
                            and bubble['text_sha256'] == digest(text.encode('utf-8')), 'presentation_receipt_invalid')
                    receipt_ids.add(receipt)
                    text_parts.append({'phase':phase,'text':text})
                previous[arm].append({'inputs':row['inputs'],'visible':text_parts})
                options.append({'label':label,'state':state,'bubbles':text_parts})
            turns.append({'number':index,'inputs':row['inputs'],'options':options})
        result_episodes.append({'id':eid,'kind':episode['kind'],'turns':turns})
    return {'study_sha256':study_hash,'results_sha256':file_hash(root/'results.private.json'),
            'issues':list(ISSUES),'episodes':result_episodes}


def render(root):
    root = outside_repository(root)
    study = read_private(root / STUDY)
    if (root / 'results.private.json').is_file():
        packet = make_packet(root, study, read_private(root / 'results.private.json'))
        destination = root / 'review/index.html'
    else:
        packet = {'episodes':[{'id':e['id'],'kind':e['kind'],'count':len(e['positions'])}
                              for e in study['episodes']]}
        destination = root / 'review/preparation.html'
    template = Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    payload = json.dumps(packet, ensure_ascii=False).replace('<', r'\u003c')
    save_text_once(destination, template.replace('@@DATA@@', payload))
    return destination


def verify_scores(root, score_path):
    root = outside_repository(root)
    study, results = (read_private(root / name) for name in (STUDY, 'results.private.json'))
    packet = make_packet(root, study, results)
    score = read_private(score_path)
    for key in ('study_sha256','results_sha256'):
        require(score[key] == packet[key], 'score_version_changed')
    episodes = score['episodes']
    require(set(episodes) == {e['id'] for e in packet['episodes']}, 'score_episode_coverage')
    order = anonymous_order(root, study)
    mapped = {}
    for episode, labels in episodes.items():
        require(set(labels) == set(LABELS), 'score_arm_coverage')
        for rating in labels.values():
            require(type(rating['naturalness']) is int and 1 <= rating['naturalness'] <= 5
                    and len(set(rating['issues'])) == len(rating['issues'])
                    and set(rating['issues']) <= set(ISSUES), 'score_invalid')
        mapped[episode] = dict(zip(order[episode], (labels[label] for label in LABELS)))
    score_hash = file_hash(score_path)
    target = root / f'author-scores-{score_hash}.private.json'
    save_once(target, {'score_sha256':score_hash,'results_sha256':score['results_sha256'],
                       'episodes':mapped,'decision':'awaiting_author_signoff'})
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--scores', type=Path)
    args = parser.parse_args()
    print(verify_scores(args.root, args.scores) if args.scores else render(args.root))
