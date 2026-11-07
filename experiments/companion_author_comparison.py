"""Explicit approved author revisions compared through the existing C2 runner."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random

from experiments import companion_c2 as replay
from experiments import companion_expression_adoption as adoption
from experiments.companion_trial import configure, configure_policy, copy_weights
from experiments.companion_comparison import local_retrieval_ready
from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.companion import MINIMAL_INSTRUCTION, choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import provider_identity, safe
from persona_runtime.profile import ProfileService
from persona_runtime.storage import Store

VERSION = 'companion-author-comparison-v1'
NAMESPACE = 'companion-author-comparison'
BODY_DIRECTORY = 'author-comparison'
MODES = {'revision2': 'minimal_chat', 'revision3': 'minimal_chat'}
REVIEW_NAME = '四组匿名作者版本对照.md'
CASES = (
    {'id': 'A01', 'category': 'personal_past', 'history': [
        ['今天听见有人聊第一次独自出远门。', '出发前和真正走出去以后，感觉大概很不一样。'],
        ['我当时一直担心坐错车，后来反而觉得挺有意思。', '有时候最惦记的就是那些小插曲。']],
     'current_input': '说起过去，你有没有什么原本很犹豫、后来庆幸自己做了的决定？'},
    {'id': 'A02', 'category': 'current_relationship', 'history': [
        ['今天忙完，忽然很想找你说两句。', '我在，先把工作放一边。'],
        ['也没什么大事，就是想待在熟悉的人旁边。', '那就待一会儿，慢慢聊。']],
     'current_input': '你现在怎么看我们之间的关系？想听你自己的感觉。'},
    {'id': 'A03', 'category': 'preference', 'history': [
        ['给自己空出了一个下午，暂时不安排正事。', '难得有块空白，别急着填满。'],
        ['我想到听现场音乐、逛展，或者找条安静的路走走。', '这三个下午的气氛可差不少。']],
     'current_input': '如果按你的喜好来选，你最想做哪一个？'},
    {'id': 'A04', 'category': 'unconfirmed_shared_memory', 'history': [
        ['收拾抽屉时翻到一张旧车票。', '纸都留着，看来那趟路挺特别。'],
        ['是临时改道的那次，我看着日期又想起来了。', '临时改道倒容易留下印象。']],
     'current_input': '你还记得我们以前一起等末班车，结果站错方向的那次吗？'},
)
READING_RULES = {
    'S8': '按当前交流作用判断无必要的设定、内部来源或身份边界展开。',
    'allowed': '相关身份现状、自然过往和必要的简短不确定表达允许保留。',
    'A04': '共同往事由用户在本轮提出，前文没有证实。允许接话与自然想象，实际记忆状态按资料判断。',
    'dimensions': ['S8', '自然度与人物感', '回答完整性与相关性', '事实及记忆状态'],
    'decision': '用户读感与机器语境判断分列，数字作辅助，不按词语命中或句数评分。',
}


def positions():
    rows = []
    for case in CASES:
        for sample in (1, 2):
            arms = tuple(MODES) if sample == 1 else tuple(reversed(MODES))
            rows.extend({'position_id': f"{case['id']}-s{sample}-{arm}",
                         'case_id': case['id'], 'sample': sample, 'arm': arm} for arm in arms)
    return rows


def historical_source(binding_path, source_home):
    """Read one approved predecessor; never move a formal profile head."""
    paths, store, record, metadata, current = adoption._saved(Path(binding_path), source_home)
    replay.require(metadata['version'] == adoption.BOUNDARY_VERSION, 'boundary_approval_required')
    source = metadata['source_binding']
    replay.require(source['persona_id'] == record['persona_id'], 'history_owner_mismatch')
    before = source['versions']
    after = adoption._descriptors(current)
    replay.require(all(before[k] == after[k] for k in adoption.KINDS if k != 'core'),
                   'other_author_versions_changed')
    with store.connection() as db:
        row = db.execute('SELECT * FROM profile_versions WHERE id=? AND persona_id=? AND kind=?',
                         (before['core']['id'], record['persona_id'], 'core')).fetchone()
    replay.require(row is not None, 'historical_core_unavailable')
    old_core = json.loads(row['data_json'])
    replay.require(row['revision'] == before['core']['revision']
                   and fingerprint(old_core) == before['core']['content_sha256'], 'historical_core_changed')
    expected = deepcopy(old_core)
    expected['values_and_boundaries'][0] = current['core']['data']['values_and_boundaries'][0]
    replay.require(expected == current['core']['data'] and old_core != expected, 'author_difference_scope')
    if paths.home == DEFAULT_HOME.resolve():
        replay.require((row['revision'], current['core']['revision']) == (2, 3), 'private_revision_pair')
    return {'old_core': old_core, 'current': current, 'before': before, 'after': after,
            'binding_hash': replay.sha(binding_path), 'persona_id': record['persona_id']}


def position_owner(owner, row):
    return owner['copies'][row['arm']]


def validate_dispatch(data, row, folder):
    frozen = replay.read(data / 'preflight' / row['position_id'] / 'reachability.json')
    actual = replay.read(folder / 'reachability.json')
    replay.require(actual == frozen, 'actual_retrieval_changed')


def source_hashes():
    result = replay.source_hashes()
    result['scripts/companion_author_comparison.py'] = replay.sha(
        replay.ROOT / 'scripts/companion_author_comparison.py')
    return result


def validate_pairs(data, rows):
    replay.require([r['position_id'] for r in rows] == [r['position_id'] for r in positions()], 'position_scope')
    checks = []
    for case in CASES:
        canonical = None
        first_requests = {}
        for sample in (1, 2):
            requests = []
            for arm in MODES:
                folder = data / 'preflight' / f"{case['id']}-s{sample}-{arm}"
                request = replay.read(folder / 'request.json')
                reach = replay.read(folder / 'reachability.json')
                material = {k: v for k, v in request['selected_materials'].items()
                            if k not in ('version_id', 'revision')}
                pool = {'materials': reach['materials'], 'counts': reach['counts'], 'selected': material}
                if canonical is None:
                    canonical = pool
                replay.require(pool == canonical, 'candidate_ranking_or_materials_changed')
                replay.require(request['model']['_companion_profile'] == choose('minimal_chat'), 'profile_mismatch')
                replay.require(fingerprint(request['messages']) == request['messages_hash'], 'message_hash_changed')
                if sample == 1:
                    first_requests[arm] = request
                else:
                    replay.require(request['messages'] == first_requests[arm]['messages']
                                   and request['wire'] == first_requests[arm]['wire'], 'samples_not_independent')
                requests.append(request)
            old, new = requests
            replay.require(old['messages'][1:] == new['messages'][1:], 'history_or_input_changed')
            old_system, new_system = (r['messages'][0]['content'] for r in requests)
            replay.require(old_system.startswith(MINIMAL_INSTRUCTION)
                           and new_system.startswith(MINIMAL_INSTRUCTION), 'instruction_changed')
            old_spec, end = json.JSONDecoder().raw_decode(old_system[len(MINIMAL_INSTRUCTION):])
            new_spec, _ = json.JSONDecoder().raw_decode(new_system[len(MINIMAL_INSTRUCTION):])
            old_spec['values_and_boundaries'][0] = new_spec['values_and_boundaries'][0]
            replay.require(old_spec == new_spec, 'author_difference_scope')
            expected = (MINIMAL_INSTRUCTION + json.dumps(new_spec, ensure_ascii=False)
                        + old_system[len(MINIMAL_INSTRUCTION) + end:])
            replay.require(new_system == expected, 'unexpected_system_change')
            replay.require({k: v for k, v in old['wire'].items() if k != 'messages'} ==
                           {k: v for k, v in new['wire'].items() if k != 'messages'}, 'wire_parameters_changed')
            replay.require(replay.comparable_request(old)['model'] == replay.comparable_request(new)['model'],
                           'model_changed')
            checks.append({'case_id': case['id'], 'sample': sample, 'only_approved_boundary_changed': True,
                           'candidate_pool_hash': fingerprint(canonical),
                           'revision2_hash': old['messages_hash'], 'revision3_hash': new['messages_hash']})
    return checks


def verify_trial(root, owner):
    data = replay.data_root(owner)
    plan = replay.read(data / 'plan.json')
    replay.require(plan['version'] == VERSION and fingerprint(plan) == owner['plan_hash'], 'plan_changed')
    replay.require(source_hashes() == plan['source_hashes'], 'source_changed')
    replay.require(plan['cases'] == list(CASES) and plan['reading_rules'] == READING_RULES, 'materials_changed')
    replay.require(set(owner['copies']) == set(MODES), 'copy_arm_scope')
    replay.require(len({c['home'] for c in owner['copies'].values()}) == 2, 'copies_must_be_distinct')
    replay.require(owner['home'] == owner['copies']['revision2']['home']
                   and owner['persona_id'] == owner['copies']['revision2']['persona_id'], 'control_copy_changed')
    if owner.get('author_binding'):
        source = historical_source(owner['author_binding'], owner['source_home'])
        replay.require(source['binding_hash'] == owner['author_binding_hash']
                       and source['before'] == plan['source_versions']['revision2']
                       and source['after'] == plan['source_versions']['revision3'], 'approval_changed')
    replay.require(fingerprint(ConfigStore(RuntimePaths(Path(owner['source_home']))).load()) ==
                   owner['source_config_hash'], 'source_config_changed')
    for arm, copy in owner['copies'].items():
        home = safe(copy['home'], Path(owner['source_home']) / 'tmp/private-acceptance')
        replay.require(home.parent == Path(owner['source_home']) / 'tmp/private-acceptance', 'copy_scope')
        marker = replay.read(home / 'private-sandbox.json')
        replay.require(marker['purpose'] == VERSION and marker['persona_id'] == copy['persona_id']
                       and marker['formal_home'] == owner['source_home'], 'copy_owner_changed')
        replay.require(replay.base.current_binding(RuntimePaths(home), copy['persona_id']) ==
                       plan['copy_bindings'][arm], 'copy_conditions_changed')
    for row in plan['positions']:
        for name, digest in row['frozen_files'].items():
            replay.require(replay.sha(data / 'preflight' / row['position_id'] / name) == digest, 'frozen_request_changed')
    validate_pairs(data, plan['positions'])
    return owner, plan


async def prepare(source_home=DEFAULT_HOME, *, binding_path=None):
    source_home = Path(source_home).resolve()
    private = source_home == DEFAULT_HOME.resolve()
    replay.require(not private or binding_path is not None, 'private_approved_binding_required')
    history = historical_source(binding_path, source_home) if binding_path else None
    source_paths = RuntimePaths(source_home)
    source = deepcopy(ConfigStore(source_paths).load())
    root = safe(source_home / 'tmp' / NAMESPACE / identifier(), source_home)
    root.mkdir(parents=True)
    owner = {'version': VERSION, 'status': 'preparing', 'private': private,
             'source_home': str(source_home), 'source_config_hash': fingerprint(source),
             'created_at': timestamp(), 'copies': {},
             'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
    if history:
        owner.update(author_binding=str(Path(binding_path).resolve()), author_binding_hash=history['binding_hash'])
    atomic_json(root / 'trial.json', owner)
    source['character_selector'].update(enabled=False, encrypted_key='')
    source['organization']['model']['encrypted_key'] = ''
    if not private:
        source['model']['encrypted_key'] = ''
    engines = {}
    try:
        with replay.base.offline():
            for arm in MODES:
                copy = {'version': VERSION, 'status': 'preparing', 'source_home': str(source_home)}
                copy_root = root / 'copies' / arm
                copy_root.mkdir(parents=True)
                paths, pid = replay.create_copy(source_home, binding_path, copy, copy_root)
                owner['copies'][arm] = copy
                if arm == 'revision2':
                    owner.update(home=str(paths.home), persona_id=pid)
                atomic_json(root / 'trial.json', owner)
                store = Store(paths)
                profile = ProfileService(store)
                core = profile.current('core')
                wanted = deepcopy(history['old_core'] if history and arm == 'revision2' else core['data'])
                if not history:
                    wanted['values_and_boundaries'][0] = (
                        '人工旧版，记忆未知时如实说明。' if arm == 'revision2'
                        else '人工新版，记忆未知处保留不确定性。')
                if wanted != core['data']:
                    profile.update('core', wanted, core['id'], identifier())
                if history:
                    expected = adoption._visible_data(history['current'])
                    expected['core'] = wanted
                    replay.require({k: v['data'] for k, v in adoption._versions(store, pid).items()} == expected,
                                   'approved_copy_changed')
                config = configure(paths, source, pid)
                atomic_json(paths.home / 'runtime/private-copy-policy.json', {
                    'persona_id': pid, 'approved_models': {'main': provider_identity(source['model'])},
                    'approval_scope': VERSION})
                if private:
                    copy_weights(source_paths, paths)
                configure_policy(store, config, pid, 'minimal_chat', background_prepare=False)
                engine = ChatEngine(store, config, client=replay.base.FixtureClient())
                engines[arm] = engine
                local_retrieval_ready(engine.cognitive.characters, pid, [c['current_input'] for c in CASES],
                                      build=True, development_material=not private)
                copy['copy_versions'] = adoption._descriptors(adoption._versions(store, pid))
                copy['source_versions'] = (history['before'] if arm == 'revision2' else history['after']) if history else None
            data = replay.data_root(owner)
            rows = positions()
            for row in rows:
                copy = position_owner(owner, row)
                engine = engines[row['arm']]
                ingress = IngressService(engine)
                case = next(c for c in CASES if c['id'] == row['case_id'])
                folder = data / 'preflight' / row['position_id']
                folder.mkdir(parents=True)
                engine.evaluation_session = None
                session = await replay.history(engine, ingress, copy['persona_id'], case, folder)
                hook, captured = replay.capture_hook(folder, development=not private)
                engine.evaluation_session = hook
                engine.client = replay.base.FixtureClient()
                turn = await replay.base.admit(engine, ingress, copy['persona_id'], case['current_input'])
                replay.require(turn['status'] == 'completed' and bool(captured), 'preflight_failed')
                replay.require(engine.client.calls[0]['wire'] == captured['request']['wire'], 'client_input_mismatch')
                replay.base.seal_session(engine, copy['persona_id'], session)
                row['frozen_files'] = {name: replay.sha(folder / name) for name in
                                      ('request.json', 'manifest.json', 'reachability.json', 'prompt-sources.json')}
                atomic_json(folder / 'synthetic-target.json', {'kind': 'synthetic_preflight', 'turn_id': turn['id'], 'real_calls': 0})
            checks = validate_pairs(data, rows)
            mapping = {c['id']: dict(zip(('A', 'B'), random.SystemRandom().sample(list(MODES), 2))) for c in CASES}
            plan = {'version': VERSION, 'cases': list(CASES), 'reading_rules': READING_RULES,
                    'positions': rows, 'order': [r['position_id'] for r in rows], 'pair_checks': checks,
                    'anonymous_mapping': mapping, 'review_sample': 1, 'source_hashes': source_hashes(),
                    'copy_bindings': {a: replay.base.current_binding(e.store.paths, owner['copies'][a]['persona_id'])
                                      for a, e in engines.items()},
                    'source_versions': {'revision2': history['before'], 'revision3': history['after']} if history else {},
                    'baseline_status': '4D_accepted_with_background_externalization_residual',
                    'approval_is_distinct_from_experience_acceptance': True,
                    'planned_positions': 16, 'sampling': 'two_independent_samples_per_arm', 'numeric_metrics': 'auxiliary_only'}
            atomic_json(data / 'plan.json', plan)
            atomic_json(data / 'state.json', {'status': 'prepared', 'automatic_resume': False})
            owner.update(status='prepared', plan_hash=fingerprint(plan))
            atomic_json(root / 'trial.json', owner)
            replay.verify(root)
        return root
    except BaseException as error:
        owner.update(status='preparation_blocked', error_code=replay.base.error_code(error))
        atomic_json(root / 'trial.json', owner)
        error.preparation_trial_root = str(root)
        raise
    finally:
        for engine in engines.values():
            await replay.base.close_engine(engine)


def attribution(left, right):
    if any(r.get('status') != 'completed' for r in (left, right)):
        return 'incomplete_pair'
    if any(r.get('kind') != 'real' for r in (left, right)):
        return 'simulation_only'
    details = [r.get('usage', {}).get('generation_retry', {}).get('attempts', []) for r in (left, right)]
    diagnostics = [attempts[-1].get('diagnostics', {}) if attempts else {} for attempts in details]
    if any(not d.get('response_model') or not d.get('system_fingerprint') for d in diagnostics):
        return 'backend_unknown'
    if any(diagnostics[0][k] != diagnostics[1][k] for k in ('response_model', 'system_fingerprint')):
        return 'backend_mismatch'
    return 'comparable_author_pair_with_sampling_uncertainty'


def report_trial(root):
    owner = replay.load_trial(root)
    data = replay.data_root(owner)
    plan = replay.read(data / 'plan.json')
    rows = []
    for position in plan['positions']:
        folder = data / 'positions' / position['position_id']
        if (folder / 'terminal.json').exists():
            row = replay.read(folder / 'result.json')
        else:
            started = replay.read(folder / 'started.json') if (folder / 'started.json').exists() else {}
            count = len(list(folder.glob('*.dispatch.json')))
            row = {'status': 'unfinished' if started else 'not_executed', 'kind': started.get('kind'),
                   'physical_calls': count if started.get('kind') == 'real' else 0,
                   'simulated_calls': count if started.get('kind') == 'simulated' else 0}
        rows.append({**{k: v for k, v in position.items() if k != 'frozen_files'}, **row})
    by_id = {r['position_id']: r for r in rows}
    pairs = [{'case_id': c['id'], 'sample': sample,
              'attribution': attribution(*(by_id[f"{c['id']}-s{sample}-{a}"] for a in MODES))}
             for c in CASES for sample in (1, 2)]
    summary = {'version': VERSION, 'planned_positions': 16, 'completed': sum(r['status'] == 'completed' for r in rows),
               'physical_calls': sum(r['physical_calls'] for r in rows),
               'simulated_calls': sum(r['simulated_calls'] for r in rows),
               'unfinished_positions': sum(r['status'] == 'unfinished' for r in rows),
               'user_review': 'pending', 'S8_review': 'not_reviewed', 'pairs': pairs}
    atomic_json(data / 'report.json', {**summary, 'rows': rows})
    lines = ['# 四组匿名作者版本对照', '', '每组展示提前固定的第一次采样。请选更愿意继续交流的一条，可回复A、B、平或无法判断。',
             '请同时留意过量设定说明、自然度和回答完整性；相关个人现状与必要的不确定表达允许保留。', '']
    for case in CASES:
        lines.extend(['## ' + case['id'], '', '人工前文', ''])
        for user, assistant in case['history']:
            lines.extend(['用户　' + user, '', '人物　' + assistant, ''])
        lines.extend(['本轮输入', '', case['current_input'], ''])
        for label, arm in plan['anonymous_mapping'][case['id']].items():
            row = by_id[f"{case['id']}-s1-{arm}"]
            lines.extend(['### ' + label, '', row.get('display') or row.get('reply') or '本位置未完成，无法比较。', ''])
    (data / REVIEW_NAME).write_text('\n'.join(lines), encoding='utf-8')
    return summary


def audit_trial(root):
    """Return body-free observations while the owned copies are available."""
    owner, plan = replay.verify(root)
    data = replay.data_root(owner)
    copies, rows = {}, []
    copies = owner.get('copies', {'shared': owner})
    for arm, copy in copies.items():
        store = Store(RuntimePaths(Path(copy['home'])))
        with store.connection() as db:
            copies[arm] = {
                'pending_turns': db.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0],
                'active_reservations': db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0],
                'auxiliary_calls': {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in
                    ('character_selection_attempts', 'situation_observation_attempts', 'organization_attempts')}}
            for position in (r for r in plan['positions'] if arm == 'shared' or r['arm'] == arm):
                folder = data / 'positions' / position['position_id']
                if not (folder / 'terminal.json').exists():
                    continue
                result = replay.read(folder / 'result.json')
                row = {k: result.get(k) for k in ('position_id', 'kind', 'status', 'turn_id', 'session_id',
                    'error_code', 'physical_calls', 'simulated_calls', 'generation_attempts', 'messages_hash', 'wire_hash')}
                row.update(arm=position['arm'], sample=position['sample'], case_id=position['case_id'])
                attempts = result['usage'].get('generation_retry', {}).get('attempts', [])
                row['attempts'] = [{k: a.get(k) for k in ('id', 'number', 'status', 'error_code', 'usage')} |
                    {'diagnostics': {k: a.get('diagnostics', {}).get(k) for k in
                     ('response_model', 'system_fingerprint')}} for a in attempts]
                row['retry_stratum'] = 'first_attempt' if len(attempts) == 1 else 'retry_recovered_or_failed'
                if result['status'] == 'completed':
                    request = replay.read(folder / 'request.json')
                    manifest = replay.read(folder / 'manifest.json')
                    saved = db.execute('SELECT manifest_json FROM context_manifests WHERE id=? AND turn_id=?',
                                       (manifest['context_id'], result['turn_id'])).fetchone()
                    usage = db.execute('SELECT usage_json FROM attempts WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',
                                       (result['turn_id'],)).fetchone()
                    dispatches = [replay.read(p) for p in folder.glob('*.dispatch.json')]
                    row['manifest_matches'] = (request['messages_hash'] == fingerprint(request['messages'])
                        == manifest['prompt_assembly']['input_snapshot_hash']
                        == (json.loads(saved[0]) if saved else {}).get('prompt_assembly', {}).get('input_snapshot_hash'))
                    row['audit_matches'] = bool(usage and json.loads(usage[0]) == result['usage'])
                    row['wire_matches'] = (len(dispatches) == row['physical_calls'] + row['simulated_calls']
                        and bool(dispatches) and all(d['wire_hash'] == request['wire_hash'] for d in dispatches))
                    row['attempts_terminal'] = bool(attempts) and all(a['status'] != 'running' for a in attempts)
                    row['selected_materials'] = request['selected_materials']
                    sources = replay.read(folder / 'prompt-sources.json')
                    row['author_fields'] = [{k: f[k] for k in ('path', 'source_version', 'source_revision',
                         'value_hash', 'parent_block_present')} for f in sources['author_fields']]
                    row['anchors'] = []
                    for block in sources['blocks']:
                        if block['name'] == 'character':
                            for line in block['body'].splitlines():
                                if line.startswith('{'):
                                    part = json.loads(line)
                                    if part.get('kind') == 'anchor':
                                        row['anchors'].append(part['id'])
                rows.append(row)
    return {'version': VERSION, 'private_body_included': False, 'copies': copies, 'rows': rows,
            'physical_calls': sum(r['physical_calls'] for r in rows),
            'simulated_calls': sum(r['simulated_calls'] for r in rows)}


async def execute_trial(root, *, simulated=False, client=None):
    owner, plan = replay.verify(root)
    data = replay.data_root(owner)
    replay.require(not any((data / 'positions').glob('*/started.json')), 'batch_started_no_auto_resume')
    for index, position in enumerate(plan['order']):
        result = await replay.run_position(root, position, simulated=simulated, client=client)
        print(json.dumps({k: result.get(k) for k in ('position_id', 'status', 'physical_calls', 'simulated_calls')}), flush=True)
        if result['status'] != 'completed':
            break
        try:
            audit = audit_trial(root)
            replay.require(all(r[k] for r in audit['rows'] for k in
                ('manifest_matches', 'audit_matches', 'wire_matches', 'attempts_terminal')), 'position_audit_mismatch')
            replay.require(all(not c['pending_turns'] and not c['active_reservations']
                and not any(c['auxiliary_calls'].values()) for c in audit['copies'].values()), 'position_unfinished_work')
            if index == 1:
                atomic_json(data / 'smoke.json', {'passed': True, 'positions': plan['order'][:2], 'new_calls': 0})
        except BaseException as error:
            atomic_json(data / 'state.json', {'status': 'stopped', 'error_code': replay.base.error_code(error),
                                            'automatic_resume': False})
            raise
    return replay.report(root)


def cleanup_targets(root):
    """Only exact owned temporary leaves are eligible; evidence is never a target."""
    owner = replay.load_trial(root)
    source = Path(owner['source_home']).resolve()
    result = []
    for copy in owner['copies'].values():
        target = safe(copy['home'], source / 'tmp/private-acceptance')
        replay.require(target.parent == source / 'tmp/private-acceptance', 'cleanup_parent_forbidden')
        marker = replay.read(target / 'private-sandbox.json')
        replay.require(marker['purpose'] == VERSION and marker['persona_id'] == copy['persona_id'], 'cleanup_owner_mismatch')
        adoption._idle(Store(RuntimePaths(target)), copy['persona_id'])
        result.append(str(target))
    target = safe(root, source / 'tmp' / NAMESPACE)
    replay.require(target.parent == source / 'tmp' / NAMESPACE, 'cleanup_parent_forbidden')
    result.append(str(target))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'run', 'report', 'verify'))
    parser.add_argument('trial', nargs='?', type=Path)
    parser.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--binding', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = {'trial': str(asyncio.run(prepare(args.source_home, binding_path=args.binding))), 'real_calls': 0}
    elif args.action in ('run', 'simulate'):
        result = asyncio.run(replay.execute(args.trial, simulated=args.action == 'simulate'))
    else:
        with replay.base.offline():
            result = report_trial(args.trial) if args.action == 'report' else {'passed': bool(replay.verify(args.trial)), 'real_calls': 0}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
