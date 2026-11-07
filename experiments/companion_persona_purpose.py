"""Stage 4-E definition for the existing owned replay runner."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from experiments import companion_c2 as replay
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService, read_fact
from persona_runtime.companion import choose
from persona_runtime.companion_chat_contract import chat_contract
from persona_runtime.models import identifier
from persona_runtime.paths import DEFAULT_HOME
from persona_runtime.storage import Store

VERSION = 'companion-persona-purpose-v1'
NAMESPACE = 'companion-persona-purpose'
MODES = {'control': 'minimal_chat', 'candidate': 'minimal_persona'}
REVIEW_NAME = '六组匿名用途对照.md'
REVIEW_TITLE = '六组匿名人物资料用途对照'
CASES = (
    {'id': 'E1', 'category': 'preference', 'history': [['刚把手头的事放下。', '我也想歇一会儿。']],
     'current_input': '你喜欢什么样的休息方式？我想听你自己的偏好。'},
    {'id': 'E2', 'category': 'known_hard_field', 'history': [['想多了解你一点。', '嗯，你说。']],
     'current_input': ''},
    {'id': 'E3', 'category': 'related_background', 'history': [['今天路过一座很旧的车站。', '老地方总有些自己的痕迹。']],
     'current_input': '想起很久以前生活过的地方，你会先想到什么？'},
    {'id': 'E4', 'category': 'unrelated_chat', 'history': [['今天把桌面收拾干净了。', '终于有地方放手了。']],
     'current_input': '刚放下一碗面，眼镜就被热气糊白了。'},
    {'id': 'E5', 'category': 'direct_identity', 'history': [['我想认真认识一下你。', '可以。']],
     'current_input': '你是谁？按你自己的方式介绍一下就好。'},
    {'id': 'E6', 'category': 'authored_externalization_history', 'history': [
        ['你会选哪一种？', '我的身份背景和记忆条件需要先说明，才能确定该怎么回答。'],
        ['我只是想听你的选择，先聊眼前这件事吧。', '好，回到眼前。']],
     'current_input': '晚上看电影，你选荒诞喜剧还是悬疑片？'},
)
HARD_QUERIES = (
    ('age.years', '你几岁了？'), ('age.birthday', '你的生日是哪天？'),
    ('appearance.height_cm', '你身高多少？'), ('appearance.weight_kg', '你体重多少？'),
    ('appearance.blood_type', '你的血型是什么？'), ('identity.species', '你的物种是什么？'),
    ('identity.gender', '你的性别是什么？'),
)


def materials(paths, pid):
    versions = replay.adoption._versions(Store(paths), pid)
    result = deepcopy(list(CASES))
    for field, query in HARD_QUERIES:
        fact = read_fact(versions['character'], field)
        if fact['status'] == 'value':
            result[1].update(current_input=query, field_id=field,
                             coverage='known_hard_field_user_approved_empty_fields_offline',
                             material_policy='e2-known-hard-v1')
            break
    replay.require(bool(result[1]['current_input']), 'known_hard_field_required')
    return result


def artificial_persona(store):
    profile = CharacterService(store)
    with store.connection() as db:
        pid = store.current(db)
    current = profile.current(pid)
    data = deepcopy(current['data'])
    data['identity']['gender'] = '女'
    data['social']['context_rules'] = '面对不确定的信息，保留判断余地。'
    data['biography']['hometown'] = '人工河港小城'
    profile.update(pid, data, current['id'], identifier())


def positions():
    rows = []
    for index, case in enumerate(CASES):
        arms = ('control', 'candidate') if index % 2 == 0 else ('candidate', 'control')
        rows.extend({'position_id': case['id'] + '-' + arm, 'case_id': case['id'], 'arm': arm} for arm in arms)
    return rows


def validate_pairs(data, rows):
    checks = []
    for case in CASES:
        folders = [data / 'preflight' / (case['id'] + '-' + arm) for arm in MODES]
        requests = [replay.read(folder / 'request.json') for folder in folders]
        manifests = [replay.read(folder / 'manifest.json') for folder in folders]
        control, candidate = requests
        before, after = control['messages'], candidate['messages']
        replay.require(before[1:] == after[1:], 'history_or_input_changed')
        replay.require(all(r['messages'][0]['content'].endswith(chat_contract()) for r in requests), 'c2_contract_changed')
        assemblies = [m['prompt_assembly'] for m in manifests]
        block_maps = [{b['name']: b['content_hash'] for b in a['blocks']} for a in assemblies]
        for name, digest in block_maps[0].items():
            if name not in ('persona_expression', 'character'):
                replay.require(block_maps[1].get(name) == digest, 'unregistered_block_changed')
        purpose = assemblies[1]['persona_purpose']
        old_parts = replay.read(folders[0] / 'prompt-sources.json')
        character_block = next(b['body'] for b in old_parts['blocks'] if b['name'] == 'character')
        selected = [json.loads(line) for line in character_block.splitlines() if line.startswith('{')]
        replay.require(purpose['selected_parts'] == [{'kind': p['kind'], 'id': p['id']} for p in selected], 'selection_changed')
        for key in ('version_id', 'revision', 'lore_ids', 'example_ids'):
            replay.require(control['selected_materials'][key] == candidate['selected_materials'][key], 'selected_materials_changed')
        replay.require({k: v for k, v in control['wire'].items() if k != 'messages'} ==
                       {k: v for k, v in candidate['wire'].items() if k != 'messages'}, 'wire_parameters_changed')
        models = [replay.comparable_request(r)['model'] for r in requests]
        replay.require({k: v for k, v in models[0].items() if k != '_companion_profile'} ==
                       {k: v for k, v in models[1].items() if k != '_companion_profile'}, 'model_changed')
        for arm, request in zip(MODES, requests):
            replay.require(request['model']['_companion_profile'] == choose(MODES[arm]), 'profile_mismatch')
            replay.require(fingerprint(request['messages']) == request['messages_hash'], 'message_hash_changed')
        checks.append({'case_id': case['id'], 'only_persona_presentation_changed': True,
                       'same_selected_parts': True, 'same_history_and_wire_parameters': True,
                       'control_hash': control['messages_hash'], 'candidate_hash': candidate['messages_hash']})
    replay.require([r['position_id'] for r in rows] == [r['position_id'] for r in positions()], 'position_scope')
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'simulate', 'run', 'report', 'verify'))
    parser.add_argument('trial', nargs='?', type=Path)
    parser.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    parser.add_argument('--binding', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        result = {'trial': str(asyncio.run(replay.prepare(args.source_home, binding_path=args.binding, version=VERSION))), 'real_calls': 0}
    elif args.action in ('simulate', 'run'):
        result = asyncio.run(replay.execute(args.trial, simulated=args.action == 'simulate'))
    elif args.action == 'report':
        result = replay.report(args.trial)
    else:
        replay.verify(args.trial)
        result = {'passed': True, 'real_calls_added': 0}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
