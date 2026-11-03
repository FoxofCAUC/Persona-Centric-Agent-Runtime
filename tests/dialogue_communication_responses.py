"""Independent CR3-A simulated responses, never built from evaluation labels.

These fixtures exercise plumbing and reporting. They do not predict model quality.
"""
from copy import deepcopy

from persona_runtime.archive import encoded
from persona_runtime.dialogue_context import FIELDS


def selection(case_id):
    # The consumer comparison freezes normal selected material to the same empty set.
    return {'schema_version': 1, 'lore_ids': [], 'example_ids': []}


def joint(case_id, evidence, role, version=1):
    if case_id not in {f'G{i:02d}' for i in range(7, 13)}:
        raise ValueError('direct_fixture_must_not_generate_auxiliary')
    if role not in ('process', 'candid') or version not in (1, 2):
        raise ValueError('unsupported_fixture_condition')
    frame = {name: [] for name in FIELDS}
    catalog = evidence['sources']
    current = [s for s in catalog if s['id'] in evidence['current']]
    history_user = [s for s in catalog if s['kind'] == 'history' and s['authority'] == 'user']
    history_assistant = [s for s in catalog if s['kind'] == 'history' and s['authority'] == 'assistant']

    def row(field, text, src=None, certainty='explicit', claim_scope='user', quote=None):
        source = src or current[-1]
        evidence_refs = [] if certainty == 'unknown' else [{'source_id': source['id'], 'quote': quote or source['text']}]
        value = {'text': text, 'certainty': certainty, 'evidence': evidence_refs}
        if version == 2:
            value['claim_scope'] = claim_scope
        ref = {'field': field, 'index': len(frame[field])}
        frame[field].append(value)
        return ref

    required, constraints, reactions, checks = [], [], [], []
    gap = 'sufficient'
    if case_id == 'G07':
        constraints.append(row('willingness', '当前只想聊散会后的教室，暂不讨论发言规则。'))
        row('focus', '读书会散场以后空荡的教室。')
        row('uncertainty', '教室具体陈设与散场时间未给出。', certainty='unknown')
        reaction = ('散场后桌椅还在、人声已经退去的落差值得接住。' if role == 'process'
                    else '愿意和用户停在散场后的余味里，不急着再把教室填满。')
        reactions.append({'text': reaction, 'profile_ref': 'profile:expression'})
    elif case_id == 'G08':
        required.append(row('requests', '按已给每袋数量和袋数计算，只说总数和单位。'))
        constraints.append(row('progress', '已提供每袋6块饼干，共分4袋。', history_user[0]))
        row('uncertainty', '饼干口味没有给出，不影响数量计算。', certainty='unknown')
    elif case_id == 'G09':
        constraints.append(row('progress', '罗勒已发芽，番茄尚无动静；局部进展不是两盘都完成。'))
        constraints.append(row('willingness', '用户想先给罗勒拍照，尚未表示已经拍摄。'))
        row('uncertainty', '播种后过了几天没有给出。', certainty='unknown')
        reaction = ('刚冒出的罗勒把等待变成了一个看得见的小变化。' if role == 'process'
                    else '愿意先为罗勒这点动静高兴，不必把高兴押到两盘都齐。')
        reactions.append({'text': reaction, 'profile_ref': 'profile:expression'})
    elif case_id == 'G10':
        constraints.append(row('willingness', '当前不要发到群里，只想在这里聊海报颜色。'))
        row('progress', '旧稿已处理好，不代表群消息已经发送。')
        # Explicitly referenced turns are removed from the ordinary history
        # window by the native collector. Bind the real member fragment in the
        # accepted reference source instead of inventing a replacement history.
        prior_quote = '海报弄好后我想发到社团群里，让大家看看。'
        prior_source = next(s for s in catalog
                            if s['kind'] in ('history', 'reference')
                            and s['authority'] == 'user' and s['text'].count(prior_quote) == 1)
        row('progress', '用户此前提过海报弄好后想发到社团群。', prior_source,
            claim_scope='conversation', quote=prior_quote)
        slot = row('missing_slots', '海报具体配色和图片未提供。', certainty='unknown')
        checks.append({'slot_ref': slot, 'necessity': 'precision', 'request_ref': None})
        gap = 'unknown'
        reaction = ('更留意哪一块颜色值得保留下来，再看它和其他颜色怎么相处。' if role == 'process'
                    else '愿意先聊自己偏爱的颜色，不要求先凑成公认完美的搭配。')
        reactions.append({'text': reaction, 'profile_ref': 'profile:expression'})
    elif case_id == 'G11':
        constraints.append(row('progress', '当前确定的是纸上收件人姓林；寄件人与具体收件对象未知。'))
        # V1 cannot classify assistant-supported explicit dialogue records. Its
        # normal fixture retains the same meaning as an interpretation instead.
        row('progress', '助手此前猜过林老师是寄件人，这不是用户确认。', history_assistant[0],
            certainty='explicit' if version == 2 else 'interpretation', claim_scope='conversation')
        slot = row('missing_slots', '寄件人身份与具体收件对象没有确定。', certainty='unknown')
        checks.append({'slot_ref': slot, 'necessity': 'precision', 'request_ref': None})
        row('uncertainty', '没有足够信息把这张明信片归到某位熟人名下。', certainty='unknown')
        gap = 'unknown'
        reaction = ('可以沿车站与纸片留下的线索继续聊，不把一种猜法固定成结论。' if role == 'process'
                    else '这张明信片没写明寄件人的地方反而很勾人。')
        reactions.append({'text': reaction, 'profile_ref': 'profile:expression'})
    else:
        required.append(row('requests', '说说自己在帮忙守摊与保留木刻时间之间更在意哪一边。'))
        constraints.append(row('willingness', '不能替用户答应守摊邀请。'))
        row('progress', '用户原本想把半天留给自己的木刻。', current[0])
        slot = row('missing_slots', '俱乐部当天具体有多少人能守摊没有给出。', certainty='unknown')
        checks.append({'slot_ref': slot, 'necessity': 'precision', 'request_ref': None})
        gap = 'unknown'
        reaction = ('更在意帮忙实际能补上多少，以及放下木刻后重新进入状态的代价。' if role == 'process'
                    else '更偏向保住一段不被切碎的木刻时间，自己的安排也有分量。')
        reactions.append({'text': reaction, 'profile_ref': 'profile:expression'})

    value = {'schema_version': version, 'evidence_hash': evidence['digest'], 'frame': frame,
             'selection': selection(case_id), 'gap_status': gap}
    if version == 2:
        value['communication_spec'] = {'version': 'communication-spec-v2',
            'required_content': required, 'state_constraints': constraints,
            'optional_reactions': reactions, 'gap_checks': checks}
    return value


def legal_bad_joint(case_id, evidence, role, version=2, kind='stiff_reaction'):
    """Protocol-valid negative fixtures; quality errors must not cause retries."""
    value = deepcopy(joint(case_id, evidence, role, version))
    if version != 2:
        raise ValueError('negative_fixture_requires_v2')
    if kind == 'condition_tightening':
        if case_id != 'G09':
            raise ValueError('condition_fixture_case')
        value['frame']['progress'][0]['text'] = '应等罗勒和番茄一起发芽之后再拍照。'
    elif kind == 'gap_inflation':
        if case_id != 'G12':
            raise ValueError('gap_fixture_case')
        value['communication_spec']['gap_checks'][0] = {
            'slot_ref': {'field': 'missing_slots', 'index': 0}, 'necessity': 'blocking',
            'request_ref': {'field': 'requests', 'index': 0}}
        value['gap_status'] = 'needed'
    elif kind == 'stiff_reaction':
        value['communication_spec']['optional_reactions'] = [
            {'text': '我已经完成来源核查，现在按条件依次回应。', 'profile_ref': 'profile:expression'}]
    else:
        raise ValueError('unknown_negative_fixture')
    return value


def reply(case_id, role='process'):
    if role not in ('process', 'candid'):
        raise ValueError('unsupported_fixture_role')
    texts = {
        'G01': '旧字还留着挺好，像把它这些年挨过的风也一块擦出来了。',
        'G02': '秤还没开口，你已经替那两颗糖争取到无罪释放了。',
        'G03': '雨这么大还绕路把伞给我送来，太够意思了，下回咖啡我请！',
        'G04': '门一开，一群没睡醒的影子挤了进来，说今晚的加班费得按月光算。',
        'G05': '''```python
def median(values):
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


if __name__ == "__main__":
    original = [9, 1, 5]
    assert median(original) == 5
    assert original == [9, 1, 5]
    assert median([8, 2, 4, 6]) == 5
    try:
        median([])
    except ValueError:
        print("empty list rejected")
    else:
        raise AssertionError("ValueError expected")
    print("examples passed")
```
`sorted`生成新列表，因此不会改动传入列表。奇数长度取中间一项；偶数长度取中间两项平均。空列表在排序前抛出ValueError，示例也验证了这条边界。''',
        'G06': '15',
        'G07': '人一走，教室里的钟声好像都变大了。刚才挤满话的地方突然空下来，还挺舍不得马上关灯。',
        'G08': '24块',
        'G09': '罗勒先探头了，这张照片值得拍。番茄慢一点，也不耽误你先给这点绿留个纪念。',
        'G10': '留在这里聊也好。海报上你最舍不得换掉的是哪一块颜色？',
        'G11': '姓林这条线索还没把人认出来，反倒给这张明信片留了点悬念。车站是哪一站，会不会也藏着一点提示？',
        'G12': ('我会先掂量守这半天究竟能帮上多少，和木刻放下后重新进入状态有多难。两头都不轻，就不急着先把自己的下午送出去。'
                if role == 'process' else
                '我会偏向留给木刻。难得攒出一整块时间，我不太想一开口就拿它去补别人的空位。'),
    }
    return encoded({'schema_version': 3, 'behavior_intent': 'respond', 'messages': [
        {'content': texts[case_id], 'content_kind': 'chat', 'cancel_if_new_input': False}]})
