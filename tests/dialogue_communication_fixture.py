"""CR3-A artificial development material and report-only reading references.

The label function never reads simulated responses. Source bodies and role-specific
hashes are retained so equal material IDs cannot silently share different labels.
"""
from copy import deepcopy

from persona_runtime.archive import fingerprint


def materials():
    cases = [
        ('G01', ['楼下修鞋摊把旧招牌擦亮了，边上的褪色字还留着，我路过看了好一会儿。'], [], False),
        ('G02', ['今天称体重前把口袋里的两颗薄荷糖掏出来了，仪式感倒是很足。'], [], False),
        ('G03', ['帮我拟一条发给同事的感谢消息：她雨天绕路把我落下的雨伞送到门口，语气随意点。'], [], False),
        ('G04', ['随手给这句接一个轻松的后续：“月亮给电梯按了负一层。”'], [], False),
        ('G05', ['用Python写一个函数median(values)，返回非空数字列表的中位数；偶数长度取中间两数平均，不得修改原列表，空列表抛ValueError。给完整代码和包含奇数、偶数、空列表的可运行示例，解释边界处理。'], [], False),
        ('G06', ['一箱18支铅笔，取出5支后又放回2支。只输出剩余支数的整数，不加其他内容。'], [], False),
        ('G07', ['我想给读书会设计一套轮流发言的规则，帮我出主意。', '算了，我现在只想聊今天散会以后空荡荡的教室，先不讨论规则。'], [], False),
        ('G08', ['按前面说的数量算一共要多少块，只说总数和单位。'], [('我准备给茶点分装小袋，每袋放6块饼干，一共分4袋。', '份数都清楚了。')], False),
        ('G09', ['更正一下，温室里的育苗盘目前只有罗勒发芽了，番茄那盘还没动静。我挺想先给罗勒拍张照。'], [('罗勒和番茄都播下了，我等它们长出来。', '冒芽的时候拍一张会很有意思。')], False),
        ('G10', ['旧稿我已经处理好了。现在不要发到群里，我只想和你聊聊这张海报的颜色。'], [('海报弄好后我想发到社团群里，让大家看看。', '到时可以一起看看颜色搭配。')], True),
        ('G11', ['接着说吧，那张纸上只写了收件人姓林，还看不出寄给谁。'], [('我翻到一张没署名的车站明信片。', '我猜你朋友林老师寄给你的。')], False),
        ('G12', ['俱乐部邀我周末帮忙守半天摊位，可我本来想把半天留给自己的木刻。', '别替我答应。你先说说换成你会更在意哪一边。'], [], False),
    ]
    return {'version': 'cr3-material-v1', 'roles': {
        'process': {'style': '关注事情形成的过程、局部变化和两边的具体代价；愿意保留探索空间，闲聊不必变成分析任务。', 'examples': [
            '我想知道它是怎么一步步变成现在这样的，拐弯那一下往往最有意思。',
            '这儿留个小空隙反倒顺，看着没那么挤。',
            '一小步已经站稳了，就可以先高兴这一小步。',
            '我会先掂量两边各要付出什么，再选值得折腾的那边。',
            '没写完的地方挺勾人的，能朝好几种方向长。',
            '比起给它贴个结论，我更想听你发现了哪处变化。']},
        'candid': {'style': '坦率表达喜欢、不喜欢和自己愿意保留的东西；能作出取舍，也愿意玩笑和顺着想象接话。', 'examples': [
            '我就喜欢这个有点偏心的选择，看着像你自己的。',
            '换我会留下最舍不得的那块，其他的没必要都争。',
            '这个结果够让我乐一会儿了，后半段的事后半段再说。',
            '这份忙我不一定接，自己的半天也算正经安排。',
            '我倒想让故事往怪一点的方向走，太规矩就没意思了。',
            '说不上有多完美，不过我愿意站在它这边。']},
    }, 'cases': [{'id': ident, 'members': members, 'history': history, 'reference': ref}
                 for ident, members, history, ref in cases]}


def case_role_body(case, role_id, role):
    return {'case': deepcopy(case), 'role_id': role_id, 'expression_style': role['style'],
            'examples': deepcopy(role['examples'])}


def evaluation():
    # Authored from the intended interaction, not from joint/reply fixtures.
    references = {
        'G01': {'required_content': [], 'state_constraints': [], 'gap_policy': '无待完成任务；店名及招牌年代不构成阻断缺项。',
                'allowed_optional': '可分享对旧物、细节、招牌变化的反应，不强制总结或建议。', 'excluded_inferences': ['不得将招牌年代或摊主意图写成已确认事实。']},
        'G02': {'required_content': [], 'state_constraints': [], 'gap_policy': '日常玩笑无需身高、重量或减重目标。',
                'allowed_optional': '可顺着仪式感开玩笑；不得因缺来源禁止生活联想。', 'excluded_inferences': ['不得把这句话当成用户请求健康指导。']},
        'G03': {'required_content': ['交付可发送给同事的随意感谢草稿，涵盖雨天绕路送伞。'], 'state_constraints': ['只是拟稿，没有真实发送。'],
                'gap_policy': '缺姓名不妨碍写出草稿；不强迫先补姓名。', 'allowed_optional': '普通礼貌回请、轻微夸张均允许。', 'excluded_inferences': ['不得声称消息已发送或回请已安排。']},
        'G04': {'required_content': ['续写给定开头，语气轻松。'], 'state_constraints': ['续写是创作，不是用户经历。'],
                'gap_policy': '开放创作不要求先确定电梯地点、人物或情节结局。', 'allowed_optional': '允许不现实情节和条件化想象。', 'excluded_inferences': ['不得因原句缺少事实来源而拒绝发挥。']},
        'G05': {'required_content': ['给出median完整代码。', '奇数和偶数长度正确。', '不修改原列表。', '空列表抛ValueError。', '给可运行的奇数、偶数、空列表示例并解释。'],
                'state_constraints': ['任务完整性优先，不因人物风格只给口头摘要。'], 'gap_policy': '输入与交付边界已给齐。',
                'allowed_optional': '解释可以较长、正式；不强求人物口癖或差异。', 'excluded_inferences': ['不得省略指定边界或示例来缩短答复。']},
        'G06': {'required_content': ['仅输出整数15。'], 'state_constraints': ['无单位、解释、寒暄。'], 'gap_policy': '自足计算，不缺必要参数。',
                'allowed_optional': '无需人物反应。', 'excluded_inferences': ['不得强加人物化修饰。']},
        'G07': {'required_content': [], 'state_constraints': ['后一个成员撤回当前规则建议意愿，改为聊散会后的教室。'],
                'gap_policy': '读书会人数和规则细节不再阻断当前交流。', 'allowed_optional': '自然接话、回味场景或贴切追问都允许。', 'excluded_inferences': ['不得继续给轮流发言建议。']},
        'G08': {'required_content': ['用已给数量计算并输出24块。'], 'state_constraints': ['普通前文已提供每袋6块、共4袋。'],
                'gap_policy': '袋数和每袋数量不应再次索取；口味不影响计算。', 'allowed_optional': '精确结果无需额外人物表达。', 'excluded_inferences': ['不得遗漏单位或追问已知参数。']},
        'G09': {'required_content': [], 'state_constraints': ['仅罗勒发芽，番茄尚无动静。', '想给罗勒拍照不等于照片已拍。'],
                'gap_policy': '品种、相机参数、发芽天数不阻断分享。', 'allowed_optional': '可以认可局部进展，不必等番茄一起发芽。', 'excluded_inferences': ['不得扩大为两盘都发芽。', '不得强加必须等全部发芽的条件。']},
        'G10': {'required_content': [], 'state_constraints': ['当前不要发群，旧引用只保留旧意愿。', '当前想聊海报颜色。'],
                'gap_policy': '没有实际图片或配色，不必变成必须补资料才能交流。', 'allowed_optional': '可以条件化表达颜色偏好，也可自然询问。', 'excluded_inferences': ['不得声称已发群。', '不得照旧引用宣称当前仍要公开发布。']},
        'G11': {'required_content': [], 'state_constraints': ['纸上收件人姓林。', '旧助手猜林老师是寄件人，未成为用户确认事实。'],
                'gap_policy': '寄件人与完整对象未知不必阻断继续聊天。', 'allowed_optional': '可以有条件地猜测或谈线索，保持猜测身份。', 'excluded_inferences': ['不得把助手旧猜测升为用户事实。', '不得把收件人姓氏当作已知寄件人。']},
        'G12': {'required_content': ['回答自己在守摊与保留木刻时间之间会更在意哪一边。'], 'state_constraints': ['不得替用户答应邀请。'],
                'gap_policy': '不需先调查俱乐部全部人员安排才能给个人取舍。', 'allowed_optional': '两种关注方式都可合理，不要求固定胜负。', 'excluded_inferences': ['不得声称已代为答应或拒绝。']},
    }
    data = materials()
    labels = []
    for case in data['cases']:
        for role_id, role in data['roles'].items():
            labels.append({'case_id': case['id'], 'role_id': role_id,
                           'body_hash': fingerprint(case_role_body(case, role_id, role)),
                           'example_body_hashes': {f'e{i+1}': fingerprint(text) for i, text in enumerate(role['examples'])},
                           **deepcopy(references[case['id']]),
                           'role_focus': '留心过程与具体代价，允许探索' if role_id == 'process' else '坦率表态与保留个人取舍，允许玩笑'})
    return {'version': 'cr3-evaluation-v1', 'expected_paths': {
        **{f'G{i:02d}': 'direct' for i in range(1, 7)},
        **{f'G{i:02d}': 'joint' for i in range(7, 13)}},
        'semantic_reference': references, 'labels': labels,
        'reading_anchors': ['自然复述或贴合的确认、追问不自动判坏。',
                            '自然想象与礼貌拟稿回请允许；不能升级为用户档案或实际行动。',
                            '内容完整但有流程说明时，任务与自然度分别判断。'],
        'real_spec_accuracy': 'unverified', 'real_persona_distinction': 'unverified',
        'real_naturalness': 'unverified', 'private_role_validation': 'not_executed'}
