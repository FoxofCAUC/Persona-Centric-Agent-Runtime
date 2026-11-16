"""从首次转译回执生成静态全文与可导出意见的研究审阅页。"""
from decimal import Decimal
import html
import json
import statistics
import unicodedata

from .study import load,read,reconcile,sha


STYLE='''body{max-width:1380px;margin:32px auto;padding:0 22px 70px;background:#f1f3ed;color:#263c32;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}h1{font-size:30px;line-height:1.35}h2{font-size:22px;margin:0}p{margin:10px 0}small,.muted{color:#627368}header{max-width:1040px}.bar{position:sticky;top:0;background:#f1f3edf5;padding:12px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #ccd8cb;z-index:2}.case{margin:24px 0;background:#fffefb;border:1px solid #d2dccf;border-radius:12px;overflow:hidden}.head,.voice,.vote,.notes{padding:17px 23px}.head{background:#f7f9f1;border-bottom:1px solid #d9e1d4}.voice{border-bottom:1px solid #d9e1d4}.columns{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid #d9e1d4}.answer{padding:23px;min-width:0}.answer+.answer{border-left:1px solid #d9e1d4}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0;tab-size:4}.label{color:#2a654c;font-weight:600;margin-bottom:12px}button,select{font:inherit;padding:7px 12px;color:inherit;background:#fff;border:1px solid #bacbbb;border-radius:7px;cursor:pointer}button[aria-pressed="true"]{background:#deeddd;border-color:#5f896b}.vote{display:flex;flex-wrap:wrap;gap:10px;padding-bottom:3px}textarea{width:100%;min-height:65px;box-sizing:border-box;padding:10px;font:inherit;border:1px solid #cbd7c5;border-radius:7px;resize:vertical}.primary{background:#2d6552;color:#fff}details{padding:10px 23px}summary{cursor:pointer}.hidden{display:none!important}.problem{padding:15px 23px;background:#fff0e4;color:#855132}table{border-collapse:collapse;width:100%;font-size:14px;margin:18px 0}td,th{border-bottom:1px solid #d2dccf;text-align:left;padding:8px}#progress{margin-left:auto;font-weight:600;color:#2a654c}footer{margin-top:35px;color:#627368}@media(max-width:800px){.columns{grid-template-columns:1fr}.answer+.answer{border-left:0;border-top:1px solid #d9e1d4}.bar{position:relative}body{padding:0 12px 50px}h1{font-size:26px}}'''

SCRIPT='''const data=JSON.parse(document.getElementById('data').textContent),storage='pcar-persona-draft-r1:'+data.plan_sha256;
let state={votes:{},exposed:false};try{const s=JSON.parse(localStorage.getItem(storage));if(s?.votes)state={...state,...s}}catch(e){}let reveal=false;
function save(){try{localStorage.setItem(storage,JSON.stringify(state));document.getElementById('save-status').textContent='意见已保存到当前浏览器。'}catch(e){document.getElementById('save-status').textContent='请导出意见保留本次选择。'}}
function update(){const type=document.getElementById('type').value;for(const card of document.querySelectorAll('.case')){const id=card.dataset.id;card.classList.toggle('hidden',type!=='all'&&card.dataset.kind!==type);for(const n of card.querySelectorAll('[data-choice]'))n.setAttribute('aria-pressed',String(state.votes[id]?.choice===n.dataset.choice));for(const n of card.querySelectorAll('.label'))n.textContent=n.dataset.label+(reveal?' · '+n.dataset.source:'');card.querySelector('.trace').classList.toggle('hidden',!reveal)}document.getElementById('progress').textContent='已选 '+data.cases.filter(c=>state.votes[c.id]?.choice).length+' / '+data.different;document.getElementById('reveal').textContent=reveal?'隐藏身份':'显示身份'}
for(const card of document.querySelectorAll('.case')){const id=card.dataset.id;for(const n of card.querySelectorAll('[data-choice]'))n.addEventListener('click',()=>{state.votes[id]={...(state.votes[id]||{}),choice:n.dataset.choice,identity_revealed_before_choice:state.exposed,updated_at:new Date().toISOString()};save();update()});const note=card.querySelector('textarea');note.value=state.votes[id]?.note||'';note.addEventListener('input',()=>{state.votes[id]={...(state.votes[id]||{}),note:note.value};save()})}
document.getElementById('type').addEventListener('change',update);document.getElementById('reveal').addEventListener('click',()=>{reveal=!reveal;if(reveal){state.exposed=true;save()}update()});
document.getElementById('export').addEventListener('click',()=>{const review={version:'expression-persona-draft-R1-author-review-v1',plan_sha256:data.plan_sha256,exported_at:new Date().toISOString(),identity_exposed:state.exposed,cases:data.cases.map(c=>({id:c.id,kind:c.kind,primary_hash:c.primary_hash,expression_hash:c.expression_hash,left_source:data.left_source[c.id],...(state.votes[c.id]||{})}))};const url=URL.createObjectURL(new Blob([JSON.stringify(review,null,2)],{type:'application/json;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='E3.R1-首批16位审阅意见.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)});update();'''


def lexical(text):return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))


def report(root):
    plan=load(root,check_source=False);key=read(root/'review-key.json');plan_hash=sha((root/'plan.json').read_bytes())
    count=len(plan['positions']);continued=count==40
    if key['plan_sha256']!=plan_hash:raise ValueError('persona_review_key_changed')
    cases=[];calls=[];known_tokens=0;unknown_usage=0;not_dispatched=0;estimate=Decimal(0);latencies=[]
    for position in plan['positions']:
        case=read(root/'inputs'/(position['id']+'.json'));result=reconcile(root,position)
        folder=root/'positions'/position['id'];dispatched=(folder/'dispatch.json').exists()
        outcome=read(folder/'outcome.json') if (folder/'outcome.json').exists() else None
        artifact=result.get('artifact') if result else None;body=artifact['body'] if artifact else None
        if body is not None:
            raw=read(folder/'response.raw.json')
            if json.loads(raw['choices'][0]['message']['content'])['body']!=body:raise ValueError('persona_review_body_changed')
        rejected_body=None
        if result and result['state']=='unusable' and (folder/'response.raw.json').exists():
            try:
                rejected=json.loads(read(folder/'response.raw.json')['choices'][0]['message']['content'])
                if isinstance(rejected,dict) and isinstance(rejected.get('body'),str):rejected_body=rejected['body']
            except (ValueError,KeyError,IndexError,TypeError):pass
        case.update(state=result['state'] if result else outcome['state'] if outcome else 'unknown' if dispatched else 'not_submitted',
            expression=body,artifact=artifact,primary_hash=sha(case['draft'].encode()),
            error_code=result.get('error_code') if result else None,rejected_body=rejected_body,
            locally_recovered=(folder/'recovered-result.json').exists(),
            expression_hash=sha(body.encode()) if body is not None else None,
            different=body is not None and body!=case['draft'],
            punctuation_only=body is not None and body!=case['draft'] and lexical(body)==lexical(case['draft']))
        cases.append(case)
        if dispatched:
            unsent=bool(outcome and outcome['state']=='not_dispatched')
            if unsent:not_dispatched+=1
            usage=result.get('usage') if result else None;total=(usage or {}).get('total_tokens')
            if type(total) is int:known_tokens+=total
            elif not unsent:unknown_usage+=1
            amount=None
            if isinstance(usage,dict) and type(usage.get('prompt_tokens')) is int and type(usage.get('completion_tokens')) is int:
                amount=(Decimal(usage['prompt_tokens'])*2+Decimal(usage['completion_tokens'])*8)/Decimal(1000000);estimate+=amount
            calls.append({'id':case['id'],'state':case['state'],'usage':usage,'not_dispatched':unsent,'estimated_cny':str(amount) if amount is not None else None,
                          'actual_cny':None,'outcome':outcome})
            if outcome:latencies.append(outcome['elapsed_ms']/1000)
    completed=sum(c['state']=='completed' for c in cases);different=sum(c['different'] for c in cases);same=sum(c['expression']==c['draft'] for c in cases)
    summary={'version':'expression-persona-draft-R1-report-v1','profile':plan['profile'],'trial_kind':plan['trial_kind'],
        'planned':count,'completed':completed,'changed':different,'unchanged':same,
        'unusable':sum(c['state']=='unusable' for c in cases),
        'local_segmentation_recoveries':sum(c['locally_recovered'] for c in cases),
        'punctuation_or_whitespace_only':sum(c['punctuation_only'] for c in cases),
        'reserved_positions':len(calls),'not_dispatched':not_dispatched,
        'real_expression_calls':len(calls)-not_dispatched if plan['trial_kind']=='real' else 0,'offline_fixture_calls':len(calls)-not_dispatched if plan['trial_kind']=='offline_fixture' else 0,
        'new_primary_calls':0,'known_tokens':known_tokens,'unknown_usage_records':unknown_usage,
        'conservative_estimated_cny':str(estimate),'actual_cny':None,
        'median_expression_request_seconds':round(statistics.median(latencies),3) if latencies else None,
        'author_value_review':'pending','remaining_40_started':continued,'E3_R2_started':False,'E4_started':False}
    esc=html.escape
    title='最小输入转译：剩余40份v2全文审阅' if continued else '最小输入转译：首批16位全文审阅'
    intro='本批为原48轮扣除首批已使用8份真实稿后的剩余40份，统一使用v2标点职责配置。首批16份v1保持，两个版本分开统计，历史v8主稿未重新生成。' if continued else '本批包含8个人工工程腔稿位置（两份稿件各由四种角色转译），以及8份已保存真实v8主稿。只调用表达模型，v8没有重新生成。'
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+title+'</title><style>'+STYLE+'</style><header><small>PCAR · E3.R1</small><h1>'+title+'</h1>',
        '<p>'+intro+'</p>',
        '<p>模型只收到角色性格与说话习惯、完整主稿。请判断说法是否更自然、更贴合角色，同时保留原意。人工稿用于检查转译能力，真实稿用于检查实际增量。</p>',
        '<p>可用结果 '+str(completed)+' / '+str(count)+'；正文有差异 '+str(different)+'，原样 '+str(same)+'。下方并排展示有差异的正文，并单列失败全文；全部位置状态见索引。甲乙逐位置随机，身份可揭示。</p></header>',
        '<div class="bar"><select id="type" aria-label="材料筛选"><option value="all">全部差异</option><option value="authored_capability_draft">人工工程腔稿</option><option value="saved_real_primary">真实v8主稿</option></select><button id="reveal">显示身份</button><button id="export" class="primary">导出意见</button><span id="progress"></span></div><p class="muted" id="save-status"></p>']
    markdown=['# '+title,'',intro,'']
    for index,c in enumerate(cases,1):
        label='人工工程腔稿' if c['kind']=='authored_capability_draft' else '真实v8主稿'
        if continued and c['expression']==c['draft']:continue
        markdown.extend(['## '+str(index).zfill(2)+' '+c['id']+' · '+c['role_name'],'',label,'','性格：'+'、'.join(c['voice']['core_traits']),'','说话习惯：'+c['voice']['style'],'','### 主稿','',c['draft'],'','### 转译稿','',c['expression'] if c['expression'] is not None else '［未取得可用转译稿］',''])
        if c['expression'] is None:
            code=c['error_code'] or c['state']
            parts.append('<article class="failure"><div class="head"><h2>失败场景'+str(index).zfill(2)+' · '+esc(c['title'])+'</h2><p>'+esc(c['id'])+' · '+esc(c['role_name'])+' · '+esc(code)+'</p></div><p>这份响应已被拒绝，未计入可用转译。保留首次返回，不补写或重跑。正式运行在尚未呈现时可回退到完整主稿。</p><div class="columns"><div class="answer"><div class="label">完整主稿</div><pre>'+esc(c['draft'])+'</pre></div><div class="answer"><div class="label">被拒绝的模型正文</div><pre>'+esc(c['rejected_body'] or '［没有可读取的正文，详见原始回执］')+'</pre></div></div></article>')
            markdown.extend(['### 首次失败返回（被拒绝）','',c['rejected_body'] or '［没有可读取的正文］','','原因：'+code,''])
            continue
        if not c['different']:continue
        left=key['left_source'][c['id']];right='expression' if left=='primary' else 'primary'
        parts.append('<article class="case" data-id="'+esc(c['id'])+'" data-kind="'+c['kind']+'"><div class="head"><h2>场景'+str(index).zfill(2)+' · '+esc(c['title'])+'</h2><small>'+esc(c['id'])+' · '+label+' · '+esc(c['role_name'])+'</small></div><div class="voice"><strong>性格：</strong>'+esc('、'.join(c['voice']['core_traits']))+'<br><strong>说话习惯：</strong>'+esc(c['voice']['style'])+'</div>')
        if c['punctuation_only']:parts.append('<p class="muted" style="padding:0 23px">本轮文字差异仅涉及标点、空白或引号。</p>')
        if c['locally_recovered']:parts.append('<p class="muted" style="padding:0 23px">首次响应正文完整，分段格式不符；保留原响应后，按完整正文单块降级。没有新增调用。</p>')
        parts.append('<div class="columns">')
        for caption,source in (('方案甲',left),('方案乙',right)):
            text=c['draft'] if source=='primary' else c['expression']
            identity=('人工工程腔原稿' if c['kind']=='authored_capability_draft' else '已保存v8主稿') if source=='primary' else '角色转译稿'
            parts.append('<div class="answer"><div class="label" data-label="'+caption+'" data-source="'+identity+'">'+caption+'</div><pre>'+esc(text)+'</pre></div>')
        parts.append('</div><div class="vote">'+''.join('<button data-choice="'+value+'" aria-pressed="false">'+caption+'</button>' for value,caption in (('left','甲更合适'),('right','乙更合适'),('equal','无明显差别'),('both_problem','两份都有问题')))+
            '</div><div class="notes"><textarea aria-label="'+esc(c['id'])+'备注" placeholder="自然度、角色口吻、意思是否保持……"></textarea></div><details class="trace hidden"><summary>查看来源和气泡计划</summary><p>'+esc(c['source'].get('old_case_id','人工能力稿，非主模型真实输出'))+'</p>'+''.join('<pre>'+esc(s['text'])+'</pre>' for s in c['artifact']['segments'])+'</details></article>')
    parts.append('<h2>全部'+str(count)+'个位置索引</h2><table><thead><tr><th>序号／ID</th><th>角色</th><th>材料</th><th>结果</th></tr></thead><tbody>')
    for i,c in enumerate(cases,1):
        state='正文有差异' if c['different'] else '正文原样（未自动计为作者评分）' if c['expression'] is not None else c['state']+' · '+str(c['error_code'])
        parts.append('<tr><td>'+str(i).zfill(2)+' / '+esc(c['id'])+'</td><td>'+esc(c['role_name'])+'</td><td>'+('人工稿' if c['kind']=='authored_capability_draft' else '真实主稿')+'</td><td>'+esc(state)+'</td></tr>')
    status='本批为研究回归，不代表完整多轮链路已获优越性认可。统一出口和真实桌面呈现仍按后续阶段验收。' if continued else '当前等待作者确认转译价值，剩余40份、统一出口适配和E4尚未启动。气泡为计划，不代表已完成桌面体验验收。'
    if continued:
        markdown.extend(['## 全部40个位置索引','']+[c['id']+'：'+(c['error_code'] or ('正文有差异' if c['different'] else '正文原样')) for c in cases]+[''])
    parts.append('</tbody></table><footer><p>已知tokens：'+str(known_tokens)+'；未知用量记录：'+str(unknown_usage)+'。按官方峰值价格保守估算 ¥'+str(estimate)+'，实际金额未知。</p><p>'+status+'</p></footer>')
    payload={'plan_sha256':plan_hash,'different':different,'cases':[{'id':c['id'],'kind':c['kind'],'primary_hash':c['primary_hash'],'expression_hash':c['expression_hash']} for c in cases],
             'left_source':key['left_source']}
    encoded=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    script=SCRIPT.replace('pcar-persona-draft-r1:','pcar-persona-draft-r1c:').replace('E3.R1-首批16位审阅意见.json','E3.R1续-40份v2审阅意见.json') if continued else SCRIPT
    parts.append('<script id="data" type="application/json">'+encoded+'</script><script>'+script+'</script></html>')
    name='E3.R1续-剩余40份v2全文审阅' if continued else 'E3.R1-最小输入转译16位全文审阅'
    document=root/(name+'.html');document.write_text(''.join(parts),encoding='utf-8')
    (root/(name+'.md')).write_text('\n'.join(markdown),encoding='utf-8')
    (root/'review-data.json').write_text(json.dumps({'plan_sha256':plan_hash,'cases':cases,'left_source':key['left_source']},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (root/'billing.json').write_text(json.dumps(summary|{'rows':calls},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    summary['html_path']=str(document);(root/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary
