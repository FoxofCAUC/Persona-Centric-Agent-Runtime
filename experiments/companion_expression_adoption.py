"""Exact user-approved author edits and disposable current-version copies.

No model, GUI, HTTP, original-package rewrite, or global policy operation lives
here. Existing profile/character services remain the authoring authority.
"""
import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService, head as character_head, validate_character
from persona_runtime.config import atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.persona_management import PersonaManagement
from persona_runtime.private_personas import PrivatePersonas, canonical, safe
from persona_runtime.profile import ProfileService, head as profile_head
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-expression-adoption-v1'
REVIEW_VERSION = 'companion-expression-adoption-review-v1'
SCOPE = 'formal_author_version_and_workbench_trial'
REVIEW_NAME = '完整表达资料改稿-待审阅.md'
KINDS = ('core', 'expression', 'relationship', 'state', 'character')
PATHS = ('core.core_traits[0]', 'expression.style', 'character.social.public_image')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _error(code):
    raise RuntimeErrorBase('adoption_' + code)


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _versions_on(db, pid):
    versions = {kind: profile_head(db, pid, kind) for kind in KINDS if kind != 'character'}
    versions['character'] = character_head(db, pid)
    expected = {'format_version': 1, **versions['core']['data'],
                'expression_style': versions['expression']['data']['style']}
    if RuntimeService._persona(db, pid)['spec'] != expected:
        _error('profile_projection_changed')
    return versions


def _versions(store, pid):
    with store.connection() as db:
        return _versions_on(db, pid)


def _descriptors(versions):
    return {kind: {'id': v['id'], 'revision': v['revision'],
                   'content_sha256': fingerprint(v['data'])} for kind, v in versions.items()}


def _selected(store):
    with store.connection() as db:
        return store.current(db)


def _select(store, expected, target):
    # Keep the existing service operation, with selection CAS in its same SQL
    # transaction; an external selection cannot be overwritten between reads.
    with store.connection(write=True) as db:
        if store.current(db) != expected:
            _error('selection_changed')
        @contextmanager
        def held_connection(**kwargs):
            yield db
        RuntimeService(SimpleNamespace(paths=store.paths, connection=held_connection)).use_persona(target)


def _idle(store, pid):
    with store.connection() as db:
        PersonaManagement(store)._idle(db, pid)


def _maintenance(paths, private_id):
    stack = ExitStack()
    try:
        for name in ('service', 'desktop', 'console'):
            lock = InstanceLock(paths.home / 'runtime' / (name + '.lock'))
            stack.callback(lock.close)
        lock = InstanceLock(safe(paths.home / 'private/personas' / private_id,
                                 paths.home / 'private/personas') / 'author-adoption.lock')
        stack.callback(lock.close)
    except BaseException:
        stack.close()
        raise
    return stack


def _originals(paths, binding):
    registry = PrivatePersonas(paths)
    record = registry.record(binding['private_id'])
    if record.get('persona_id') != binding['persona_id'] or record.get('state') != 'ready':
        _error('source_identity_changed')
    folder = safe(registry.root / record['id'], registry.root)
    expected = {
        'original_package_sha256': record['package_sha256'],
        'original_archive_sha256': record['archive_sha256'],
        'initial_relationship_sha256': record['relationship_sha256'],
    }
    actual = {
        'original_package_sha256': sha(safe(folder / 'source-package.json', folder).read_bytes()),
        'original_archive_sha256': sha(safe(folder / 'source-archive.md', folder).read_bytes()),
        'initial_relationship_sha256': canonical(safe(folder / 'initial-relationship.txt', folder).read_text(encoding='utf-8')),
    }
    if any(binding.get(k) != value or actual[k] != value for k, value in expected.items()):
        _error('original_bytes_changed')
    return record


def _review(draft_path, source_home, require_approval):
    paths = RuntimePaths(Path(source_home).resolve())
    draft_path = safe(draft_path, paths.home / 'tmp/private-acceptance')
    relative = draft_path.relative_to(paths.home / 'tmp/private-acceptance')
    if len(relative.parts) < 2:
        _error('review_scope_invalid')
    review_home = paths.home / 'tmp/private-acceptance' / relative.parts[0]
    marker = _json(safe(review_home / 'private-sandbox.json', review_home))
    raw = draft_path.read_bytes()
    draft = json.loads(raw.decode('utf-8'))
    binding = draft.get('source_binding') or {}
    if (draft.get('version') != REVIEW_VERSION or draft.get('status') != 'awaiting_user_review'
            or draft.get('approval') is not None
            or marker.get('purpose') != 'stage4B_expression_review_only'
            or Path(marker.get('formal_home', '')).resolve() != paths.home
            or marker.get('private_id') != binding.get('private_id')
            or marker.get('persona_id') != binding.get('persona_id')):
        _error('review_scope_invalid')
    rows = draft.get('replacements')
    if (not isinstance(rows, list) or len(rows) != 3
            or any(not isinstance(r, dict) for r in rows)
            or {r.get('path') for r in rows} != set(PATHS)):
        _error('replacement_paths_invalid')
    for row in rows:
        if any(not isinstance(row.get(k), str) or not row[k].strip()
               for k in ('before', 'after', 'reason')) or row['before'] == row['after']:
            _error('replacement_text_invalid')
    declared = binding.get('versions')
    if not isinstance(declared, dict) or set(declared) != set(KINDS):
        _error('source_versions_invalid')
    for row in declared.values():
        if (not isinstance(row, dict) or set(row) != {'id', 'revision', 'content_sha256'}
                or not isinstance(row['id'], str) or type(row['revision']) is not int
                or not isinstance(row['content_sha256'], str) or len(row['content_sha256']) != 64):
            _error('source_versions_invalid')
    record = _originals(paths, binding)
    approval = None
    review_file = safe(draft_path.parent / REVIEW_NAME, review_home)
    review_hash = sha(review_file.read_bytes())
    if require_approval:
        approval_path = safe(draft_path.parent / 'approval.json', review_home)
        if not approval_path.is_file():
            _error('approval_missing')
        approval = _json(approval_path)
        if (approval.get('draft_sha256') != sha(raw) or approval.get('review_sha256') != review_hash
                or approval.get('scope') != SCOPE
                or any(not isinstance(approval.get(k), str) or not approval[k].strip()
                       for k in ('approved_at', 'user_answer'))):
            _error('approval_mismatch')
    return paths, draft_path, draft, record, sha(raw), review_hash, approval


def _target_data(versions, draft):
    data = {kind: deepcopy(v['data']) for kind, v in versions.items()}
    fields = {PATHS[0]: (data['core']['core_traits'], 0),
              PATHS[1]: (data['expression'], 'style'),
              PATHS[2]: (data['character']['social'], 'public_image')}
    for row in draft['replacements']:
        target, key = fields[row['path']]
        if target[key] != row['before']:
            _error('before_text_changed')
        target[key] = row['after']
    return data


def _validate_target(store, pid, data):
    with store.connection() as db:
        for kind in ('core', 'expression'):
            ProfileService.validate(db, pid, kind, deepcopy(data[kind]))
    validate_character(data['character'])


def _binding_path(paths, private_id, digest):
    parent = safe(paths.home / 'private/personas' / private_id, paths.home / 'private/personas')
    return safe(parent / 'author-adoptions' / (digest + '.json'), parent)


def _saved(binding_path, source_home):
    paths = RuntimePaths(Path(source_home).resolve())
    binding_path = safe(binding_path, paths.home / 'private/personas')
    value = _json(binding_path)
    source = value.get('source_binding') or {}
    if (value.get('version') != VERSION or value.get('status') != 'saved'
            or value.get('approval_scope') != SCOPE
            or _binding_path(paths, source.get('private_id', ''), value.get('draft_sha256', '')) != binding_path):
        _error('saved_binding_invalid')
    record = _originals(paths, source)
    store = Store(paths)
    current = _versions(store, source['persona_id'])
    if _descriptors(current) != value.get('after_versions'):
        _error('approved_versions_changed')
    return paths, store, record, value, current


def verify(draft_path, source_home=DEFAULT_HOME, *, require_approval=False):
    paths, path, draft, _, digest, review_hash, _ = _review(draft_path, source_home, require_approval)
    source = draft['source_binding']
    saved_path = _binding_path(paths, source['private_id'], digest)
    if saved_path.exists():
        _saved(saved_path, source_home)
        state = 'saved'
    else:
        versions = _versions(Store(paths), source['persona_id'])
        if _descriptors(versions) != source['versions']:
            _error('source_versions_changed')
        _validate_target(Store(paths), source['persona_id'], _target_data(versions, draft))
        state = 'approved_not_saved' if require_approval else 'awaiting_user_review'
    return {'version': VERSION, 'status': state, 'draft_sha256': digest,
            'review_sha256': review_hash, 'binding_path': str(saved_path),
            'original_bytes_unchanged': True, 'generation_calls': 0}


def save(draft_path, source_home=DEFAULT_HOME):
    paths, path, draft, _, digest, review_hash, approval = _review(draft_path, source_home, True)
    source = draft['source_binding']; pid = source['persona_id']
    journal_path = path.parent / 'save-progress.json'
    binding_path = _binding_path(paths, source['private_id'], digest)
    with _maintenance(paths, source['private_id']):
        if journal_path.exists():
            old = _json(journal_path)
            if old.get('status') == 'saved' and old.get('draft_sha256') == digest and binding_path.exists():
                _saved(binding_path, source_home)
                return old | {'already_saved': True}
            _error('save_requires_inspection')
        if binding_path.exists():
            _error('save_requires_inspection')
        store = Store(paths); _idle(store, pid)
        before = _versions(store, pid)
        if _descriptors(before) != source['versions']:
            _error('source_versions_changed')
        target = _target_data(before, draft)
        _validate_target(store, pid, target)
        selected = _selected(store)
        journal = {'version': VERSION, 'status': 'saving', 'started_at': timestamp(),
                   'draft_sha256': digest, 'completed_kinds': [], 'previous_selection': selected,
                   'target_persona_id': pid, 'generation_calls': 0, 'binding_path': str(binding_path)}
        atomic_json(journal_path, journal)
        written = {}; current = before; compensation = {}; selection_restored = False
        try:
            if selected != pid:
                _select(store, selected, pid)
            for kind in ('core', 'expression', 'character'):
                if _selected(store) != pid:
                    _error('selection_changed')
                _idle(store, pid)
                if _descriptors(_versions(store, pid)) != _descriptors(current):
                    _error('source_versions_changed')
                _originals(paths, source)
                with store.connection(write=True) as db:
                    if store.current(db) != pid:
                        _error('selection_changed')
                    if _descriptors(_versions_on(db, pid)) != _descriptors(current):
                        _error('source_versions_changed')
                    PersonaManagement(store)._idle(db, pid)
                    if kind == 'character':
                        result = CharacterService(store)._write(db, pid, target[kind], current[kind]['id'],
                            'adoption-' + digest + '-' + kind)
                    else:
                        result = ProfileService(store)._write(db, pid, kind, target[kind], current[kind]['id'],
                            'adoption-' + digest + '-' + kind, source_ids=current[kind]['source_ids'])
                written[kind] = result['id']
                current = _versions(store, pid)
                expected_data = {k: target[k] if k in written else before[k]['data'] for k in KINDS}
                if {k: v['data'] for k, v in current.items()} != expected_data:
                    _error('unexpected_data_change')
                journal.update(completed_kinds=list(written), after_versions=_descriptors(current))
                atomic_json(journal_path, journal)
            if _selected(store) != pid:
                _error('selection_changed')
            if selected != pid:
                _select(store, pid, selected)
            selection_restored = _selected(store) == selected
            if not selection_restored:
                _error('selection_changed')
            _originals(paths, source)
            journal.update(status='saved', finished_at=timestamp(), selection_restored=True)
            atomic_json(journal_path, journal)
            metadata = {'version': VERSION, 'status': 'saved', 'saved_at': timestamp(),
                        'draft_sha256': digest, 'review_sha256': review_hash,
                        'approval_sha256': fingerprint(approval), 'approval_scope': SCOPE,
                        'approved_at': approval['approved_at'], 'source_binding': source,
                        'after_versions': _descriptors(current), 'changed_paths': list(PATHS),
                        'original_bytes_unchanged': True, 'generation_calls': 0}
            atomic_json(binding_path, metadata)
            return journal
        except BaseException as error:
            # Never roll back an independently changed head or another selection.
            if selection_restored and _selected(store) == selected and selected != pid:
                _select(store, selected, pid)
                selection_restored = False
            for kind in reversed(tuple(written)):
                if _selected(store) != pid:
                    compensation[kind] = 'selection_conflict'; continue
                now = _versions(store, pid)
                if now[kind]['id'] != written[kind]:
                    compensation[kind] = 'version_conflict'; continue
                try:
                    if kind == 'character':
                        CharacterService(store).restore(pid, before[kind]['id'], written[kind],
                            'adoption-rollback-' + digest + '-' + kind)
                    else:
                        ProfileService(store).restore(before[kind]['id'], written[kind],
                            'adoption-rollback-' + digest + '-' + kind)
                    compensation[kind] = 'restored'
                except BaseException as rollback_error:
                    compensation[kind] = type(rollback_error).__name__
            if _selected(store) == pid and selected != pid:
                try:
                    _select(store, pid, selected)
                except BaseException as selection_error:
                    compensation['selection'] = type(selection_error).__name__
            selection_restored = _selected(store) == selected
            original_data_restored = {k: v['data'] for k, v in _versions(store, pid).items()} == {
                k: v['data'] for k, v in before.items()}
            journal.update(status='failed_rolled_back' if all(v == 'restored' for v in compensation.values())
                           and selection_restored and original_data_restored else 'failed_requires_inspection',
                           error_code=str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__,
                           compensation=compensation, selection_restored=selection_restored, finished_at=timestamp())
            if binding_path.exists():
                atomic_json(binding_path, {'version': VERSION, 'status': 'failed_requires_inspection',
                                           'draft_sha256': digest})
            atomic_json(journal_path, journal)
            raise


def _visible_data(versions):
    result = {k: deepcopy(v['data']) for k, v in versions.items()}
    result['character']['lorebook'] = [r for r in result['character']['lorebook'] if r['visibility'] == 'conversation']
    result['state'] = {'text': '', 'expires_at': None}
    return result


def _copy_check(home, metadata, source_versions, paths):
    root = paths.home / 'tmp/private-acceptance'
    home = safe(home, root)
    if home.parent != root:
        _error('copy_scope_invalid')
    marker = _json(home / 'private-sandbox.json')
    if (marker.get('adoption_sha256') != metadata['draft_sha256']
            or marker.get('formal_home') != str(paths.home)
            or marker.get('private_id') != metadata['source_binding']['private_id']
            or marker.get('purpose') != 'stage4B_expression_validation'):
        _error('copy_binding_changed')
    store = Store(RuntimePaths(home))
    versions = _versions(store, marker['persona_id'])
    if _descriptors(versions) != marker.get('copy_versions'):
        _error('copy_versions_changed')
    if {k: v['data'] for k, v in versions.items()} != _visible_data(source_versions):
        _error('copy_approved_content_changed')
    return marker


def prepare_copy(binding_path, source_home=DEFAULT_HOME):
    paths, source_store, record, metadata, versions = _saved(binding_path, source_home)
    source = metadata['source_binding']; pid = source['persona_id']
    state_path = Path(binding_path).with_suffix('.copy.json')
    with _maintenance(paths, source['private_id']):
        paths, source_store, record, metadata, versions = _saved(binding_path, source_home)
        if state_path.exists():
            saved = _json(state_path)
            if saved.get('status') != 'prepared' or not Path(saved['home']).is_dir():
                _error('copy_requires_inspection')
            marker = _copy_check(saved['home'], metadata, versions, paths)
            if marker['persona_id'] != saved['persona_id']:
                _error('copy_binding_changed')
            return saved | {'already_prepared': True}
        _idle(source_store, pid)
        root = safe(paths.home / 'tmp/private-acceptance')
        home = safe(root / identifier(), root)
        state = {'version': VERSION, 'status': 'preparing', 'home': str(home),
                 'draft_sha256': metadata['draft_sha256'], 'generation_calls': 0}
        atomic_json(state_path, state)
        home.mkdir(parents=True)
        marker = {'private_id': source['private_id'], 'persona_id': None,
                  'formal_home': str(paths.home), 'created_at': timestamp(),
                  'purpose': 'stage4B_expression_validation', 'adoption_sha256': metadata['draft_sha256']}
        atomic_json(home / 'private-sandbox.json', marker)
        try:
            target = Store(RuntimePaths(home)); target.initialize()
            runtime = RuntimeService(target)
            seed = runtime.create_persona(generic_persona())['id']; runtime.use_persona(seed)
            character = _visible_data(versions)['character']
            package = {'format': 'pcar_character_v1',
                       'persona_spec': {'format_version': 1, **versions['core']['data'],
                                        'expression_style': versions['expression']['data']['style']},
                       'character': character}
            copied = CharacterService(target).import_package(seed, package, 'adoption-copy-' + metadata['draft_sha256'])
            runtime.use_persona(copied['id'])
            profile = ProfileService(target); relation = profile.current('relationship')
            profile.update('relationship', deepcopy(versions['relationship']['data']), relation['id'],
                           'adoption-copy-relationship-' + metadata['draft_sha256'])
            copy_versions = _versions(target, copied['id'])
            with target.connection() as db:
                empty = {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                         for table in ('events', 'turns', 'attempts', 'memories', 'summaries',
                                       'input_batches', 'quota_reservations', 'disposition_decisions')}
            if any(empty.values()):
                _error('copy_history_not_empty')
            if _descriptors(_versions(source_store, pid)) != metadata['after_versions']:
                _error('approved_versions_changed')
            marker.update(persona_id=copied['id'], copy_versions=_descriptors(copy_versions),
                          source_versions=metadata['after_versions'])
            atomic_json(home / 'private-sandbox.json', marker)
            atomic_json(home / 'runtime/private-copy-policy.json', {
                'persona_id': copied['id'], 'approved_models': record['approved_models']})
            state.update(status='prepared', persona_id=copied['id'], empty_history_counts=empty,
                         creator_only_lore_copied=False, source_state_copied=False,
                         original_bytes_unchanged=True, prepared_at=timestamp())
            atomic_json(state_path, state)
            return state
        except BaseException as error:
            state.update(status='failed_requires_inspection',
                         error_code=str(error) if isinstance(error, RuntimeErrorBase) else type(error).__name__)
            atomic_json(state_path, state)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'save', 'prepare_copy'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--require-approval', action='store_true')
    args = parser.parse_args()
    if args.action == 'verify':
        result = verify(args.path, args.home, require_approval=args.require_approval)
    else:
        result = {'save': save, 'prepare_copy': prepare_copy}[args.action](args.path, args.home)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
