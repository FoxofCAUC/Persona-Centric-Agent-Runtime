"""Owned C2 comparison of one post-budget character-field consumer."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from experiments import companion_c2 as replay
from experiments import companion_author_comparison as author
from experiments.companion_persona_purpose import artificial_persona
from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose
from persona_runtime.companion_consumption import consume, FIELD
from persona_runtime.config import atomic_json
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.storage import Store

VERSION = 'companion-context-rules-v1'
NAMESPACE = 'companion-context-rules'
BODY_DIRECTORY = 'context-rules-comparison'
MODES = {'control': 'minimal_chat', 'candidate': 'minimal_context_rules_off'}
REVIEW_NAME = '十二组匿名情境规则对照.md'
CASES = author.CASES + (
    {'id': 'A05', 'category': 'ordinary_chat', 'history': [
        ['今天事情总算告一段落了。', '这会儿总算能坐一下了。'],
        ['我刚下楼买了点吃的。', '吃上就好。']],
     'current_input': '店员多塞了一只小面包，今天运气还挺好。'},
    {'id': 'A06', 'category': 'direct_identity', 'history': [
        ['今天在整理常用聊天窗口。', '把常用的留前面，找起来省事。'],
        ['看到你的名字，突然有点好奇。', '嗯？']],
     'current_input': '你是谁？也想听听你的身份背景，你按自己的理解说就好。'},
)
execute_trial = author.execute_trial
audit_trial = author.audit_trial


def materials(paths, pid):
    return deepcopy(list(CASES))


def positions():
    rows = []
    for case in CASES:
        for sample in (1, 2):
            arms = tuple(MODES) if sample == 1 else tuple(reversed(MODES))
            rows.extend({'position_id':f"{case['id']}-s{sample}-{arm}", 'case_id':case['id'],
                         'sample':sample, 'arm':arm} for arm in arms)
    return rows


def verify_conditions(owner, plan):
    if owner['private']:
        replay.require(owner['approved_versions']['core']['revision'] == 3, 'author_revision3_required')


def rendered_parts(body):
    return [dict(kind=p['kind'], id=p['id'], text=line) for line in body.splitlines()
            if line.startswith('{') for p in [json.loads(line)]]


def validate_pairs(data, rows):
    replay.require([r['position_id'] for r in rows] == [r['position_id'] for r in positions()], 'position_scope')
    checks = []
    for case in CASES:
        first = {}
        pool = None
        for sample in (1, 2):
            requests, sources, manifests = [], [], []
            for arm in MODES:
                folder = data / 'preflight' / f"{case['id']}-s{sample}-{arm}"
                request = replay.read(folder / 'request.json')
                reach = replay.read(folder / 'reachability.json')
                observed = {'materials': reach['materials'], 'counts': reach['counts']}
                pool = observed if pool is None else pool
                replay.require(pool == observed, 'candidate_pool_changed')
                replay.require(request['model']['_companion_profile'] == choose(MODES[arm]), 'profile_mismatch')
                if sample == 1:
                    first[arm] = request
                else:
                    replay.require(request['messages'] == first[arm]['messages']
                                   and request['wire'] == first[arm]['wire'], 'samples_not_independent')
                requests.append(request)
                sources.append(replay.read(folder / 'prompt-sources.json'))
                manifests.append(replay.read(folder / 'manifest.json'))
            control, candidate = requests
            blocks = [{b['name']: b for b in s['blocks']} for s in sources]
            old = blocks[0]['character']
            kept, diagnostic = consume(rendered_parts(old['body']))
            expected_body = '\n人物补充资料：\n' + '\n'.join(p['text'] for p in kept) if kept else ''
            replay.require(blocks[1]['character']['body'] == expected_body, 'consumer_difference_invalid')
            for name, block in blocks[0].items():
                if name != 'character':
                    replay.require(blocks[1][name]['body'] == block['body'], 'other_block_changed')
            before, after = control['messages'], candidate['messages']
            start, count = old['start_character'], old['characters']
            replay.require(after[0]['content'] == before[0]['content'][:start] + expected_body
                           + before[0]['content'][start + count:] and before[1:] == after[1:], 'unregistered_input_change')
            material = deepcopy(control['selected_materials'])
            material['fact_paths'] = [p for p in material['fact_paths'] if p != FIELD]
            replay.require(candidate['selected_materials'] == material, 'other_material_changed')
            info = manifests[1]['prompt_assembly']['character_consumption']
            replay.require(info['budget_retained_parts'] == diagnostic['budget_retained_parts']
                           and info['included_parts'] == diagnostic['included_parts'], 'consumer_diagnostic_mismatch')
            replay.require(info['final_input_hash'] == candidate['messages_hash'] == fingerprint(after), 'input_hash_mismatch')
            replay.require({k:v for k,v in control['wire'].items() if k != 'messages'} ==
                           {k:v for k,v in candidate['wire'].items() if k != 'messages'}, 'wire_parameters_changed')
            models = [replay.comparable_request(r)['model'] for r in requests]
            replay.require({k:v for k,v in models[0].items() if k != '_companion_profile'} ==
                           {k:v for k,v in models[1].items() if k != '_companion_profile'}, 'model_parameters_changed')
            checks.append({'case_id':case['id'], 'sample':sample, 'only_registered_field_consumption_differs':True,
                           'actual_consumption_difference':before != after, 'consumer_status':info['status'],
                           'control_hash':control['messages_hash'], 'candidate_hash':candidate['messages_hash'],
                           'candidate_pool_hash':fingerprint(pool)})
    return checks


def validate_dispatch(data, row, folder):
    frozen = replay.read(data / 'preflight' / row['position_id'] / 'reachability.json')
    replay.require(replay.read(folder / 'reachability.json') == frozen, 'actual_retrieval_changed')


async def prepare(source_home=DEFAULT_HOME, *, binding_path=None):
    if Path(source_home).resolve() == DEFAULT_HOME.resolve():
        replay.require(binding_path is not None, 'approved_binding_required')
        _, _, _, _, versions = replay.adoption._saved(binding_path, source_home)
        replay.require(versions['core']['revision'] == 3, 'author_revision3_required')
    return await replay.prepare(source_home, binding_path=binding_path, version=VERSION)


def report_trial(root):
    owner = replay.load_trial(root)
    data = replay.data_root(owner)
    plan = replay.read(data / 'plan.json')
    rows = []
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
    lookup = {r['position_id']:r for r in rows}
    pairs = []
    lines = ['# 十二组匿名情境规则对照', '', '每组使用同一人物与人工前文。请按01—12选择A、B、平或无法判断，可附具体出戏处。',
             '相关身份现状与必要的不确定表达允许保留。请看自然度、人物感、回答相关性和继续交流意愿。', '']
    for index, check in enumerate(plan['pair_checks'], 1):
        case = next(c for c in plan['cases'] if c['id']==check['case_id'])
        sample = check['sample']
        pair = {'case_id':case['id'], 'sample':sample, 'review_id':f'{index:02d}',
                'actual_consumption_difference':check['actual_consumption_difference'],
                'attribution':author.attribution(*(lookup[f"{case['id']}-s{sample}-{a}"] for a in MODES))}
        if not check['actual_consumption_difference'] and pair['attribution'] != 'incomplete_pair':
            pair['attribution'] = 'same_main_request_sampling_only'
        pairs.append(pair)
        lines.extend([f'## {index:02d}', '', f"场景{case['id']}，独立采样{sample}", ''])
        for user, assistant in case['history']:
            lines.extend(['用户　'+user, '', '人物　'+assistant, ''])
        lines.extend(['本轮输入', '', case['current_input'], ''])
        for label, arm in plan['anonymous_mapping'][case['id']].items():
            row = lookup[f"{case['id']}-s{sample}-{arm}"]
            lines.extend(['### '+label, '', row.get('display') or row.get('reply') or '本位置未完成，无法比较。', ''])
    summary = {'version':VERSION, 'planned_positions':24, 'completed':sum(r['status']=='completed' for r in rows),
               'physical_calls':sum(r['physical_calls'] for r in rows), 'simulated_calls':sum(r['simulated_calls'] for r in rows),
               'unfinished_positions':sum(r['status']=='unfinished' for r in rows), 'pairs':pairs,
               'user_review':'pending_all12', 'real_effect':'not_settled'}
    atomic_json(data / 'report.json', {**summary, 'rows':rows})
    (data / REVIEW_NAME).write_text('\n'.join(lines), encoding='utf-8')
    return summary


def cleanup_targets(root):
    owner = replay.load_trial(root)
    home = safe(owner['home'], Path(owner['source_home']) / 'tmp/private-acceptance')
    replay.require(home.parent == Path(owner['source_home']) / 'tmp/private-acceptance', 'cleanup_scope')
    replay.adoption._idle(Store(RuntimePaths(home)), owner['persona_id'])
    return [str(home), str(Path(root).resolve())]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare','simulate','run','report','verify'))
    p.add_argument('trial', nargs='?', type=Path)
    p.add_argument('--source-home', type=Path, default=DEFAULT_HOME)
    p.add_argument('--binding', type=Path)
    args = p.parse_args()
    if args.action == 'prepare':
        result = {'trial':str(asyncio.run(prepare(args.source_home, binding_path=args.binding))), 'real_calls':0}
    elif args.action in ('run','simulate'):
        result = asyncio.run(replay.execute(args.trial, simulated=args.action=='simulate'))
    else:
        with replay.base.offline():
            result = report_trial(args.trial) if args.action=='report' else {'passed':bool(replay.verify(args.trial)), 'real_calls':0}
    print(json.dumps(result, ensure_ascii=False))
