"""Apply an explicitly approved three-field rewrite to a fresh private trial only.

This helper has no generation entry point. Preparation and the chat window reuse
companion_trial; all approved text and application evidence stay inside the
disposable copy, whose existing cleanup removes them together.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments import companion_trial as trial
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService, head as character_head
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.profile import ProfileService, head as profile_head
from persona_runtime.storage import Store

VERSION = 'companion-expression-candidate-v1'
DRAFT_VERSION = 'companion-expression-guidance-draft-v1'
PATHS = ('core.core_traits[0]', 'expression.style', 'character.social.public_image')
KINDS = ('core', 'expression', 'relationship', 'character')
RUNTIME_DIRECTORY = 'runtime/companion-expression-candidate'


@contextmanager
def _offline_preparation():
    from persona_runtime.chat import ChatCompletionsClient
    with patch('socket.socket.connect', side_effect=RuntimeErrorBase('expression_prepare_network_blocked')), \
            patch('socket.create_connection', side_effect=RuntimeErrorBase('expression_prepare_network_blocked')), \
            patch('asyncio.BaseEventLoop.create_connection', side_effect=RuntimeErrorBase('expression_prepare_network_blocked')), \
            patch.object(ChatCompletionsClient, 'complete', side_effect=RuntimeErrorBase('expression_prepare_generation_blocked')):
        yield


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _copy_marker(home, source_home):
    home = safe(home, Path(source_home).resolve() / 'tmp/private-acceptance')
    if home.parent != Path(source_home).resolve() / 'tmp/private-acceptance':
        raise RuntimeErrorBase('expression_copy_scope_mismatch')
    marker = json.loads((home / 'private-sandbox.json').read_text(encoding='utf-8'))
    if Path(marker['formal_home']).resolve() != Path(source_home).resolve():
        raise RuntimeErrorBase('expression_copy_scope_mismatch')
    return home, marker


def validate_approval(value):
    approval = value.get('approval') or {}
    if (value.get('version') != DRAFT_VERSION or value.get('status') != 'approved_not_applied'
            or not all(isinstance(approval.get(k), str) and approval[k].strip()
                       for k in ('at', 'user_answer', 'scope'))):
        raise RuntimeErrorBase('expression_rewrite_not_approved')
    rows = value.get('replacements')
    if (not isinstance(rows, list) or len(rows) != len(PATHS)
            or {r.get('path') for r in rows if isinstance(r, dict)} != set(PATHS)):
        raise RuntimeErrorBase('expression_rewrite_paths_invalid')
    for row in rows:
        if (not all(isinstance(row.get(k), str) and row[k].strip() for k in ('before', 'after'))
                or row['before'] == row['after']):
            raise RuntimeErrorBase('expression_rewrite_text_invalid')
    hashes = value.get('source_content_hashes')
    if not isinstance(hashes, dict) or set(hashes) != set(KINDS):
        raise RuntimeErrorBase('expression_source_binding_missing')
    if not isinstance(value.get('source_binding'), dict):
        raise RuntimeErrorBase('expression_source_binding_missing')
    return value


def read_approval(path, source_home):
    base = Path(source_home).resolve() / 'tmp/private-acceptance'
    path = safe(path, base)
    relative = path.relative_to(base)
    if len(relative.parts) < 3:
        raise RuntimeErrorBase('expression_patch_scope_invalid')
    home, marker = _copy_marker(base / relative.parts[0], source_home)
    raw = path.read_bytes()
    value = validate_approval(json.loads(raw.decode('utf-8')))
    return raw, value, marker


def _versions(conn, pid):
    result = {k: profile_head(conn, pid, k) for k in ('core', 'expression', 'relationship', 'state')}
    result['character'] = character_head(conn, pid)
    return result


def _content_hashes(versions):
    return {k: fingerprint(v['data']) for k, v in versions.items()}


def _empty_history(conn):
    # Empty sessions can be created by policy setup; prior content/calls cannot.
    tables = ('events', 'turns', 'attempts', 'input_batches', 'memories', 'summaries',
              'character_selection_attempts', 'quota_reservations')
    counts = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
    if any(counts.values()):
        raise RuntimeErrorBase('expression_candidate_requires_fresh_history')
    return counts


def replaced_data(versions, approved):
    if {k: fingerprint(versions[k]['data']) for k in KINDS} != approved['source_content_hashes']:
        raise RuntimeErrorBase('expression_source_content_changed')
    result = {k: deepcopy(v['data']) for k, v in versions.items()}
    for row in approved['replacements']:
        if row['path'] == PATHS[0]:
            target, key = result['core']['core_traits'], 0
        elif row['path'] == PATHS[1]:
            target, key = result['expression'], 'style'
        else:
            target, key = result['character']['social'], 'public_image'
        if target[key] != row['before']:
            raise RuntimeErrorBase('expression_before_mismatch')
        target[key] = row['after']
    return result


def apply_to_fresh_copy(root, raw, approved, source_marker):
    """Used only after existing prepare has registered the new cleanup home."""
    approved = validate_approval(approved)
    if json.loads(raw.decode('utf-8')) != approved:
        raise RuntimeErrorBase('expression_approval_bytes_mismatch')
    root = Path(root).resolve()
    manifest = trial.load_trial(root)
    if (not manifest.get('private') or manifest.get('purpose') != trial.CANDIDATE_PURPOSE
            or set(manifest['arms']) != {'C'} or manifest['arms']['C']['mode'] != trial.STRUCTURAL_MODE):
        raise RuntimeErrorBase('expression_candidate_scope_invalid')
    arm = manifest['arms']['C']
    home, marker = _copy_marker(arm['home'], manifest['source_home'])
    if (marker['persona_id'] != arm['persona_id']
            or marker['private_id'] != source_marker['private_id']):
        raise RuntimeErrorBase('expression_candidate_role_mismatch')
    private_root = safe(home / RUNTIME_DIRECTORY, home)
    if private_root.exists():
        raise RuntimeErrorBase('expression_candidate_already_started')
    manifest['status'] = 'preparing_expression_rewrite'
    atomic_json(root / 'trial.json', manifest)
    lock = None
    try:
        lock = InstanceLock(home / 'runtime/service.lock')
        private_root.mkdir(parents=True)
        (private_root / 'approved-patch.json').write_bytes(raw)
        store = Store(RuntimePaths(home))
        pid = arm['persona_id']
        with store.connection(write=True) as conn:
            if store.current(conn) != pid:
                raise RuntimeErrorBase('expression_candidate_role_mismatch')
            counts = _empty_history(conn)
            before = _versions(conn, pid)
            expected = replaced_data(before, approved)
            operation = 'expression-candidate-' + _sha(raw)
            profile = ProfileService(store)
            for kind in ('core', 'expression'):
                profile._write(conn, pid, kind, expected[kind], before[kind]['id'], operation + '-' + kind,
                               source_ids=before[kind]['source_ids'])
            CharacterService(store)._write(conn, pid, expected['character'], before['character']['id'],
                                           operation + '-character')
            after = _versions(conn, pid)
            if any(after[k]['data'] != expected[k] for k in after):
                raise RuntimeErrorBase('expression_unexpected_data_change')
        # The character head changed; the index prepared before the patch is stale.
        ready = trial.prepare_candidate_retrieval(store, ConfigStore(store.paths), pid)
        with store.connection() as conn:
            _empty_history(conn)
            final = _versions(conn, pid)
            if final != after:
                raise RuntimeErrorBase('expression_versions_changed_during_preparation')
        from persona_runtime.character_retrieval import assemble
        anchors = assemble(final['character'], [], {'lore': [], 'example': []}, 0)
        anchor_present = any(p['id'] == 'social.public_image' for p in anchors['parts'])
        receipt = {'version': VERSION, 'status': 'applied', 'applied_at': timestamp(),
                   'patch_sha256': _sha(raw), 'persona_id': pid, 'source_binding': approved['source_binding'],
                   'source_content_hashes': approved['source_content_hashes'],
                   'before_versions': {k: v['id'] for k, v in before.items()},
                   'after_versions': {k: v['id'] for k, v in final.items()},
                   'before_hashes': _content_hashes(before), 'after_hashes': _content_hashes(final),
                   'changed_paths': list(PATHS), 'fresh_counts': counts,
                   'local_retrieval': ready, 'public_image_anchor_retained': anchor_present,
                   'config_hash': fingerprint(ConfigStore(store.paths).load()), 'generation_calls': 0}
        atomic_json(private_root / 'application.json', receipt)
        manifest.update(status='prepared', local_retrieval=ready,
                        expression_candidate={'version': VERSION, 'patch_sha256': _sha(raw),
                            'receipt_sha256': _sha((private_root / 'application.json').read_bytes()),
                            'planned_rounds': [8, 12], 'public_image_anchor_retained': anchor_present})
        atomic_json(root / 'trial.json', manifest)
        return root
    except BaseException as error:
        manifest.update(status='preparation_blocked',
                        error_code=str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__)
        atomic_json(root / 'trial.json', manifest)
        error.preparation_trial_root = str(root)
        raise
    finally:
        if lock is not None:
            lock.close()


def prepare(approved_patch, source_home=DEFAULT_HOME, *, approve_current_provider=False):
    with _offline_preparation():
        raw, approved, marker = read_approval(approved_patch, source_home)
        root = trial.prepare(source_home, private=True, approve_current_provider=approve_current_provider,
                             structural_candidate=True)
        try:
            return apply_to_fresh_copy(root, raw, approved, marker)
        except BaseException as error:
            # Even an early scope check after successful clone creation retains
            # cleanup ownership and cannot leave an apparently usable trial.
            manifest = json.loads((root / 'trial.json').read_text(encoding='utf-8'))
            manifest.update(status='preparation_blocked',
                            error_code=str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__)
            atomic_json(root / 'trial.json', manifest)
            error.preparation_trial_root = str(root)
            raise


def verify(root):
    manifest = trial.load_trial(root)
    stamp = manifest.get('expression_candidate') or {}
    if stamp.get('version') != VERSION or set(manifest['arms']) != {'C'}:
        raise RuntimeErrorBase('expression_candidate_not_prepared')
    arm = manifest['arms']['C']
    home, marker = _copy_marker(arm['home'], manifest['source_home'])
    private_root = safe(home / RUNTIME_DIRECTORY, home)
    raw = (private_root / 'approved-patch.json').read_bytes()
    approved = validate_approval(json.loads(raw.decode('utf-8')))
    receipt_bytes = (private_root / 'application.json').read_bytes()
    receipt = json.loads(receipt_bytes)
    if (_sha(raw) != stamp['patch_sha256'] or _sha(receipt_bytes) != stamp['receipt_sha256']
            or receipt.get('patch_sha256') != stamp['patch_sha256'] or receipt.get('status') != 'applied'
            or marker['persona_id'] != arm['persona_id'] or receipt['persona_id'] != arm['persona_id']):
        raise RuntimeErrorBase('expression_application_binding_changed')
    store = Store(RuntimePaths(home))
    with store.connection() as conn:
        if store.current(conn) != arm['persona_id']:
            raise RuntimeErrorBase('expression_candidate_role_mismatch')
        versions = _versions(conn, arm['persona_id'])
    if ({k: v['id'] for k, v in versions.items()} != receipt['after_versions']
            or _content_hashes(versions) != receipt['after_hashes']
            or fingerprint(ConfigStore(store.paths).load()) != receipt['config_hash']):
        raise RuntimeErrorBase('expression_candidate_changed')
    return {'version': VERSION, 'verified': True, 'root': str(Path(root).resolve()),
            'home': str(home), 'persona_id': arm['persona_id'],
            'public_image_anchor_retained': stamp['public_image_anchor_retained']}


def open_window(root):
    verify(root)
    return trial.open_window(root, 'C')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--approved-patch', required=True, type=Path)
    prep.add_argument('--home', type=Path, default=DEFAULT_HOME)
    prep.add_argument('--approve-current-provider', action='store_true')
    for name in ('verify', 'open'):
        sub.add_parser(name).add_argument('root', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        root = prepare(args.approved_patch, args.home, approve_current_provider=args.approve_current_provider)
        print(json.dumps(verify(root), ensure_ascii=False))
    elif args.action == 'verify':
        print(json.dumps(verify(args.root), ensure_ascii=False))
    else:
        open_window(args.root)


if __name__ == '__main__':
    main()
