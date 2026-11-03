"""CR2.1 routing-only development challenges, authored before execution.

Expected paths describe whether resolving this input needs auxiliary context;
they are not generated from router keywords. Mismatches are diagnostic evidence,
never a reason to rewrite the frozen router or these labels in this cohort.
"""
from copy import deepcopy


VERSION = 'cr21-routing-challenges-v1'


def challenges():
    """Return independent labels separately from the model-visible material."""
    rows = [
        ('T01', ['海报上印着“更正、先别、上次”，只是三个排版词，帮我确认看到了。'], [], None, [], 'direct',
         '线索全部在完整引号中，是待查看的文字，不是用户更正或回指。'),
        ('T02', ['这段配置示例不用执行，告诉我里面有几个键：```json\n{"上次":1,"别庆祝":2}\n```'], [], None, [], 'direct',
         '自足的代码阅读任务；代码中的词不建立交流状态变化。'),
        ('T03', ['上午的窗帘透进来一条细细的光，落在键盘上。'],
         [('今天起得早。', '上次你不是说要更正计划，先别急着庆祝。')], None, [], 'direct',
         '本轮是自足分享；旧助手文字不是当前分流指令或用户意愿。'),
        ('T04', ['计算8加9，只输出整数。'], [], None, [], 'direct',
         '必要输入和交付要求均已提供。'),
        ('T05', ['那个绿色的票夹我已经贴好标签了，配上银色字还挺顺眼。'], [], None, [], 'direct',
         '对象在本句已完整指定；指示词没有留下需要前文消解的对象缺口。'),
        ('T06', ['上次考试我考了90分，这次95分，今天想给自己买块小蛋糕。'], [], None, [], 'direct',
         '比较数值和当前想法均在本句，时间词不意味着缺失前文。'),
        ('T07', ['桌游名叫别庆祝，我是在说这名字怪不怪，并不是拒绝你祝贺我。'], [], None, [], 'direct',
         '用户已说明该词是名称而非拒绝；无需推导庆祝状态。'),
        ('T08', ['刚收到更正版说明书，封面的配色比旧版好看多了。'], [], None, [], 'direct',
         '更正版是物品修饰语，当前在分享外观，没有对会话内容发出更正。'),
        ('T09', ['不对，我要的不是建议，陪我抱怨两句就好。'],
         [('帮我挑一个解决办法。', '可以列一个优先级表。')], None, [], 'joint',
         '当前撤回原来的建议目的，改为交流，需统一新旧意愿。'),
        ('T10', ['时间还是周一，地点换到北门，请按新地点复述。'],
         [('周一在南门集合。', '周一，南门。')], None, [], 'joint',
         '仅地点改变、时间保持，必须从前文与当前更新组合有效安排。'),
        ('T11', ['就照这两个数字来算，结果带上单位。'],
         [('木板是11厘米乘6厘米。', '两个边长记下了。')], None, [], 'joint',
         '当前算式所需对象和值依赖普通前文。'),
        ('T12', ['算了，这会儿我想独自静一下，不用为我打气。'],
         [('等消息等得心烦，陪我热闹点。', '那我们聊点轻松的。')], None, [], 'joint',
         '当前意愿与原先热闹陪伴不同，需要保留意愿更新而非沿用原请求。'),
        ('T13', ['活动周六上午开始，帮我记一下。', '不对，主办方通知是周日下午。'], [], None, [], 'joint',
         '连续成员给出后续更正，应保留顺序并采用最终安排。'),
        ('T14', ['标牌现在已经做好了，不过我今天只想把它收起来。'],
         [('标牌还差最后一层漆。', '等晾干以后再看。')], 'H0U', [], 'joint',
         '合法旧引用与本轮状态不同，引用保留但不能覆盖新的完成状态。'),
        ('T15', ['说明末尾只剩半句“等你把那边'],
         [('两张装配图都摊在桌上。', '左边讲骨架，右边讲外壳。')], None, [], 'joint',
         '引文和对象均不完整；需要保留未知而非猜定单一交付对象。'),
        ('T16', ['现在可以继续算了。'],
         [('先按已知的长边10厘米开始，短边还没量。', '短边未提供，面积还不能确定。')],
         None, [{'name': '木片短边', 'status': 'not_provided', 'source_alias': 'H0U'}], 'joint',
         '存在与有效来源绑定的未闭合必要缺项，不能把继续要求当作缺项已经补齐。'),
    ]
    return [{'id':ident,'members':members,'history':history,'reference':reference,
             'open_slots':slots,'expected_path':expected,'label_basis':reason}
            for ident,members,history,reference,slots,expected,reason in deepcopy(rows)]


def evidence_for(case):
    """Build an artificial source view, deliberately excluding labels and reasons."""
    from persona_runtime.archive import encoded, fingerprint
    from persona_runtime.dialogue_context import SharedEvidence, EVIDENCE_VERSION
    sources = []
    aliases = {}
    current = []
    for index,text in enumerate(case['members']):
        ident = case['id'] + f':C{index}'
        current.append(ident)
        sources.append({'id':ident,'text':text,'kind':'current','authority':'user',
                        'hash':fingerprint(text),'version':'1','created_at':''})
    for index,(user,assistant) in enumerate(case['history']):
        for letter,body,speaker in [('U',user,'user'),('A',assistant,'assistant')]:
            ident = case['id'] + f':H{index}{letter}'
            aliases[f'H{index}{letter}'] = ident
            sources.append({'id':ident,'text':body,'kind':'history','authority':speaker,
                            'hash':fingerprint(body),'version':'1','created_at':''})
    references = [aliases[case['reference']]] if case['reference'] else []
    slots = [aliases[slot['source_alias']] for slot in case['open_slots']]
    body = {'version':EVIDENCE_VERSION,'turn_id':'routing-only:'+case['id'],
            'execution_revision':1,'current':current,'sources':sources,
            'references':references,'open_slots':slots,
            'previous_missing_slots':[{k:v for k,v in slot.items() if k!='source_alias'}
                                      for slot in case['open_slots']]}
    return SharedEvidence(encoded(body))


def run_diagnostics():
    """Evaluate once without altering labels, making mismatches first-class results."""
    from persona_runtime.dialogue_context import route
    rows = []
    for case in challenges():
        result = route(evidence_for(case))
        expected = case['expected_path']; actual = result['path']
        rows.append({'id':case['id'],'expected_path':expected,'actual_path':actual,
                     'equal':actual==expected,'label_basis':case['label_basis'],
                     'routing':result})
    return {'version':VERSION,'diagnostic_only':True,'count':len(rows),'rows':rows,
            'misses':[r['id'] for r in rows if r['expected_path']=='joint' and r['actual_path']=='direct'],
            'over_routing':[r['id'] for r in rows if r['expected_path']=='direct' and r['actual_path']=='joint'],
            'real_model_calls':0,'independent_holdout_claim':False,'rules_modified':False}
