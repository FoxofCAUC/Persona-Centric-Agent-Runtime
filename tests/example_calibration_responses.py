"""Scripted provider doubles, authored separately from evaluation labels.

Deliberately includes empty, inappropriate and different selections. These are
not predictions of v2/v3 quality. Missing candidate IDs are recorded and omitted.
"""
RESPONSES={
 'C01':(['progress'],['progress']), 'C02':(['fairness'],['fairness','play']),
 'C03':(['play'],['play']), 'C04':([],['tradeoff']),
 'C05':([],['progress']), 'C06':(['fairness'],['fairness']),
 'C07':([],['tradeoff']), 'C08':([],['aftermath']),
 'C09':(['progress'],['aftermath']), 'C10':(['play'],['formal']),
 'C11':([],[]), 'C12':([],['tradeoff']), 'C13':(['formal'],['formal']),
 'C14':([],['formal']), 'C15':(['fairness'],['fairness']), 'C16':([],['progress']),
 'C17':([],[]), 'C18':([],[]), 'C19':([],[]), 'C20':([],[]),
 'C21':(['tradeoff'],['progress','tradeoff']), 'C22':(['formal'],['formal']),
 'C23':([],['aftermath']), 'C24':(['formal'],['formal'])}

def response_plan(case_id,arm,pool):
    requested=list(RESPONSES[case_id][0 if arm=='control' else 1])
    roots=pool['payload']['roots']['example']
    return {'schema_version':1,'lore_ids':[],'example_ids':[k for k in requested if k in roots]},[k for k in requested if k not in roots]

def reply(case_id,arm):
    return {'C17':'168','C19':'322','C20':'{"ready":true}'}.get(case_id,
       '离线响应夹具'+('甲' if arm=='control' else '乙')+'，仅检验数据流。')
