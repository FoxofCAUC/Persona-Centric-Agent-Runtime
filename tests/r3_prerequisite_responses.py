"""Independent scripted responses, including intentional mistakes; never gold labels."""
RESPONSES={
 'P01':([],['e1']), 'P02':(['e2'],['e3']), 'P03':(['e1'],['e3']),
 'P04':(['e1'],['e1','e2']), 'P05':(['e4'],['e4']), 'P06':(['e5'],['e4']),
 'P07':(['e2'],['e3']), 'P08':([],['e1']), 'P09':(['e2'],['e1','e6']),
 'P10':(['e6'],['e6']), 'P11':(['e3'],['e5']), 'P12':(['e2'],[])}


def response_plan(case_id,arm,pool):
    wanted=RESPONSES[case_id][0 if arm=='control' else 1]
    available=pool['payload']['roots']['example']
    return {'schema_version':1,'lore_ids':[],'example_ids':[k for k in wanted if k in available]},[k for k in wanted if k not in available]


def reply(case_id,arm):
    return '本条为离线脚本响应，仅验证数据流，不代表人物表达效果。'
