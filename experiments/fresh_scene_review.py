"""Freeze scene-level blind assignments and publish the two offline reading views."""
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import outside_repository
from .body_repair_review import GROUPS, LABELS, body_record
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once


KEY_FILE = 'blind-key.private.json'
KEY_VERSION = 'fresh-scene-blind-key-v1'


def require(condition, code):
    if not condition:
        raise CorpusError('fresh_scene_review_' + code)


def freeze_key(root, scene_ids):
    """Assign one side per comparison and scene, before the manifest and calls exist."""
    root = outside_repository(root)
    ids = list(scene_ids)
    require(ids and all(isinstance(s, str) and s for s in ids) and len(ids) == len(set(ids)),
            'invalid_scene_ids')
    path = root / KEY_FILE
    if not path.exists():
        require(not any((root / filename).exists() for filename in
                        ('manifest.private.json', 'results.private.json', 'ledger')), 'original_key_required')
        random = SystemRandom()
        mapping = {s: {} for s in ids}
        for group in GROUPS:
            sides = list(LABELS) * (len(ids) // 2)
            if len(ids) % 2:
                sides.append(random.choice(tuple(LABELS)))
            random.shuffle(sides)
            for scene_id, side in zip(ids, sides):
                mapping[scene_id][group] = side
        save_once(path, {'version': KEY_VERSION, 'scenes': mapping})
    key = read_private(path)
    require(key['version'] == KEY_VERSION and list(key['scenes']) == ids
            and all(set(group) == set(GROUPS) and all(s in LABELS for s in group.values())
                    for group in key['scenes'].values()), 'invalid_blind_key')
    return digest(path.read_bytes())


def paired(first, second, candidate_side):
    sides = (first, second) if candidate_side == 'left' else (second, first)
    if any(entry['state'] != 'completed' for entry in sides):
        status = 'incomplete'
    elif first['body'] == second['body']:
        status = 'identical'
    else:
        status = 'different'
    return {'status': status, 'sides': [{'text': entry['body'], 'state': entry['state']} for entry in sides]}


def export(root):
    """Use the frozen scene order and each branch's first presentation, including gaps."""
    root = outside_repository(root)
    manifest_path, results_path = root / 'manifest.private.json', root / 'results.private.json'
    manifest, results = read_private(manifest_path), read_private(results_path)
    manifest_sha = digest(manifest_path.read_bytes())
    require(manifest['version'] == 'fresh-private-dialogue-v1'
            and results['study_id'] == manifest['study_id']
            and results['manifest_sha256'] == manifest_sha, 'unbound_results')
    key_sha = freeze_key(root, [scene['id'] for scene in manifest['scenes']])
    require(key_sha == manifest['blind_key_sha256'], 'blind_key_changed')
    sides = read_private(root / KEY_FILE)['scenes']
    cases = iter(results['cases'])
    scenes = []
    comparisons = {g: dict(different=0, identical=0, incomplete=0) for g in GROUPS}
    for frozen in manifest['scenes']:
        scene_id = frozen['id']
        turns = []
        require(frozen['turns'], 'empty_scene')
        for number, source in enumerate(frozen['turns'], start=1):
            case = next(cases, None)
            require(case and (case['scene_id'], case['turn'], case['input'], case['title'])
                    == (scene_id, number, source['text'], frozen['title']), 'case_source_changed')
            require(set(case['bodies']) == {'B', 'S', 'R'}, 'missing_branch')
            bodies = {branch: body_record(entry) for branch, entry in case['bodies'].items()}
            pairs = {g: paired(bodies['R'], bodies[g], sides[scene_id][g]) for g in GROUPS}
            for group, pair in pairs.items():
                comparisons[group][pair['status']] += 1
            turns.append({'turn': number, 'input': case['input'], 'pairs': pairs})
        scenes.append({'id': scene_id, 'title': frozen['title'], 'turns': turns})
    require(next(cases, None) is None, 'unexpected_case')
    payload = {'study_id': results['study_id'], 'manifest_sha256': manifest_sha,
               'result_set_hash': digest(results_path.read_bytes()), 'blind_key_sha256': key_sha,
               'groups': GROUPS, 'scenes': scenes, 'comparisons': comparisons}
    template = Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    content = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c')
    output = root / 'review' / 'index.html'
    save_text_once(output, template.replace('@@SCENE_DATA@@', content))
    return output
