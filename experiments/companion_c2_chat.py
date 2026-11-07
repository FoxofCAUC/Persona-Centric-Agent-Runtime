"""One owned C2 Workbench conversation. Preparation never generates a reply."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments import companion_c2 as replay
from experiments import companion_expression_adoption as adoption
from experiments import companion_c2_lifecycle as lifecycle
from experiments.companion_trial import configure, configure_policy, copy_weights
from experiments.companion_comparison import local_retrieval_ready
from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.generation_retry import audited_complete
from persona_runtime.models import RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity, safe
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store

VERSION = 'companion-c2-workbench-v1'
NAMESPACE = 'companion-c2-chat'
TITLE = 'PCAR · C2连续体验'
PURPOSE_VERSION = 'companion-persona-workbench-v1'
PURPOSE_NAMESPACE = 'companion-persona-chat'
PURPOSE_TITLE = 'PCAR · 人物资料用途验证'
RULES_VERSION = 'companion-context-rules-workbench-v1'
RULES_NAMESPACE = 'companion-context-rules-chat'
RULES_TITLE = 'PCAR · 情境规则消费验证'
LIGHT_VERSION = 'companion-prompt-light-workbench-v1'
LIGHT_NAMESPACE = 'companion-prompt-light-chat'
LIGHT_TITLE = 'PCAR · 轻量提示连续体验'
NATIVE_VERSION = 'companion-native-text-workbench-v1'
NATIVE_NAMESPACE = 'companion-native-text-chat'
NATIVE_TITLE = 'PCAR · 无示范纯文本体验'
IM_VERSION = 'companion-im-workbench-v1'
IM_NAMESPACE = 'companion-im-chat'
IM_TITLE = 'PCAR · DeepSeek即时通讯体验'
read = replay.read


def require(condition, reason):
    if not condition:
        raise RuntimeErrorBase('c2_chat_' + reason)


def source_hashes():
    result = replay.source_hashes()
    for name in ('experiments/companion_c2_chat.py', 'experiments/companion_workbench_trial.py',
                 'experiments/companion_c2_lifecycle.py'):
        result[name] = replay.sha(ROOT / name)
    return result


def body(owner):
    return safe(Path(owner['home']) / 'runtime/c2-chat', owner['home'])


def conditions(paths, pid):
    store = Store(paths)
    cfg = ConfigStore(paths).load()
    # The owned loopback listener receives an available port on startup.
    cfg.pop('service_port', None)
    with store.connection() as db:
        require(store.current(db) == pid, 'persona_changed')
        row = db.execute('SELECT p.* FROM interaction_heads h JOIN interaction_policies p '
                         'ON p.id=h.version_id WHERE h.persona_id=? AND h.channel=?', (pid, 'desktop')).fetchone()
    require(row is not None, 'policy_missing')
    return {'versions': adoption._descriptors(adoption._versions(store, pid)),
            'policy_hash': fingerprint(dict(row)), 'config_hash': fingerprint(cfg),
            'permission_hash': replay.sha(paths.home / 'runtime/private-copy-policy.json'),
            'owner_hash': replay.sha(paths.home / 'private-sandbox.json')}


def prepare(binding_path, source_home=DEFAULT_HOME, *, approve_current_provider=False, integration=False, persona_purpose=False, context_rules_off=False, prompt_light=False, native_text=False, im_style=False, model_configuration_home=None):
    require(sum((integration, persona_purpose, context_rules_off, prompt_light, native_text, im_style)) <= 1, 'conflicting_trial_modes')
    source_home = Path(source_home).resolve()
    paths, _, record, _, _ = adoption._saved(binding_path, source_home)
    source = deepcopy(ConfigStore(paths).load())
    formal_config_hash = fingerprint(source)
    donor = None
    if model_configuration_home is not None:
        require(native_text and approve_current_provider, 'model_import_not_authorized')
        donor_home = safe(model_configuration_home, source_home / 'tmp/private-acceptance')
        require(donor_home.parent == source_home / 'tmp/private-acceptance', 'model_import_scope')
        marker = read(donor_home / 'private-sandbox.json')
        require(marker.get('purpose') == NATIVE_VERSION and marker.get('private_id') == record['id']
                and Path(marker.get('formal_home', '')).resolve() == source_home, 'model_import_owner')
        donor_config = ConfigStore(RuntimePaths(donor_home)).load()
        donor = {'home': str(donor_home), 'config_hash': fingerprint(donor_config),
                 'model_public': replay.base.public_model(donor_config['model'])}
        source['model'] = deepcopy(donor_config['model'])
    private = source_home == DEFAULT_HOME.resolve()
    if private and not approve_current_provider:
        guard_cloud_dispatch(Store(paths), record['persona_id'], 'main', source['model'])
    version = lifecycle.VERSION if integration else VERSION
    namespace = lifecycle.NAMESPACE if integration else NAMESPACE
    mode = 'minimal_chat'
    if persona_purpose:
        version, namespace, mode = PURPOSE_VERSION, PURPOSE_NAMESPACE, 'minimal_persona'
    if context_rules_off:
        version, namespace, mode = RULES_VERSION, RULES_NAMESPACE, 'minimal_context_rules_off'
    if prompt_light:
        version, namespace, mode = LIGHT_VERSION, LIGHT_NAMESPACE, 'minimal_experience'
    if native_text:
        version, namespace, mode = NATIVE_VERSION, NATIVE_NAMESPACE, 'minimal_native'
    if im_style:
        version, namespace, mode = IM_VERSION, IM_NAMESPACE, 'minimal_im'
    root = safe(source_home / 'tmp' / namespace / identifier(), source_home)
    root.mkdir(parents=True)
    if (prompt_light or native_text or im_style) and private:
        private_directory(source_home, root)
    owner = {'version': version, 'status': 'preparing', 'private': private,
             'source_home': str(source_home), 'source_config_hash': formal_config_hash,
             'provider_approval': 'explicit_current_stage' if private and approve_current_provider else 'existing_registry_or_artificial',
             'created_at': timestamp(),
             'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
    if donor:
        owner['model_configuration'] = donor
    manifest = root / 'trial.json'
    atomic_json(manifest, owner)
    source['character_selector'].update(enabled=False, encrypted_key='')
    source['organization']['model']['encrypted_key'] = ''
    if not private:
        source['model']['encrypted_key'] = ''
    engine = None
    try:
        with replay.base.offline():
            copy_paths, pid = replay.create_copy(source_home, binding_path, owner, root)
            marker_path = copy_paths.home / 'private-sandbox.json'
            marker = read(marker_path)
            marker['purpose'] = version
            atomic_json(marker_path, marker)
            if (prompt_light or native_text or im_style) and private:
                private_directory(source_home, copy_paths.home)
            config = configure(copy_paths, source, pid)
            atomic_json(copy_paths.home / 'runtime/private-copy-policy.json', {
                'persona_id': pid, 'approved_models': {'main': provider_identity(source['model'])},
                'approval_scope': version})
            if private:
                copy_weights(paths, copy_paths)
            store = Store(copy_paths)
            configure_policy(store, config, pid, mode, background_prepare=False)
            engine = ChatEngine(store, config, client=replay.base.FixtureClient())
            local_retrieval_ready(engine.cognitive.characters, pid, ['你好'], build=True,
                                  development_material=not private)
            frozen = {'version': version, 'profile': choose(mode),
                      'conditions': conditions(copy_paths, pid), 'source_hashes': source_hashes(),
                      'model_public': replay.base.public_model(source['model']),
                      'experience': ('one_free_conversation_10_to_15_turns_early_end_allowed' if persona_purpose
                                     else 'one_free_conversation_8_to_12_turns_early_end_allowed')}
            if prompt_light or native_text or im_style:
                frozen['approved_versions'] = deepcopy(owner['approved_versions'])
            atomic_json(body(owner) / 'plan.json', frozen)
            atomic_json(body(owner) / 'state.json', {'status': 'prepared', 'real_calls': 0})
            owner.update(status='prepared', plan_hash=fingerprint(frozen))
            atomic_json(manifest, owner)
            verify(manifest)
        return manifest
    except BaseException as error:
        owner.update(status='preparation_blocked', error_code=replay.base.error_code(error))
        atomic_json(manifest, owner)
        error.preparation_trial_root = str(root)
        raise
    finally:
        if engine is not None:
            engine.cognitive.characters.close()
            engine.cognitive.shadow.close()


def verify(manifest):
    manifest = safe(manifest)
    owner = read(manifest)
    source = Path(owner['source_home']).resolve()
    version = owner.get('version')
    require(version in (VERSION, lifecycle.VERSION, PURPOSE_VERSION, RULES_VERSION, LIGHT_VERSION, NATIVE_VERSION, IM_VERSION), 'version_invalid')
    namespace = {VERSION: NAMESPACE, lifecycle.VERSION: lifecycle.NAMESPACE,
                 PURPOSE_VERSION: PURPOSE_NAMESPACE, RULES_VERSION: RULES_NAMESPACE,
                 LIGHT_VERSION: LIGHT_NAMESPACE, NATIVE_VERSION: NATIVE_NAMESPACE, IM_VERSION: IM_NAMESPACE}[version]
    require(manifest.name == 'trial.json' and manifest.parent.parent == source / 'tmp' / namespace,
            'manifest_scope')
    require(owner.get('status') == 'prepared', 'not_prepared')
    require(datetime.now(timezone.utc) <= datetime.fromisoformat(owner['review_deadline']), 'review_expired')
    home = safe(owner['home'], source / 'tmp/private-acceptance')
    require(home.parent == source / 'tmp/private-acceptance', 'copy_scope')
    marker = read(home / 'private-sandbox.json')
    require(marker['purpose'] == version and marker['persona_id'] == owner['persona_id']
            and Path(marker['formal_home']).resolve() == source, 'owner_changed')
    plan = read(body(owner) / 'plan.json')
    require(fingerprint(plan) == owner['plan_hash'], 'plan_changed')
    require(source_hashes() == plan['source_hashes'], 'source_changed')
    require(conditions(RuntimePaths(home), owner['persona_id']) == plan['conditions'], 'conditions_changed')
    require(replay.sha(owner['author_binding']) == owner['author_binding_hash'], 'approval_changed')
    adoption._saved(owner['author_binding'], source)
    if version in (LIGHT_VERSION, NATIVE_VERSION, IM_VERSION):
        mode = {LIGHT_VERSION: 'minimal_experience', NATIVE_VERSION: 'minimal_native', IM_VERSION: 'minimal_im'}[version]
        require(plan['profile'] == choose(mode), 'light_profile_changed')
        require(plan.get('approved_versions') == owner['approved_versions'], 'light_author_binding_changed')
    require(fingerprint(ConfigStore(RuntimePaths(source)).load()) == owner['source_config_hash'],
            'formal_config_changed')
    if owner.get('model_configuration'):
        donor = owner['model_configuration']
        donor_home = safe(donor['home'], source / 'tmp/private-acceptance')
        require(fingerprint(ConfigStore(RuntimePaths(donor_home)).load()) == donor['config_hash'],
                'model_configuration_changed')
    return owner, plan


def begin(manifest, *, reopen=False):
    owner, plan = verify(manifest)
    data = body(owner)
    if owner['version'] == lifecycle.VERSION:
        lifecycle.begin(owner, data, reopen=reopen)
        return owner, plan
    require(not reopen, 'legacy_reopen_forbidden')
    require(not (data / 'started.json').exists(), 'started_requires_review')
    store = Store(RuntimePaths(Path(owner['home'])))
    adoption._idle(store, owner['persona_id'])
    with store.connection() as db:
        require(all(db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
                    for table in ('events', 'turns', 'attempts', 'input_batches', 'quota_reservations')),
                'initial_history_not_empty')
    atomic_json(data / 'started.json', {'at': timestamp(), 'version': owner['version'], 'startup_generation_calls': 0})
    atomic_json(data / 'state.json', {'status': 'starting', 'automatic_resume': False})
    return owner, plan


def light_material_record(turn, prepared):
    """Current finalized input IDs only; never retain author or message text."""
    assembly = prepared['details'].get('prompt_assembly') or {}
    parts = []
    observed = False
    for block in assembly.get('blocks', []):
        if block['name'] != 'character':
            continue
        text = prepared['messages'][block['message_index']]['content']
        text = text[block['start_character']:block['start_character'] + block['characters']]
        observed = True
        for line in text.splitlines():
            if not line.startswith('{'):
                continue
            item = json.loads(line)
            if item.get('kind') == 'lore_group':
                parts.extend({'kind': 'lore', 'id': row['id']} for row in item['data'])
            elif item.get('kind') in ('anchor', 'fact', 'example'):
                row = {'kind': item['kind'], 'id': item['id']}
                if isinstance(item.get('data'), dict) and 'status' in item['data']:
                    row['fill_status'] = item['data']['status']
                parts.append(row)
    profile = json.loads(turn['profile_snapshot'])
    return {'status': 'collected' if observed else 'not_collected',
            'input_snapshot_hash': fingerprint(prepared['messages']),
            'expression_renderer': assembly.get('expression_renderer'),
            'format_renderer': assembly.get('format_renderer'),
            'author_versions': {kind: {'id': row['id'], 'revision': row['revision']}
                                for kind, row in profile.items() if isinstance(row, dict) and 'id' in row},
            'core_field_ids': ['core.' + key for key in profile.get('core', {}).get('data', {})],
            'final_character_parts': parts,
            'selected_materials': replay.base.selected_materials(prepared),
            's8': {'status': 'not_reviewed', 'judged_by': None}}


class ConversationGuard:
    """Capture actual dispatches and delegate all retries to the native runtime."""
    def __init__(self, manifest, engine, target):
        self.manifest = Path(manifest)
        self.owner, self.plan = verify(manifest)
        self.data = body(self.owner)
        self.engine, self.target = engine, target
        self.folder = None
        self.request = None
        self.audit = None
        self.blocked = False

    @staticmethod
    def get(*args):
        return None

    def stop(self, error):
        self.blocked = True
        self.engine.draining = True
        atomic_json(self.data / 'state.json', {'status': 'stopped', 'automatic_resume': False,
                    'error_code': replay.base.error_code(error), 'at': timestamp()})

    def before_dispatch(self, engine, turn, prepared):
        try:
            require(not self.blocked, 'stopped')
            verify(self.manifest)
            require(turn['persona_id'] == self.owner['persona_id'] and turn['channel'] == 'desktop', 'turn_scope')
            model = json.loads(turn['model_snapshot'])
            require(model.get('_companion_profile') == self.plan['profile'], 'profile_changed')
            with engine.store.connection() as db:
                require(not db.execute("SELECT 1 FROM turns WHERE id!=? AND (status='failed' OR "
                                       "(status='interrupted' AND coalesce(error_code,'')!='cancelled_by_user'))",
                                       (turn['id'],)).fetchone(), 'previous_failure_requires_review')
            self.folder = self.data / 'turns' / turn['id']
            require(not (self.folder / 'request.json').exists(), 'successful_position_not_replayed')
            if self.owner['version'] in (NATIVE_VERSION, IM_VERSION):
                require(not (prepared['details'].get('character') or {}).get('example_ids'), 'native_examples_present')
                require(all(b['characters'] == 0 for b in prepared['details']['prompt_assembly']['blocks']
                            if b['name'] == 'reply_protocol'), 'native_output_template_present')
            hook, captured = replay.base.capture_hook(self.folder, development=not self.owner['private'])
            hook.before_dispatch(engine, turn, prepared)
            if self.owner['version'] in (LIGHT_VERSION, NATIVE_VERSION, IM_VERSION):
                atomic_json(self.folder / 'material-usage.json', light_material_record(turn, prepared))
            self.request = captured['request']
            self.audit = None
        except BaseException as error:
            self.stop(error)
            raise

    def save(self, value):
        try:
            require(self.folder is not None, 'request_missing')
            self.audit = deepcopy(value)
            for attempt in value['generation_retry']['attempts']:
                reserve = self.folder / (attempt['id'] + '.reserve.json')
                if not reserve.exists():
                    require(attempt['status'] == 'running', 'reservation_missing')
                    atomic_json(reserve, {'kind': 'real' if self.owner['private'] else 'simulated', 'attempt': attempt})
                atomic_json(self.folder / (attempt['id'] + '.attempt.json'), attempt)
            atomic_json(self.folder / 'audit.json', value)
        except BaseException as error:
            self.stop(error)
            raise

    async def complete(self, model, messages):
        try:
            require(not self.blocked, 'stopped')
            verify(self.manifest)
            require(self.request is not None and messages == self.request['messages'], 'input_changed')
            wire = build_request(model, messages)[0]
            require(wire == self.request['wire'], 'wire_changed')
            require(self.audit is not None, 'audit_missing')
            attempt = self.audit['generation_retry']['attempts'][-1]
            require(attempt['status'] == 'running', 'attempt_inactive')
            dispatch = self.folder / (attempt['id'] + '.dispatch.json')
            require(not dispatch.exists(), 'attempt_already_dispatched')
            atomic_json(dispatch, {'attempt_id': attempt['id'], 'at': timestamp(),
                        'kind': 'real' if self.owner['private'] else 'simulated', 'wire_hash': fingerprint(wire)})
        except Exception as error:
            self.stop(error)
            raise
        # Provider failures belong to the native retry loop. Only receipt writes
        # below are classified as local storage failures.
        value = await self.target.complete(model, messages)
        try:
            atomic_json(self.folder / (attempt['id'] + '.response.json'), {
                'raw': value[0], 'usage': value[1], 'diagnostics': getattr(value, 'diagnostics', {})})
            return value
        except (OSError, ValueError, TypeError) as error:
            self.stop(error)
            raise RuntimeErrorBase('c2_chat_receipt_or_storage_error') from error

    async def execute(self, client, model, messages, **kwargs):
        try:
            result = await audited_complete(client, model, messages, **kwargs)
            atomic_json(self.folder / 'generation-terminal.json', {'status': 'completed', 'at': timestamp()})
            return result
        except asyncio.CancelledError:
            atomic_json(self.folder / 'generation-terminal.json', {'status': 'cancelled', 'at': timestamp()})
            raise
        except BaseException as error:
            self.stop(error)
            raise


def install(manifest, engine):
    guard = ConversationGuard(manifest, engine, engine.client)
    require(not guard.owner['private'] or type(engine.client) is ChatCompletionsClient, 'private_client_override')
    engine.evaluation_session = guard
    engine.generation_audit_sink = guard.save
    engine.generation_executor = guard.execute
    engine.client = guard
    return guard


def audit(manifest):
    owner, _ = verify(manifest)
    paths = RuntimePaths(Path(owner['home']))
    engine = ChatEngine(Store(paths), ConfigStore(paths), client=replay.base.FixtureClient())
    rows = []
    try:
        with engine.store.connection() as db:
            turn_ids = [row[0] for row in db.execute('SELECT id FROM turns WHERE persona_id=? ORDER BY rowid',
                                                    (owner['persona_id'],))]
        for turn_id in turn_ids:
            turn = engine.get(turn_id)
            folder = body(owner) / 'turns' / turn['id']
            dispatches = list(folder.glob('*.dispatch.json'))
            row = {'turn_id': turn['id'], 'status': turn['status'], 'error_code': turn.get('error_code'),
                   'physical_calls': len(dispatches) if owner['private'] else 0,
                   'simulated_calls': len(dispatches) if not owner['private'] else 0,
                   'manifest_matches': None}
            if (folder / 'request.json').exists():
                request = read(folder / 'request.json')
                manifest_data = read(folder / 'manifest.json')
                purpose = manifest_data.get('prompt_assembly', {}).get('persona_purpose')
                row['persona_purpose'] = purpose or {'status': 'not_collected'}
                row['manifest_matches'] = (manifest_data.get('prompt_assembly', {}).get('input_snapshot_hash')
                                           == request['messages_hash'] == fingerprint(request['messages']))
                with engine.store.connection() as db:
                    saved = db.execute('SELECT manifest_json FROM context_manifests WHERE id=? AND turn_id=?',
                                       (manifest_data['context_id'], turn_id)).fetchone()
                    attempt = db.execute('SELECT usage_json FROM attempts WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',
                                         (turn_id,)).fetchone()
                stored = json.loads(saved[0]) if saved else {}
                row['persisted_manifest_matches'] = (stored.get('prompt_assembly', {}).get('input_snapshot_hash')
                                                     == request['messages_hash'])
                if (folder / 'audit.json').exists():
                    usage = read(folder / 'audit.json')
                    row['runtime_audit_matches'] = bool(attempt and json.loads(attempt[0]) == usage)
                    row['provider_attempts'] = usage['generation_retry']['attempts']
                    row['retries'] = max(0, len(row['provider_attempts']) - 1)
                row['wire_hash'] = request['wire_hash']
                row['wire_matches'] = all(read(p)['wire_hash'] == request['wire_hash'] for p in dispatches)
                if owner['version'] in (LIGHT_VERSION, NATIVE_VERSION, IM_VERSION):
                    material = folder / 'material-usage.json'
                    row['material_usage'] = read(material) if material.exists() else {'status': 'not_collected'}
            atomic_json(folder / 'result.json', turn)
            rows.append(row)
        with engine.store.connection() as db:
            pending = db.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0]
            active = db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
            auxiliary = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in
                         ('character_selection_attempts', 'situation_observation_attempts', 'organization_attempts')}
        summary = {'version': owner['version'], 'rows': rows, 'pending_turns': pending, 'active_reservations': active,
                   'auxiliary_calls': auxiliary, 'physical_calls': sum(r['physical_calls'] for r in rows),
                   'simulated_calls': sum(r['simulated_calls'] for r in rows)}
        atomic_json(body(owner) / 'audit-summary.json', summary)
        return summary
    finally:
        engine.cognitive.characters.close()
        engine.cognitive.shadow.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify', 'audit'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--approve-current-provider', action='store_true')
    parser.add_argument('--integration', action='store_true')
    parser.add_argument('--persona-purpose', action='store_true')
    parser.add_argument('--context-rules-off', action='store_true')
    parser.add_argument('--prompt-light', action='store_true')
    parser.add_argument('--native-text', action='store_true')
    parser.add_argument('--im-style', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = {'trial_manifest': str(prepare(args.path, args.home,
                      approve_current_provider=args.approve_current_provider, integration=args.integration,
                      persona_purpose=args.persona_purpose, context_rules_off=args.context_rules_off,
                      prompt_light=args.prompt_light, native_text=args.native_text, im_style=args.im_style)), 'real_calls': 0}
    elif args.action == 'verify':
        verify(args.path)
        result = {'passed': True, 'real_calls_added': 0}
    else:
        result = audit(args.path)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
