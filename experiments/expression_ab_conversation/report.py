"""独立会话完整审阅：同排版正文、逐泡会话与B链源稿分开。"""
from collections import Counter
from decimal import Decimal
import html
import json
import statistics

from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.knowledge_search_notice import read as notice
from .study import load
from experiments.expression_e3_review.common import read,sha

STYLE='''body{max-width:1380px;margin:30px auto;padding:0 24px 80px;background:#f3f4ef;color:#26382e;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}h1{font-size:29px}h2{font-size:23px}section{margin:30px 0;background:#fffefb;border:1px solid #d2dacc;border-radius:12px;padding:24px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}.side{min-width:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:10px 0 16px}.user{padding:12px;background:#eef3ea;border-radius:8px}details{border-top:1px solid #dbe1d6;padding:12px 0}summary{cursor:pointer}button,textarea{font:inherit}button{padding:7px 12px;background:white;border:1px solid #b6c9b9;border-radius:7px;margin:5px;cursor:pointer}button[aria-pressed=true]{background:#dcecdc}textarea{width:100%;min-height:65px;box-sizing:border-box}.bubble{background:#edf3e7;border-radius:10px;padding:9px 13px;margin:7px 0}.bar{position:sticky;top:0;background:#f3f4efee;padding:10px;z-index:2}small{color:#637264}.failure{color:#95482e}@media(max-width:800px){.pair{grid-template-columns:1fr}}'''


def report(root):
    plan=load(root,check_source=False);key=read(root/'review-key.json');ph=sha((root/'plan.json').read_bytes())
    if key['plan_sha256']!=ph:raise ValueError('ab_review_key_changed')
    groups=[];store=Store(RuntimePaths(root/'instance'))
    with store.connection() as db:
        for g in plan['groups']:
            group={k:g[k] for k in ('id','title','persona','network')};group['chains']={}
            bindings=[]
            for chain in ('A','B'):
                bp=root/'groups'/g['id']/chain/'binding.json'
                if bp.exists():bindings.append(read(bp))
                turns=[]
                for slot in (p for p in plan['positions'] if p['group']==g['id'] and p['chain']==chain):
                    path=root/'positions'/slot['id'];row=None
                    if (path/'turn.json').exists():row=db.execute('SELECT * FROM turns WHERE id=?',(read(path/'turn.json')['turn_id'],)).fetchone()
                    value={'position':slot['id'],'user':slot['user'],'index':slot['index'],'body':None,'notice':None,'notice_draft':None,'main_draft':None,'bubbles':[],
                           'state':row['status'] if row else 'not_submitted','error':row['error_code'] if row else None,'turn_id':row['id'] if row else None}
                    if row:
                        event=db.execute('SELECT text FROM events WHERE id=?',(row['reply_event_id'],)).fetchone();value['body']=event[0] if event else None
                        n=notice(db,dict(row));value['notice']=n['text'] if n else None
                        if chain=='B':
                            draft=db.execute('SELECT body,body_hash FROM expression_main_receipts WHERE turn_id=? AND valid=1',(row['id'],)).fetchone()
                            if draft:
                                if sha(draft['body'].encode())!=draft['body_hash']:raise ValueError('ab_main_draft_changed')
                                value['main_draft']=draft['body']
                            for u in db.execute('SELECT * FROM expression_speech_units WHERE turn_id=? ORDER BY rowid',(row['id'],)):
                                if u['phase']=='search_notice':value['notice_draft']=u['body']
                                table='expression_utterances' if u['phase']=='main' else 'expression_speech_visible'
                                field='turn_id' if u['phase']=='main' else 'utterance_id';ident=row['id'] if u['phase']=='main' else u['id']
                                parts=[r[0] for r in db.execute('SELECT text FROM '+table+' WHERE '+field+'=? ORDER BY ordinal',(ident,))]
                                value['bubbles'].extend({'phase':u['phase'],'text':part} for part in parts)
                        else:value['bubbles']=([{'phase':'search_notice','text':value['notice']}] if value['notice'] else [])+([{'phase':'main','text':value['body']}] if value['body'] else [])
                        value['body_hash']=sha(value['body'].encode()) if value['body'] is not None else None
                        if (path/'finished.json').exists():
                            finished=read(path/'finished.json')
                            if value['body_hash']!=finished['reply_hash']:raise ValueError('ab_final_body_changed')
                        if chain=='B':
                            value['preview_bubbles']=[]
                            for purpose in ('expression_notice','expression'):
                                preview=root/'derived-segmentation'/slot['id']/(purpose+'.json')
                                if preview.exists():
                                    derived=read(preview);expected=value['notice'] if purpose=='expression_notice' else value['body']
                                    if expected is None or sha(expected.encode())!=derived['body_hash']:raise ValueError('ab_segmentation_preview_body_changed')
                                    value['preview_bubbles'].extend(s['text'] for s in derived['artifact']['segments'])
                    turns.append(value)
                group['chains'][chain]=turns
            if len(bindings)==2 and bindings[0]['initial_persona_sha256']!=bindings[1]['initial_persona_sha256']:raise ValueError('ab_initial_persona_mismatch')
            groups.append(group)
    rows=[];known=0;unknown=0;estimate=Decimal(0);increment=Decimal(0);jev_unknown=0;counts=Counter();elapsed=[];expression_times=[];unsent=0;exceptions=[]
    for path in sorted((root/'positions').glob('*/calls/*.dispatch.json')):
        record=read(path);purpose=record['purpose'];counts[purpose]+=1;out=path.with_name(purpose+'.outcome.json')
        outcome=read(out) if out.exists() else None;usage=outcome.get('usage') if outcome else None
        if outcome and outcome.get('state')=='error':exceptions.append({'position':record['position'],'purpose':purpose,'error_code':outcome.get('error_code')})
        diagnostic=outcome.get('diagnostics',{}) if outcome else {}
        not_dispatched=bool(outcome and outcome.get('state')=='error' and diagnostic.get('request_headers_sent') is False and diagnostic.get('response_headers_received') is False)
        if not_dispatched:unsent+=1
        if purpose in ('expression','expression_notice'):
            raw=path.with_name(purpose+'.response.raw.json')
            if raw.exists():
                from persona_runtime.provider_compat import usage_fields
                usage=usage_fields(read(raw).get('usage'),deepseek=True)
        total=(usage or {}).get('total_tokens')
        if type(total) is int:known+=total
        elif not not_dispatched:unknown+=1
        amount=None
        if purpose=='jev':jev_unknown+=1
        elif type((usage or {}).get('prompt_tokens')) is int and type((usage or {}).get('completion_tokens')) is int:
            amount=(Decimal(usage['prompt_tokens'])*2+Decimal(usage['completion_tokens'])*8)/Decimal(1000000);estimate+=amount
            if purpose in ('expression','expression_notice'):increment+=amount
        if outcome:
            elapsed.append(outcome['elapsed_ms']/1000)
            if purpose in ('expression','expression_notice'):expression_times.append(outcome['elapsed_ms']/1000)
        rows.append(record|{'outcome':outcome,'usage':usage,'estimated_cny':str(amount) if amount is not None else None,'actual_cny':None})
    complete={chain:sum(t['state']=='completed' and t['body'] is not None for g in groups for t in g['chains'][chain]) for chain in ('A','B')}
    change=sum(t['main_draft'] is not None and t['body']!=t['main_draft'] for g in groups for t in g['chains']['B'])
    controlled=len(list((root/'positions').glob('*/controlled-native.json')))
    with store.connection() as db:
        expression_states=dict(Counter(r[0] for r in db.execute('SELECT state FROM expression_runs UNION ALL SELECT state FROM expression_speech_runs')))
        segmentation_fallbacks=0
        for raw, in db.execute('SELECT result_json FROM expression_runs UNION ALL SELECT result_json FROM expression_speech_runs'):
            result_json=json.loads(raw) if raw else {};artifact=result_json.get('candidate') or result_json.get('artifact') or {}
            if artifact.get('plan_fallback'):segmentation_fallbacks+=1
    result={'version':'expression-AB-conversation-report-v1','trial_kind':plan['trial_kind'],'completed_turns':complete,
        'reserved_positions':sum(counts.values()),'known_not_dispatched':unsent,
        'client_requests':sum(counts.values())-unsent if plan['trial_kind']=='real' else 0,'offline_fixture_requests':sum(counts.values()) if plan['trial_kind']=='offline_fixture' else 0,
        'counts':dict(counts),'max_client_requests':174,'real_native_http_requests':0,'controlled_retrieval_positions':controlled,
        'B_body_different_from_own_main_draft':change,'known_tokens':known,'unknown_usage_records':unknown,
        'deepseek_conservative_estimated_cny':str(estimate),'jev_cost_unknown_records':jev_unknown,'actual_total_cny':None,
        'B_expression_increment_estimated_cny':str(increment),
        'median_B_expression_request_seconds':round(statistics.median(expression_times),3) if expression_times else None,
        'median_client_request_seconds':round(statistics.median(elapsed),3) if elapsed else None,
        'client_exceptions':exceptions,'expression_run_states':expression_states,'valid_body_segmentation_fallbacks':segmentation_fallbacks,
        'author_full_chain_gate':'pending','E4_started':False,'formal_deployed':False}
    preview_audit=root.parent/'derived-segmentation-audit.json'
    if preview_audit.exists():result['local_segmentation_compatibility_preview']=read(preview_audit)
    esc=html.escape
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E3.R3完整会话A/B审阅</title><style>'+STYLE+'</style><h1>原v8与v8＋表达转译：完整会话审阅</h1><p>8组新会话，两条链各48轮，分别维护实际发言历史。甲乙身份在同组内固定、组间随机。先看同排版文字，再展开气泡与B链源稿；只有你明确认定正文和整体交流更优，才推进正式接入。</p><p>联网语言使用两链相同的冻结公开资料／失败状态，属于受控素材。这里没有新的真实搜索；Jev及人物调用按账目另计。呈现回执为无界面模拟，未声称完成桌面体验。</p><p>完成：甲乙对应的A链 '+str(complete['A'])+' / 48、B链 '+str(complete['B'])+' / 48。失败与缺失保留显示；相同正文只计索引。</p><p>DeepSeek请求合计保守估算 ¥'+str(estimate)+'，其中B链转译增量 ¥'+str(increment)+'；实际金额及Jev费用未知。转译请求耗时中位数 '+str(result['median_B_expression_request_seconds'])+' 秒；本次模拟没有人为打字等待。</p><div class="bar"><button id="reveal">显示身份</button><button id="export">导出审阅意见</button><span id="progress"></span></div>']
    md=['# E3.R3：原v8与v8＋表达转译完整独立会话','',json.dumps(result,ensure_ascii=False),''];payload=[]
    if exceptions:
        jev_errors=sum(e['purpose']=='jev' for e in exceptions)
        parts.append('<details open><summary>首次调用异常记录：'+str(len(exceptions))+'处</summary><p>Jev请求异常 '+str(jev_errors)+' 次、正常返回 '+str(counts['jev']-jev_errors)+' 次；异常依原有策略降级并留证。这批不能作为Jev全程正常的验收记录。以下均为首次结果，没有补跑。</p><ul>'+''.join('<li>'+esc(e['position']+' · '+e['purpose']+' · '+str(e['error_code']))+'</li>' for e in exceptions)+'</ul></details>')
    parts.append('<p>'+str(expression_states.get('completed',0))+'次转译取得可用正文，其中'+str(segmentation_fallbacks)+'次分段降级为完整正文；气泡效果与正文改善分开判断。</p>')
    if preview_audit.exists():parts.append('<p>已用这50份首次返回完成分段对象兼容，正文逐字保持。修复预览单列，原呈现回执没有改写，也没有重新调用模型；原完整气泡会话仍显示首次单块降级结果。</p>')
    for g in groups:
        left=key['left_chain'][g['id']];right='B' if left=='A' else 'A';role=plan['personas'][g['persona']]
        parts.append('<section data-id="'+g['id']+'"><h2>'+esc(g['id']+' · '+g['title']+' · '+role['name'])+'</h2><p>'+esc('、'.join(role['core_traits'])+'；'+role['expression_style'])+'</p><div class="pair"><h3 class="identity" data-chain="'+left+'">方案甲</h3><h3 class="identity" data-chain="'+right+'">方案乙</h3></div>')
        md.extend(['## '+g['id']+' '+g['title'],''])
        for i in range(6):
            a,b=g['chains']['A'][i],g['chains']['B'][i]
            same=a['body'] is not None and a['body']==b['body'] and a['notice']==b['notice']
            user=a['user'];parts.append('<details'+('' if same else ' open')+'><summary>第'+str(i+1)+'轮'+(' · 正文相同' if same else ' · 查看正文')+'</summary><p class="user">'+esc(user)+'</p><div class="pair">')
            for chain in (left,right):
                t=g['chains'][chain][i]
                parts.append('<div class="side">'+('<small>查前发言</small><pre>'+esc(t['notice'])+'</pre>' if t['notice'] else '')+'<pre>'+esc(t['body'] or '［未取得人物正文：'+t['state']+'／'+str(t['error'])+'］')+'</pre></div>')
            parts.append('</div></details>')
            md.extend(['### 第'+str(i+1)+'轮','',user,'','甲：',g['chains'][left][i]['notice'] or '',g['chains'][left][i]['body'] or '［缺失］','','乙：',g['chains'][right][i]['notice'] or '',g['chains'][right][i]['body'] or '［缺失］',''])
        parts.append('<details><summary>完整气泡会话</summary><div class="pair">')
        for chain in (left,right):
            parts.append('<div>')
            for t in g['chains'][chain]:
                parts.append('<p class="user">'+esc(t['user'])+'</p>'+''.join('<pre class="bubble">'+esc(p['text'])+'</pre>' for p in t['bubbles']))
            parts.append('</div>')
        parts.append('</div></details><details><summary>B链未转译主稿与最终稿（展开会揭示身份）</summary>')
        for t in g['chains']['B']:
            if t['notice_draft'] is not None:
                parts.append('<p>第'+str(t['index'])+'轮查前发言</p><div class="pair"><div><small>查前源稿</small><pre>'+esc(t['notice_draft'])+'</pre></div><div><small>实际查前发言</small><pre>'+esc(t['notice'] or '［缺失］')+'</pre></div></div>')
            parts.append('<p>第'+str(t['index'])+'轮</p><div class="pair"><div><small>未转译主稿</small><pre>'+esc(t['main_draft'] or '［缺失］')+'</pre></div><div><small>实际最终稿</small><pre>'+esc(t['body'] or '［缺失］')+'</pre></div></div>')
        parts.append('</details>')
        if any(t.get('preview_bubbles') for t in g['chains']['B']):
            parts.append('<details><summary>B链分段兼容修复预览（未重新呈现）</summary><p>以下只改变原文切分，沿用同一首次正文；不是新的实际发送记录。</p>')
            for t in g['chains']['B']:
                parts.append('<p class="user">'+esc(t['user'])+'</p>'+''.join('<pre class="bubble">'+esc(s)+'</pre>' for s in t.get('preview_bubbles',[])))
            parts.append('</details>')
        parts.append('<div>'+''.join('<button data-choice="'+v+'" aria-pressed="false">'+caption+'</button>' for v,caption in (('left','甲更好'),('right','乙更好'),('equal','无明显差别'),('both_problem','都有问题')) )+'</div><textarea aria-label="本组意见" placeholder="正文、角色感、意思保持、整体交流与等待感受"></textarea></section>')
        payload.append({'id':g['id'],'left_chain':left,'bodies':{chain:[t.get('body_hash') for t in g['chains'][chain]] for chain in ('A','B')},
                        'notices':{chain:[sha(t['notice'].encode()) if t['notice'] is not None else None for t in g['chains'][chain]] for chain in ('A','B')}})
    data=json.dumps({'plan_sha256':ph,'groups':payload},ensure_ascii=False).replace('<','\\u003c')
    script='''const data=JSON.parse(document.getElementById('data').textContent),key='pcar-r3-ab:'+data.plan_sha256;let state=JSON.parse(localStorage.getItem(key)||'{"votes":{},"identity_exposed":false}');function save(){localStorage.setItem(key,JSON.stringify(state))}function update(){for(const s of document.querySelectorAll('section'))for(const b of s.querySelectorAll('[data-choice]'))b.setAttribute('aria-pressed',String(state.votes[s.dataset.id]?.choice===b.dataset.choice));document.getElementById('progress').textContent=Object.keys(state.votes).length+' / 8组已选'}for(const s of document.querySelectorAll('section')){for(const b of s.querySelectorAll('[data-choice]'))b.onclick=()=>{state.votes[s.dataset.id]={...(state.votes[s.dataset.id]||{}),choice:b.dataset.choice,identity_exposed:state.identity_exposed};save();update()};const n=s.querySelector('textarea');n.value=state.votes[s.dataset.id]?.note||'';n.oninput=()=>{state.votes[s.dataset.id]={...(state.votes[s.dataset.id]||{}),note:n.value};save()};s.querySelectorAll('details').forEach(d=>{if(d.querySelector('summary').textContent.startsWith('B链'))d.ontoggle=()=>{if(d.open){state.identity_exposed=true;save()}}})}document.getElementById('reveal').onclick=()=>{state.identity_exposed=true;save();for(const n of document.querySelectorAll('.identity'))n.textContent=(n.textContent.startsWith('方案甲')?'方案甲':'方案乙')+' · '+(n.dataset.chain==='A'?'原v8':'v8＋表达转译')};document.getElementById('export').onclick=()=>{const out={version:'expression-R3-AB-author-review-v1',plan_sha256:data.plan_sha256,groups:data.groups.map(g=>({...g,...state.votes[g.id]})),identity_exposed:state.identity_exposed,exported_at:new Date().toISOString()},url=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='E3.R3-完整会话A-B审阅意见.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};update();'''
    parts.append('<aside><h2>整体判断</h2><p>完成分组审阅后，请明确正文与整体交流的结论，并考虑上方额外等待和费用。</p><select id="overall"><option value="">尚未作整体判断</option value="B_better_and_cost_acceptable">转译链正文和整体交流更优，等待与费用可接受</option><option value="prefer_original_v8">更倾向原v8</option><option value="uncertain">还不能确认</option></select></aside>')
    script=script.replace('identity_exposed:state.identity_exposed,exported_at:','identity_exposed:state.identity_exposed,overall:state.overall||null,exported_at:')
    script=script.replace('Object.keys(state.votes).length','data.groups.filter(g=>state.votes[g.id]?.choice).length')
    script+='const overall=document.getElementById("overall");overall.value=state.overall||"";overall.onchange=()=>{state.overall=overall.value;save()};'
    name='E3.R3-原v8与表达转译完整会话审阅';parts.append('<script id="data" type="application/json">'+data+'</script><script>'+script+'</script></html>')
    path=root/(name+'.html');path.write_text(''.join(parts),encoding='utf-8');(root/(name+'.md')).write_text('\n'.join(md),encoding='utf-8')
    (root/'review-data.json').write_text(json.dumps({'plan_sha256':ph,'groups':groups},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (root/'billing.json').write_text(json.dumps(result|{'rows':rows},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result['html_path']=str(path);(root/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result
