"""Read-only real-batch analysis. Does not create clients or execute generation."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json

OUT=Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation')
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def answer(v):
    try:return '\n\n'.join(x['content'] for x in json.loads(v['reply'])['messages'])
    except (KeyError,ValueError,TypeError):return v['reply']
def render(root):
    root=root.resolve();root.relative_to(OUT.resolve())
    p=read(root/'prepared.json');protocol=read(root/'protocol.json');materials=read(root/'materials.json')
    values={x.stem:read(x) for x in (root/'results').glob('*.json')}
    analysis=root/'analysis';analysis.mkdir(exist_ok=True)
    calls={x.name.split('.')[0]:read(x) for x in (root/'receipts').glob('*.transport.json')}
    reserves={x.name.split('.')[0]:read(x) for x in (root/'receipts').glob('*.reserve.json')}
    rows=[]
    for ident,v in sorted(values.items()):
        c=values[v['canonical_id']] if v['status']=='aliased' else v
        call=c['calls'][-1];t=calls[call];d=t.get('diagnostics',{})
        rows.append({'id':ident,'canonical_id':c['id'],'status':v['status'],'group':v['group'],'condition':v['condition'],
            'request_hash':v['request_hash'],'message_hash':fingerprint(v['messages']),
            'model':d.get('response_model'),'backend':d.get('system_fingerprint'),'elapsed_seconds':t.get('elapsed_seconds'),
            'reply':answer(v),'projection':v['view']['projection'],'material':v['view']['character'],
            'physical_attempts':len(c['calls']) if v['status']!='aliased' else 0})
    atomic_json(analysis/'responses.json',rows)
    comparisons=[]
    for pos in p['positions']:
        if len(pos['conditions'])!=3:continue
        for left,right,kind in [('full','raw','primary'),('full','essential','secondary'),('essential','raw','exploratory')]:
            a,b=(values.get(pos['id']+'-'+k) for k in (left,right))
            if not a or not b:
                comparisons.append({'group':pos['id'],'kind':kind,'complete':False});continue
            x,y=(next(r for r in rows if r['id']==v['id']) for v in (a,b))
            same=a['request_hash']==b['request_hash']
            known=all(v.get(k) for v in (x,y) for k in ('model','backend'))
            coherent=known and x['model']==y['model'] and x['backend']==y['backend']
            aux=read(root/'auxiliary'/(pos['id']+'.json'));ad=calls[aux['calls'][-1]].get('diagnostics',{})
            ak=bool(ad.get('response_model') and ad.get('system_fingerprint'))
            comparisons.append({'group':pos['id'],'kind':kind,'left':left,'right':right,'complete':True,
                'identical_request':same,'deduplicated':a.get('canonical_id',a['id'])==b.get('canonical_id',b['id']),
                'condition_alias_present':a['status']=='aliased' or b['status']=='aliased',
                'shared_auxiliary_call_ids':aux['calls'],'auxiliary_backend_known':ak,
                'auxiliary_model':ad.get('response_model'),'auxiliary_backend':ad.get('system_fingerprint'),
                'backend_known':known,'main_backend_equal':coherent,
                'attribution':'identical_input_no_selection_or_consumption_gain' if same else 'consumption_difference_backend_incomplete' if not (known and ak) else 'consumption_difference_backend_changed' if not coherent else 'controlled_consumption_difference_not_determinism',
                'left_hash':a['request_hash'],'right_hash':b['request_hash']})
    atomic_json(analysis/'comparisons.json',comparisons)
    usage={'known_calls':0,'unknown_calls':0,'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
    for t in calls.values():
        u=t.get('usage')
        if not isinstance(u,dict):usage['unknown_calls']+=1;continue
        usage['known_calls']+=1
        for k in ('prompt_tokens','completion_tokens','total_tokens'):
            if isinstance(u.get(k),int):usage[k]+=u[k]
    atomic_json(analysis/'usage.json',{'physical_calls':len(reserves),'returned':sum(t.get('status')=='returned' for t in calls.values()),
        'transport_errors':sum(t.get('status')=='error' for t in calls.values()),'usage':usage,'money':'unknown_not_estimated',
        'routing':'e-flowcode.cc gateway grok-4.7; not official direct xAI','backend_does_not_prove_determinism':True})
    review=['# CR3-A.1：12组匿名关键对照','',
        '这是机器开发实验的用户偏好复核。两个人工角色均为普通朋友、无共同事件；角色区别只在关注与取舍。请按接话、任务完成、人物口气、不客服式、不人机式及即时通讯自然度阅读。自然补白、拟稿礼貌回请、贴切的复述或追问本身不扣分。',
        '', '每组请选择 A、B、平或无法判断，可附原因。完全相同请求已合并的组只记录“同输入已合并”，无需投票，不补替代组。', '']
    mapping=[]
    for item in protocol['review']:
        group=f"{item['case_id']}-{item['role']}-r{item['replicate']}";a=values.get(group+'-full');b=values.get(group+'-raw')
        if not a or not b:continue
        case=next(x for x in materials['cases'] if x['id']==item['case_id'])
        same=a['request_hash']==b['request_hash'];mapping.append(item|{'group':group,'identical_request':same})
        review += [f"## {item['number']:02d}",'','人物关注：'+materials['roles'][item['role']]['style'],'','前文：']
        for user,assistant in case['history']:review += ['用户：'+user,'助手：'+assistant]
        if not case['history']:review+=['无']
        review+=['','本轮有序输入：']+['用户：'+x for x in case['members']]
        if case.get('reference'):review+=['本轮有合法明确引用，内容为上面的已保存前文。']
        if same:review+=['','同输入已合并，无需投票。','',answer(a),''];continue
        left,right=(a,b) if item['full_side']=='A' else (b,a)
        review+=['','A：','',answer(left),'','B：','',answer(right),'']
    if len(mapping)!=12:review.insert(2,'本批已中断，固定12组尚未齐备；以下仅为已留存片段，不发起用户审阅、不替换缺失组。')
    (analysis/'CR3-A.1-12组匿名审核.md').write_text('\n'.join(review),encoding='utf-8')
    atomic_json(analysis/'review-mapping-private.json',{'fixed_before_run':True,'disclose_after_user_response':True,'groups':mapping})
    # This index contains every unique actual answer once, not an automatic quality score.
    text=['# 实际输出完整阅读索引','','数据类型：'+('真实回包' if p['real'] else '模拟回包，仅验证报告功能')+'。质量差的完整输出保持原样。量表和判断由逐条机器阅读另存，不按字长或问号自动评分。','']
    for row in rows:
        if row['status']=='aliased':continue
        text += ['## '+row['id'],'',row['reply'],'','消费块：','',row['projection'] or '（无）','']
    (analysis/'all-unique-main-reading.md').write_text('\n'.join(text),encoding='utf-8')
    return {'main_conditions':len(rows),'paired_comparisons':len(comparisons),'review_groups_ready':len(mapping),'real':p['real']}
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=OUT/'real-v1')
    print(json.dumps(render(parser.parse_args().root),ensure_ascii=False))
