"""Freeze independently grouped conversations for the repair study, using the original desktop boundaries."""
import argparse
from difflib import SequenceMatcher
import json
from pathlib import Path

from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.ingress import ROLLING_PARAMS
from .corpus_reply_study import read_private, save_once
from .en3_holdout_material import (SPLIT, excluded_inputs, load_fragment, require,
                                   scene_window, verify_materials as verify_prior_holdout)
from .fresh_scene_material import text_key


VERSION = 'en3-ling-repair-expanded-holdout-v2'
SCENE_COUNT = 6
# The original corpus grouping already includes near-duplicate fragments. For this
# comparison, screen short utterances against the actual inputs previously used.
SHORT_NEAR = {'min_chars': 8, 'min_similarity': .90}


def exclude_reviewed_holdout(groups, used, prior):
    require(prior['split'] == SPLIT and len(prior['scenes']) == 2 and
            prior['materials_hash'] == fingerprint({k:v for k,v in prior.items() if k != 'materials_hash'}),
            'reviewed_holdout_changed')
    for scene in prior['scenes']:
        require(scene['group_id'] not in groups, 'reviewed_group_overlap')
        groups.add(scene['group_id'])
        for turn in scene['turns']:
            used.update(text_key(b['text']) for b in turn['bubbles'])


def check_short_near_duplicates(scene, prior_texts):
    seen = set(prior_texts)
    for turn in scene['turns']:
        for bubble in turn['bubbles']:
            key = text_key(bubble['text'])
            for other in seen:
                shorter, longer = sorted((len(key), len(other)))
                if shorter < SHORT_NEAR['min_chars'] or 2*shorter/(shorter+longer) < SHORT_NEAR['min_similarity']:
                    continue
                similarity = SequenceMatcher(None, key, other, autojunk=False).ratio()
                require(similarity < SHORT_NEAR['min_similarity'], 'near_duplicate_input')
            seen.add(key)


def build_materials(corpus, previous_plan, fresh, prior, selection):
    require(len(selection) == SCENE_COUNT, 'expanded_scene_count')
    verify_prior_holdout(corpus, prior, previous_plan, fresh)
    with corpus.connection() as db:
        db.execute('BEGIN')
        view = corpus._view_status(db, previous_plan['view_id'])
        require(view['payload_hash'] == previous_plan['view_payload_hash'] == prior['view_payload_hash']
                and view['source_proof_hash'] == prior['source_proof_hash'], 'expanded_source_changed')
        groups, used = excluded_inputs(db, view, previous_plan, fresh)
        exclude_reviewed_holdout(groups, used, prior)
        excluded_count = len(groups)
        scenes = []
        for index, spec in enumerate(selection, 1):
            fragment, rows = load_fragment(db, view, spec['first_ordinal'])
            require(fragment['group_id'] not in groups, 'expanded_group_reused')
            used_before = used.copy()
            scene = scene_window(fragment, rows, spec, index, used)
            check_short_near_duplicates(scene, used_before)
            scenes.append(scene)
            groups.add(fragment['group_id'])
        proof = db.execute('SELECT source_proof_json FROM corpus_dataset_versions WHERE id=?', (view['id'],)).fetchone()
    packet = {'version': VERSION, 'split': SPLIT, 'view_id': view['id'], 'view_payload_hash': view['payload_hash'],
              'original_parse_id': view['parse_id'], 'source_proof_hash': view['source_proof_hash'],
              'source_authority': json.loads(proof[0]), 'previous_positions_hash': fresh['previous_positions_hash'],
              'dev_materials_hash': fresh['materials_hash'], 'prior_holdout_materials_hash': prior['materials_hash'],
              'excluded_prior_groups': excluded_count, 'short_near_duplicate_policy': dict(SHORT_NEAR),
              'boundary_policy': dict(ROLLING_PARAMS), 'scenes': scenes}
    return packet | {'materials_hash':fingerprint(packet)}


def verify_materials(corpus, material, previous_plan, fresh_material, prior_holdout):
    selection = [{k:s[k] for k in ('first_ordinal','title')} for s in material['scenes']]
    require(build_materials(corpus, previous_plan, fresh_material, prior_holdout, selection) == material,
            'expanded_material_changed')
    gaps = [g for s in material['scenes'] for g in s['omitted_counterpart_turns']]
    turns = [t for s in material['scenes'] for t in s['turns']]
    return {'scenes': len(selection), 'turns': len(turns), 'bubbles': sum(len(t['bubbles']) for t in turns),
            'gaps_with_peer_text': sum('text' in g['kinds'] for g in gaps),
            'gaps_with_peer_emoji_only':sum(bool(g['kinds']) and 'text' not in g['kinds'] for g in gaps),
            'gaps_without_peer':sum(not g['kinds'] for g in gaps),
            'source_authority_valid':True, 'materials_hash':material['materials_hash']}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ('previous-plan','development-materials','prior-holdout','selection','output'):
        cli.add_argument('--' + name, required=True, type=Path)
    args = cli.parse_args()
    old, fresh, prior, selection = map(read_private,
        (args.previous_plan, args.development_materials, args.prior_holdout, args.selection))
    corpus = DatasetCorpusStore(old['corpus_root'])
    path = outside_repository(args.output) / 'materials.private.json'
    material = build_materials(corpus, old, fresh, prior, selection)
    save_once(path, material)
    print(json.dumps(verify_materials(corpus, read_private(path), old, fresh, prior)))


if __name__ == '__main__':
    main()
