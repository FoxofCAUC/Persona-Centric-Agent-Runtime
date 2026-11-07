"""Owned artificial paid comparison of the approved light prompt bundle."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random

from experiments import companion_c2 as replay
from experiments import companion_author_comparison as author
from experiments import companion_prompt_light_materials as fixture
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatEngine
from persona_runtime.companion import MINIMAL_INSTRUCTION, choose
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.companion_prompt_light import render_blocks
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import provider_identity, safe
from persona_runtime.profile import ProfileService
from persona_runtime.prompt_assembly import PromptBlock
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION = 'companion-prompt-light-v1'
NAMESPACE = 'companion-prompt-light'
BODY_DIRECTORY = 'prompt-light-comparison'
MODES = {'control': 'minimal_chat', 'candidate': 'minimal_experience'}
REVIEW_NAME = '六组匿名轻量提示对照.md'
REVIEW_TITLE = '六组匿名轻量提示对照'
CASES = fixture.CASES
execute_trial = author.execute_trial
audit_trial = author.audit_trial


def materials(paths=None, pid=None):
    return deepcopy(list(CASES))


def positions():
    return [{'position_id': case['id'] + '-' + arm, 'case_id': case['id'], 'arm': arm, 'sample': 1}
            for index, case in enumerate(CASES)
            for arm in (tuple(MODES) if index % 2 == 0 else tuple(reversed(MODES)))]


def validate_owner(root, owner, source):
    home = safe(owner['home'], root)
    replay.require(home == root / 'artificial-runtime', 'artificial_home_scope')
    replay.require(owner.get('private') is False and owner.get('material_source') == 'artificial'
                   and not owner.get('author_binding'), 'artificial_owner_scope')
    marker = replay.read(home / 'artificial-sandbox.json')
    replay.require(marker == {'purpose': VERSION, 'persona_id': owner['persona_id'],
                              'source_home': str(source), 'material_source': 'artificial'}, 'artificial_marker_changed')


def verify_execution(owner, simulated):
    expected = 'simulation_only' if simulated else 'real_main_only'
    replay.require(owner.get('generation_authorization') == expected, 'artificial_execution_not_authorized')
    replay.require(simulated or not owner.get('development'), 'development_real_forbidden')


def current_binding(owner):
    paths = RuntimePaths(Path(owner['home']))
    return {'author_versions': replay.adoption._descriptors(replay.adoption._versions(Store(paths), owner['persona_id'])),
            'config_hash': fingerprint(ConfigStore(paths).load()),
            'permission_hash': replay.sha(paths.home / 'runtime/private-copy-policy.json'),
            'owner_hash': replay.sha(paths.home / 'artificial-sandbox.json')}


def verify_trial(root, owner):
    data = replay.data_root(owner)
    plan = replay.read(data / 'plan.json')
    replay.require(fingerprint(plan) == owner['plan_hash'], 'plan_changed')
    replay.require(replay.source_hashes() == plan['source_hashes'], 'source_changed')
    replay.require(current_binding(owner) == plan['binding'], 'artificial_conditions_changed')
    replay.require(fingerprint(ConfigStore(RuntimePaths(Path(owner['source_home']))).load()) ==
                   owner['source_config_hash'], 'source_config_changed')
    replay.require(plan['cases'] == materials() and plan['order'] == [p['position_id'] for p in positions()], 'materials_changed')
    replay.require(owner['generation_authorization'] == plan['generation_authorization']
                   and owner['material_source'] == 'artificial', 'generation_scope_changed')
    if owner['generation_authorization'] == 'real_main_only':
        replay.require(not owner['development'] and plan['representative_hybrid'], 'real_hybrid_required')
    else:
        replay.require(owner['generation_authorization'] == 'simulation_only', 'unknown_generation_authorization')
    for row in plan['positions']:
        for name, digest in row['frozen_files'].items():
            replay.require(replay.sha(data / 'preflight' / row['position_id'] / name) == digest, 'frozen_request_changed')
    validate_pairs(data, plan['positions'])
    return owner, plan


def validate_pairs(data, rows):
    replay.require([{k: r[k] for k in ('position_id', 'case_id', 'arm', 'sample')} for r in rows] == positions(), 'position_scope')
    checks = []
    for case in CASES:
        folders = [data / 'preflight' / (case['id'] + '-' + arm) for arm in MODES]
        requests = [replay.read(f / 'request.json') for f in folders]
        sources = [replay.read(f / 'prompt-sources.json') for f in folders]
        old_blocks = [PromptBlock(b['name'], b['body'], b['owner'], b['version'])
                      for b in sources[0]['blocks'] if b['message_index'] == 0]
        expected = render_blocks(old_blocks, legacy_instruction=MINIMAL_INSTRUCTION, legacy_contract=chat_contract())
        actual = [b for b in sources[1]['blocks'] if b['message_index'] == 0]
        replay.require([b.name for b in expected] == [b['name'] for b in actual], 'block_order_changed')
        replay.require(all(b.content == a['body'] for b, a in zip(expected, actual)), 'unregistered_prompt_difference')
        before, after = (r['messages'] for r in requests)
        replay.require(before[1:] == after[1:] and after[0]['content'] == ''.join(b.content for b in expected), 'history_or_input_changed')
        replay.require(requests[0]['selected_materials'] == requests[1]['selected_materials'], 'materials_changed')
        reach = [replay.read(f / 'reachability.json') for f in folders]
        replay.require(all(reach[0][k] == reach[1][k] for k in ('materials', 'counts')), 'candidate_pool_changed')
        manifests = [replay.read(f / 'manifest.json') for f in folders]
        replay.require(manifests[0]['truncation_info'] == manifests[1]['truncation_info']
                       and manifests[0]['excluded_state_refs'] == manifests[1]['excluded_state_refs'], 'budget_decisions_changed')
        replay.require({k:v for k,v in requests[0]['wire'].items() if k != 'messages'} ==
                       {k:v for k,v in requests[1]['wire'].items() if k != 'messages'}, 'wire_parameters_changed')
        models = [replay.comparable_request(r)['model'] for r in requests]
        replay.require({k:v for k,v in models[0].items() if k != '_companion_profile'} ==
                       {k:v for k,v in models[1].items() if k != '_companion_profile'}, 'model_parameters_changed')
        for arm, request, manifest in zip(MODES, requests, manifests):
            replay.require(request['model']['_companion_profile'] == choose(MODES[arm]), 'profile_mismatch')
            replay.require(request['messages_hash'] == fingerprint(request['messages']) == manifest['input_snapshot_hash'], 'final_hash_mismatch')
        checks.append({'case_id': case['id'], 'sample': 1, 'only_registered_bundle_differs': True,
                       'changed_blocks': [b.name for b, old in zip(expected, old_blocks) if b.content != old.content],
                       'control_hash': requests[0]['messages_hash'], 'candidate_hash': requests[1]['messages_hash']})
    return checks


def validate_dispatch(data, row, folder):
    frozen = replay.read(data / 'preflight' / row['position_id'] / 'reachability.json')
    replay.require(replay.read(folder / 'reachability.json') == frozen, 'actual_retrieval_changed')


async def prepare(source_home=DEFAULT_HOME, *, real_authorized=False, development=False):
    replay.require(not (real_authorized and development), 'development_real_forbidden')
    source_home = safe(source_home)
    if development:
        replay.require(source_home != DEFAULT_HOME.resolve() and source_home.is_relative_to(DEFAULT_HOME / 'tmp'), 'development_source_scope')
    source_paths = RuntimePaths(source_home)
    original = ConfigStore(source_paths).load()
    source = deepcopy(original)
    source['character_selector'].update(enabled=False, encrypted_key='')
    source['organization']['model']['encrypted_key'] = ''
    if real_authorized:
        replay.require(bool(source['model']['encrypted_key']) and source['model']['name'] == 'grok-4.7', 'real_model_not_configured')
    else:
        source['model']['encrypted_key'] = ''
    root = safe(source_home / 'tmp' / NAMESPACE / identifier(), source_home)
    private_directory(source_home, root)
    paths = RuntimePaths(root / 'artificial-runtime')
    owner = {'version': VERSION, 'status': 'preparing', 'private': False, 'material_source': 'artificial',
             'generation_authorization': 'real_main_only' if real_authorized else 'simulation_only',
             'development': development, 'source_home': str(source_home), 'source_config_hash': fingerprint(original),
             'home': str(paths.home), 'created_at': timestamp(),
             'review_deadline': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
    atomic_json(root / 'trial.json', owner)
    engine = None
    try:
        with replay.base.offline():
            store = Store(paths); store.initialize()
            service = RuntimeService(store)
            pid = service.create_persona(PersonaSpec.from_dict(fixture.PERSONA))['id']
            service.use_persona(pid); owner['persona_id'] = pid
            profile = ProfileService(store); relationship = profile.current('relationship')
            profile.update('relationship', {'description': fixture.RELATIONSHIP}, relationship['id'], identifier())
            characters = CharacterService(store); current = characters.current(pid)
            character = deepcopy(current['data'])
            character['age'].update(policy='fixed', fixed_age=29)
            character['biography']['occupation'] = '修旧收音机'
            character['interests'].update(likes=['橙子汽水的柑橘味'], dislikes=['奶茶黏厚的口感'])
            characters.update(pid, character, current['id'], identifier())
            marker = {'purpose': VERSION, 'persona_id': pid, 'source_home': str(source_home), 'material_source': 'artificial'}
            atomic_json(paths.home / 'artificial-sandbox.json', marker)
            config = replay.configure(paths, source, pid)
            atomic_json(paths.home / 'runtime/private-copy-policy.json',
                        {'persona_id': pid, 'approved_models': {'main': provider_identity(source['model'])}, 'approval_scope': VERSION,
                         'material_source': 'artificial', 'generation_authorization': owner['generation_authorization']})
            with store.connection() as db:
                owner['initial_counts'] = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                                           for t in ('events','turns','attempts','memories','summaries')}
            replay.require(not any(owner['initial_counts'].values()), 'artificial_initial_state_not_empty')
            if not development:
                replay.copy_weights(source_paths, paths)
            replay.configure_policy(store, config, pid, MODES['control'], background_prepare=False)
            engine = ChatEngine(store, config, client=replay.base.FixtureClient())
            ingress = IngressService(engine)
            local = replay.local_retrieval_ready(engine.cognitive.characters, pid,
                      [c['current_input'] for c in CASES], build=True, development_material=development)
            data = replay.data_root(owner)
            rows = positions()
            for row in rows:
                case = next(c for c in CASES if c['id'] == row['case_id'])
                folder = data / 'preflight' / row['position_id']; folder.mkdir(parents=True)
                engine.evaluation_session = None
                replay.enable(engine, pid, row['arm'], version=VERSION)
                session = await replay.history(engine, ingress, pid, case, folder)
                hook, captured = replay.capture_hook(folder, development=development)
                engine.evaluation_session = hook; engine.client = replay.base.FixtureClient()
                turn = await replay.base.admit(engine, ingress, pid, case['current_input'])
                replay.require(turn['status'] == 'completed' and captured, 'preflight_failed')
                replay.require(engine.client.calls[0]['wire'] == captured['request']['wire'], 'client_input_mismatch')
                atomic_json(folder / 'synthetic-target.json', {'kind':'synthetic_preflight', 'turn_id':turn['id'], 'real_calls':0})
                replay.base.seal_session(engine, pid, session)
                row['frozen_files'] = {n: replay.sha(folder / n) for n in ('request.json','manifest.json','reachability.json','prompt-sources.json')}
            checks = validate_pairs(data, rows)
            plan = {'version': VERSION, 'cases': materials(), 'positions': rows,
                    'order': [r['position_id'] for r in rows], 'pair_checks': checks,
                    'anonymous_mapping': {c['id']: dict(zip(('A','B'), random.SystemRandom().sample(list(MODES),2))) for c in CASES},
                    'source_hashes': replay.source_hashes(), 'binding': current_binding(owner),
                    'generation_authorization': owner['generation_authorization'],
                    'representative_hybrid': local['representative_hybrid'], 'local_preparation': local,
                    'planned_positions': 12, 'sampling': 'one_per_arm_no_quality_retry',
                    'material_source_sha256': fixture.SOURCE_MATERIAL_SHA256, 'numeric_metrics':'auxiliary_only'}
            atomic_json(data / 'plan.json', plan); atomic_json(data / 'state.json', {'status':'prepared','automatic_resume':False})
            owner.update(status='prepared', plan_hash=fingerprint(plan)); atomic_json(root / 'trial.json', owner)
            replay.verify(root)
        return root
    except BaseException as error:
        owner.update(status='preparation_blocked', error_code=replay.base.error_code(error))
        atomic_json(root / 'trial.json', owner)
        error.preparation_trial_root = str(root)
        raise
    finally:
        if engine is not None:
            await replay.base.close_engine(engine)


def report_trial(root):
    owner = replay.load_trial(root); data = replay.data_root(owner)
    plan = replay.read(data / 'plan.json'); rows = []
    for p in plan['positions']:
        folder = data / 'positions' / p['position_id']
        if (folder / 'terminal.json').exists():
            result = replay.read(folder / 'result.json')
        else:
            started = replay.read(folder / 'started.json') if (folder / 'started.json').exists() else {}
            count = len(list(folder.glob('*.dispatch.json')))
            result = {'status':'unfinished' if started else 'not_executed', 'kind':started.get('kind'),
                      'physical_calls':count if started.get('kind')=='real' else 0,
                      'simulated_calls':count if started.get('kind')=='simulated' else 0}
        rows.append({**{k:v for k,v in p.items() if k!='frozen_files'}, **result})
    by_id = {r['position_id']:r for r in rows}
    pairs = [{'case_id':c['id'], 'attribution':author.attribution(*(by_id[c['id']+'-'+a] for a in MODES))}
             for c in CASES]
    for pair in pairs:
        if pair['attribution']=='comparable_author_pair_with_sampling_uncertainty':
            pair['attribution']='comparable_prompt_bundle_pair_with_sampling_uncertainty'
    summary = {'version':VERSION, 'planned_positions':12, 'completed':sum(r['status']=='completed' for r in rows),
               'physical_calls':sum(r['physical_calls'] for r in rows), 'simulated_calls':sum(r['simulated_calls'] for r in rows),
               'unfinished_positions':sum(r['status']=='unfinished' for r in rows), 'pairs':pairs,
               'material_source':'artificial', 'user_review':'pending', 'real_effect':'not_settled'}
    atomic_json(data / 'report.json', {**summary, 'rows':rows})
    lines = ['# '+REVIEW_TITLE, '', '人工人物岑禾，两臂使用相同资料和前文。以下为本轮实际输出。',
             '请逐组给更愿继续交流的A、B、平或无法判断，并可标出明显出戏的位置。',
             '以自然度、人物感和继续交流意愿为主；日常联想、补白和轻微叙述偏差结合实际读感判断。', '']
    for i,c in enumerate(CASES,1):
        lines += [f'## {i:02d}　{c["id"]}', '', '人工前文', '']
        for u,a in c['history']: lines += ['用户：'+u, '', '岑禾：'+a, '']
        lines += ['本轮用户：'+c['current_input'], '']
        for label,arm in plan['anonymous_mapping'][c['id']].items():
            row=by_id[c['id']+'-'+arm]
            lines += ['### '+label, '', (row.get('display') or row.get('reply') or '本位置未完成，无法比较。'), '']
    (data / REVIEW_NAME).write_text('\n'.join(lines), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','simulate','run','report','verify'))
    parser.add_argument('trial', nargs='?', type=Path)
    parser.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--execution', choices=('simulated','real'), default='simulated')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            root = asyncio.run(prepare(args.source_home, real_authorized=args.execution=='real'))
            result = {'trial':str(root), 'status':'prepared', 'physical_calls':0}
        else:
            replay.require(args.trial is not None, 'trial_required')
            if args.action in ('simulate','run'):
                result = asyncio.run(replay.execute(args.trial, simulated=args.action=='simulate'))
            else:
                with replay.base.offline():
                    result = report_trial(args.trial) if args.action=='report' else {'status':'verified','physical_calls':0}
                    if args.action=='verify': replay.verify(args.trial)
        print(json.dumps(result, ensure_ascii=False))
    except BaseException as error:
        print(json.dumps({'status':'blocked','error_code':replay.base.error_code(error),
                          'trial':getattr(error,'preparation_trial_root',None)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
