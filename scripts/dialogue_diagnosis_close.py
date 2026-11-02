"""Close CR1's native selector reservations after experimental main positions.

No generation. This post-run step preserves sealed execution sources, accepted
snapshots and physical receipts. It is safe to repeat as a read-only check after
the first successful release.
"""
import argparse
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from persona_runtime.archive import fingerprint
from persona_runtime.character_selector import SelectorService
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.config import ConfigStore
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, require
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from scripts import dialogue_diagnosis_real as real
from scripts.dialogue_refactor import offline, NoDispatch


def close_pack(root):
    prepared = real.check_freeze(root)
    real.verify(root)
    real.protect()
    root = Path(root)
    context_hashes = real.read(root / 'contexts-freeze.json')
    before, after = Counter(), Counter()
    proofs = []
    for item in prepared['items']:
        paths = RuntimePaths(Path(item['runtime']))
        store, cfg = Store(paths), ConfigStore(paths)
        view = SimpleNamespace(store=store, config=cfg)
        original = real.owned_turn(view, item, root)
        context_file = root / 'contexts' / (item['key'] + '.json')
        require(real.sha(context_file) == context_hashes[item['key']], 'cr1_context_evidence_changed')
        context = real.read(context_file)
        evidence = DialogueEvidence.load(context['evidence'])
        frame = DialogueFrame.load(context['frame']).validate(evidence)
        CommunicativeSpec.load(context['spec']).validate(evidence, frame)
        with store.connection() as db:
            require(db.execute("SELECT count(*) FROM character_selection_attempts WHERE status='running'").fetchone()[0] == 0,
                    'cr1_native_attempt_still_running')
            for state, count in db.execute('SELECT state,count(*) FROM quota_reservations GROUP BY state'): before[state] += count
        SelectorService(store, cfg, NoDispatch(), None).release(item['turn_id'])
        current = real.owned_turn(view, item, root)
        require({k: current[k] for k in real.base.FROZEN} == {k: original[k] for k in real.base.FROZEN}, 'cr1_cleanup_changed_snapshot')
        with store.connection() as db:
            require(db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0] == 0,
                    'cr1_native_reservation_remains_active')
            for state, count in db.execute('SELECT state,count(*) FROM quota_reservations GROUP BY state'): after[state] += count
        proofs.append({'item': item['key'], 'accepted_snapshots_unchanged': True,
                       'context_hash_unchanged': True, 'native_active_reservations': 0})
    result = {'tool': 'cr1-close-v1', 'native_states_before': dict(before), 'native_states_after': dict(after),
              'items': proofs, 'generation_calls': 0, 'formal_protection_passed': True,
              'note': 'base queued turns are experimental source authority, not fabricated production completed history'}
    file = root / 'native-reservation-cleanup.json'
    if not file.exists(): real.once(file, result)
    return {'closed_items': len(proofs), 'native_active_reservations': 0, 'generation_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=real.OUT / 'real-v1')
    args = parser.parse_args()
    with offline():
        print(close_pack(real.base.confined(args.root)))


if __name__ == '__main__': main()
