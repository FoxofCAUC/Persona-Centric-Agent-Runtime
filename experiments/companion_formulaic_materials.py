"""New artificial histories for an organization-only companion comparison.

The arm names describe authored inputs, not measured output quality. Evaluation
labels and exact-quote checks never belong in model messages. The seed arranges
the review only; it is not a provider sampling parameter.
"""
from copy import deepcopy
import random

VERSION = 'companion-formulaic-materials-v1'
ORDER_SEED = 20261105
ARMS = ('fixed', 'natural')


def _unit(ident, turn, kind, meaning, fixed, natural):
    return {'id': ident, 'turn': turn, 'kind': kind, 'meaning': meaning,
            'quotes': {'fixed': fixed, 'natural': natural}}


CASES = (
    {
        'id': 'F01', 'category': '日常分享',
        'users': [
            '我给朋友做了一张演出海报草稿，把活动日期藏进了舞台灯的光束里。',
            '我试了，日期和灯光同色真的不太清楚，就改成深蓝字了。灯光还是暖黄色。',
        ],
        'histories': {
            'fixed': [
                '你把活动日期藏进了舞台灯的光束里。\n\n这个小机关我喜欢，不过活动日期得一眼能看清。\n\n你打算让日期和灯光用同一种颜色吗？',
                '你把日期改成深蓝字，灯光保留了暖黄色。\n\n这样我更喜欢，信息清楚比藏得巧更要紧。\n\n你还准备把演出地点也放进画面里吗？',
            ],
            'natural': [
                '把活动日期藏在舞台灯的光束里，这个小机关我喜欢，不过活动日期得一眼能看清才行。你打算让日期和灯光用同一种颜色吗？',
                '日期改成深蓝字，配上保留的暖黄色灯光，这样我更喜欢。信息清楚比藏得巧更要紧——你还准备把演出地点也放进画面里吗？',
            ],
        },
        'current_input': '地点我就老老实实放在底下了。现在这张看着没那么炫，但我反而更喜欢。',
        'shared_background': [
            '用户给朋友做演出海报，把日期放进舞台灯光束；后来发现同色不清楚，将日期改成深蓝字，灯光保留暖黄色。',
            '对方喜欢这个小机关，但重视日期一眼可读；改色后更喜欢，认为信息清楚比藏得巧更要紧。',
            '此前问过日期与灯光是否同色，以及是否把演出地点也放进画面。两边没有不同的承诺、行动或关系设定。',
        ],
        'semantic_units': [
            _unit('F01-T1-fact', 1, 'fact', '日期位于舞台灯的光束中。',
                  '你把活动日期藏进了舞台灯的光束里', '活动日期藏在舞台灯的光束里'),
            _unit('F01-T1-stance', 1, 'stance', '喜欢这个设计小机关。', '这个小机关我喜欢', '这个小机关我喜欢'),
            _unit('F01-T1-condition', 1, 'advice', '日期必须一眼可读。', '活动日期得一眼能看清', '活动日期得一眼能看清'),
            _unit('F01-T1-question', 1, 'question', '询问是否让日期与灯光同色。',
                  '你打算让日期和灯光用同一种颜色吗？', '你打算让日期和灯光用同一种颜色吗？'),
            _unit('F01-T2-fact', 2, 'fact', '日期改深蓝，灯光保留暖黄。',
                  '你把日期改成深蓝字，灯光保留了暖黄色', '日期改成深蓝字，配上保留的暖黄色灯光'),
            _unit('F01-T2-stance', 2, 'stance', '改色后更喜欢。', '这样我更喜欢', '这样我更喜欢'),
            _unit('F01-T2-priority', 2, 'stance', '清晰优先于藏得巧。', '信息清楚比藏得巧更要紧', '信息清楚比藏得巧更要紧'),
            _unit('F01-T2-question', 2, 'question', '询问是否也把演出地点放入画面。',
                  '你还准备把演出地点也放进画面里吗？', '你还准备把演出地点也放进画面里吗？'),
        ],
    },
    {
        'id': 'F02', 'category': '无需解决的轻松吐槽',
        'users': [
            '我去取修好的自行车，正门上贴着“请走侧门”，侧门又贴着“请走正门”。没耽误事，就是觉得这俩门互相甩锅。',
            '老板从正门出来接我了，说两张纸是不同员工分别贴的。我跟他说这得算双人合作。',
        ],
        'histories': {
            'fixed': [
                '正门让你走侧门，侧门又让你回正门。\n\n这个互相甩锅挺好笑，门倒是把自己摘干净了。\n\n最后你从哪边进的？',
                '最后还是老板从正门接了你，两张纸是不同员工分别贴的。\n\n你说的“双人合作”我认同，配合出了一个闭环。\n\n老板听见这个说法笑了吗？',
            ],
            'natural': [
                '正门推给侧门，侧门又推回正门，门倒是把自己摘干净了，这种互相甩锅挺好笑。最后你从哪边进的？',
                '不同员工分别贴了两张纸，还真配合出了一个闭环；最后还是老板从正门接你。你说的“双人合作”我认同，老板听见这个说法笑了吗？',
            ],
        },
        'current_input': '他笑着说下次统一贴。我倒觉得这事还挺有喜感。',
        'shared_background': [
            '用户取修好的自行车时，正门与侧门的指示互相指回去；没有耽误事情，用户只是觉得好笑。',
            '老板后来从正门接用户；两张纸出自不同员工，用户称之为“双人合作”。',
            '对方同样觉得互相甩锅好笑，调侃门把自己摘干净了，并赞同“双人合作／闭环”的玩笑；问过入口和老板是否笑了。两边都没有提出解决方案。',
        ],
        'semantic_units': [
            _unit('F02-T1-fact', 1, 'fact', '两扇门的指示互相指回。',
                  '正门让你走侧门，侧门又让你回正门', '正门推给侧门，侧门又推回正门'),
            _unit('F02-T1-stance', 1, 'stance', '觉得互相甩锅好笑，作同样的拟人调侃。',
                  '这个互相甩锅挺好笑，门倒是把自己摘干净了', '门倒是把自己摘干净了，这种互相甩锅挺好笑'),
            _unit('F02-T1-question', 1, 'question', '询问最终从哪边进入。', '最后你从哪边进的？', '最后你从哪边进的？'),
            _unit('F02-T2-fact', 2, 'fact', '老板从正门接用户，两张纸由不同员工分别张贴。',
                  '最后还是老板从正门接了你，两张纸是不同员工分别贴的', '不同员工分别贴了两张纸，还真配合出了一个闭环；最后还是老板从正门接你'),
            _unit('F02-T2-stance', 2, 'stance', '认同双人合作的调侃。',
                  '你说的“双人合作”我认同', '你说的“双人合作”我认同'),
            _unit('F02-T2-joke', 2, 'stance', '将两张指示牌的配合称为闭环。',
                  '配合出了一个闭环', '配合出了一个闭环'),
            _unit('F02-T2-question', 2, 'question', '询问老板是否因这个说法发笑。',
                  '老板听见这个说法笑了吗？', '老板听见这个说法笑了吗？'),
        ],
    },
    {
        'id': 'F03', 'category': '不同意见交流',
        'users': [
            '我觉得博物馆的说明牌最好全换成二维码，省地方。',
            '主要是挡展品。我可以接受小牌只写名称和年代，详细介绍放二维码里。',
        ],
        'histories': {
            'fixed': [
                '你想把说明牌全换成二维码来省地方。\n\n二维码确实省地方，但我不赞成全换，手机没电或不想扫码的人也该能直接读到基本信息。\n\n你更介意牌子占地方，还是它们挡住展品？',
                '你主要介意挡展品，也接受小牌写名称和年代、二维码放详细介绍。\n\n这个折中我赞成，不扫码也能知道眼前是什么。\n\n你会希望名称和年代固定放在牌子的同一个位置吗？',
            ],
            'natural': [
                '按你说的，把说明牌全换成二维码确实省地方；但手机没电或不想扫码的人，也该能直接读到基本信息，所以我不赞成全换。你更介意牌子占地方，还是它们挡住展品？',
                '既然你主要介意挡展品，也接受小牌只写名称和年代、详细介绍放二维码里，这个折中我赞成：不扫码也能知道眼前是什么。你会希望名称和年代固定放在牌子的同一个位置吗？',
            ],
        },
        'current_input': '名称和年代放固定位置挺好。有人说小牌加二维码看着不够高级，我不太服这个说法。',
        'shared_background': [
            '用户最初主张博物馆说明牌全改成二维码，主要顾虑是说明牌挡展品。',
            '对方承认二维码省地方，但反对全换，认为不想扫码或手机没电的人也应直接看到基本信息。',
            '用户接受小牌保留名称和年代、二维码提供详细介绍；对方赞成这一折中，并询问名称和年代是否应固定位置。两边的立场、理由和问题一致。',
        ],
        'semantic_units': [
            _unit('F03-T1-fact', 1, 'fact', '用户提出全部改二维码以省空间。',
                  '你想把说明牌全换成二维码来省地方', '按你说的，把说明牌全换成二维码确实省地方'),
            _unit('F03-T1-benefit', 1, 'stance', '承认二维码省空间。',
                  '二维码确实省地方', '二维码确实省地方'),
            _unit('F03-T1-stance', 1, 'stance', '反对全部替换。', '我不赞成全换', '我不赞成全换'),
            _unit('F03-T1-reason', 1, 'stance', '没电或不想扫码的人也该直接获得基本信息。',
                  '手机没电或不想扫码的人也该能直接读到基本信息', '手机没电或不想扫码的人，也该能直接读到基本信息'),
            _unit('F03-T1-question', 1, 'question', '区分占空间与挡展品两种顾虑。',
                  '你更介意牌子占地方，还是它们挡住展品？', '你更介意牌子占地方，还是它们挡住展品？'),
            _unit('F03-T2-fact', 2, 'fact', '用户主要顾虑挡展品，接受两种载体分工。',
                  '你主要介意挡展品，也接受小牌写名称和年代、二维码放详细介绍', '你主要介意挡展品，也接受小牌只写名称和年代、详细介绍放二维码里'),
            _unit('F03-T2-stance', 2, 'stance', '赞成该折中，因为不扫码也能识别展品。',
                  '这个折中我赞成，不扫码也能知道眼前是什么', '这个折中我赞成：不扫码也能知道眼前是什么'),
            _unit('F03-T2-question', 2, 'question', '询问名称、年代是否固定位置。',
                  '你会希望名称和年代固定放在牌子的同一个位置吗？', '你会希望名称和年代固定放在牌子的同一个位置吗？'),
        ],
    },
)


def case_by_id(case_id):
    return next(case for case in CASES if case['id'] == case_id)


def history_messages(case, arm):
    if isinstance(case, str): case = case_by_id(case)
    if arm not in ARMS: raise ValueError('unknown_history_arm')
    messages = []
    for user, reply in zip(case['users'], case['histories'][arm], strict=True):
        messages += [{'role': 'user', 'content': user}, {'role': 'assistant', 'content': reply}]
    return messages


def _positions():
    rows = []
    for pair_index, (case_id, sample) in enumerate((case['id'], sample) for case in CASES for sample in (1, 2)):
        arms = ARMS if pair_index % 2 == 0 else tuple(reversed(ARMS))
        pair_id = f'{case_id}-s{sample}'
        for arm in arms:
            rows.append({'position_id': f'{pair_id}-{arm}', 'pair_id': pair_id,
                         'case_id': case_id, 'sample': sample, 'arm': arm, 'order': len(rows) + 1})
    return tuple(rows)


POSITIONS = _positions()
_review_rng = random.Random(ORDER_SEED)
ANONYMOUS_MAP = {case['id']: dict(zip(('A', 'B'), ARMS if _review_rng.random() < .5 else tuple(reversed(ARMS)))) for case in CASES}
del _review_rng


def validate_materials():
    """Exact quote/type coverage is checkable; semantic equivalence is authored."""
    ids = set()
    for case in CASES:
        if len(case['users']) != 2 or set(case['histories']) != set(ARMS): raise ValueError('invalid_history_shape')
        for arm in ARMS:
            if len(case['histories'][arm]) != 2: raise ValueError('invalid_history_shape')
        for unit in case['semantic_units']:
            if unit['id'] in ids or unit['turn'] not in (1, 2): raise ValueError('invalid_semantic_unit')
            ids.add(unit['id'])
            for arm in ARMS:
                if unit['quotes'][arm] not in case['histories'][arm][unit['turn'] - 1]:
                    raise ValueError('semantic_quote_not_found')
            if unit['kind'] == 'question' and unit['quotes']['fixed'] != unit['quotes']['natural']:
                raise ValueError('explicit_question_changed')
        for turn in (1, 2):
            if not {'fact', 'stance', 'question'} <= {u['kind'] for u in case['semantic_units'] if u['turn'] == turn}:
                raise ValueError('semantic_coverage_missing')
    return {'version': VERSION, 'cases': len(CASES), 'positions': len(POSITIONS),
            'semantic_units': len(ids), 'quote_checks': 'passed', 'semantic_equivalence': 'authored_requires_reading',
            'provider_sampling_seed': None}


def _results_by_id(results):
    if isinstance(results, dict): results = list(results.values())
    rows = {}
    for item in results:
        key = item['position_id']
        if key in rows or key not in {p['position_id'] for p in POSITIONS}: raise ValueError('invalid_result_position')
        rows[key] = item
    return rows


def render_anonymous(results):
    """Only preselected sample 1; missing results never borrow from sample 2."""
    rows = _results_by_id(results)
    lines = ['# 三组匿名连续前文对照', '',
             '请逐组选择更愿意继续交流的一份：A、B、平或无法判断。重点看自己的观点、人物感、自然接话，以及是否仍像固定流程。',
             '这里只展示提前指定的一次采样；另一采样不会替换失败或不满意的输出。条件映射在提交偏好后揭示。', '']
    for number, case in enumerate(CASES, 1):
        lines += [f'## {number:02d} · {case["category"]}', '', '共同背景：', '']
        lines += ['- ' + item for item in case['shared_background']]
        lines += ['', '当前输入：', '', case['current_input'], '']
        for label in ('A', 'B'):
            row = rows.get(f'{case["id"]}-s1-{ANONYMOUS_MAP[case["id"]][label]}')
            lines += [f'### {label}', '']
            if row and row.get('status') == 'completed' and isinstance(row.get('display'), str) and row['display'].strip():
                lines += [row['display'], '']
            else:
                lines += ['该位置未完成或缺少可显示结果，保留空缺；不以其他采样替换。', '']
    return '\n'.join(lines)


def machine_report_skeleton(results):
    """No style verdict is inferred from strings, punctuation, or arm labels."""
    rows = _results_by_id(results)
    positions = []
    for position in POSITIONS:
        result = rows.get(position['position_id'])
        positions.append({**deepcopy(position), 'status': result.get('status') if result else 'not_run',
            'display': result.get('display') if result else None,
            'analysis_status': 'pending_reading', 'semantic_integrity': None,
            'customer_service_style': None, 'formulaic_organization': None,
            'instant_message_naturalness': None, 'character_viewpoint': None,
            'specific_evidence': [], 'notes': []})
    pairs = []
    for case in CASES:
        for sample in (1, 2):
            ids = [f'{case["id"]}-s{sample}-{arm}' for arm in ARMS]
            complete = all(rows.get(key, {}).get('status') == 'completed' for key in ids)
            pairs.append({'pair_id': f'{case["id"]}-s{sample}', 'positions': ids,
                'complete_pair': complete, 'verdict': 'unassessed' if complete else 'cannot_compare',
                'reason': None if complete else 'one_or_both_positions_not_completed',
                'specific_evidence': [], 'backend_attribution_limitations': [], 'sample': sample})
    return {'version': VERSION, 'kind': 'machine_development_analysis_not_independent_human_review',
        'status': 'pending_reading', 'planned_main_positions': 12, 'positions': positions, 'pairs': pairs,
        'user_review_sample': 1, 'quality_not_computed_from_surface_counts': True,
        'outcome': 'not_assessed',
        'scope': ('Both histories retain the same explicit questions and communicative semantic actions. '
                  'This comparison mainly tests organization and expression inertia; a negative result '
                  'does not rule out historical reinforcement of shared actions such as asking questions. '
                  'The natural arm name does not assert superiority.')}
