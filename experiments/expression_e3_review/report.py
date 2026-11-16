"""从首次回执生成全文审阅，报告重建不派发模型。"""
from contextlib import closing
from decimal import Decimal
import html
import json
from pathlib import Path
import sqlite3

from persona_runtime.deepseek_native_contract import tool_accounting
from persona_runtime.knowledge_search_notice import read as read_notice

from .common import digest,load,read,sha,write_once


def collect(root,plan):
    cases=[]
    database=root/'instance/data/database/runtime.sqlite3'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for position in plan['positions']:
            group=next(g for g in plan['fixtures']['groups'] if g['id']==position['group_id'])
            case=position|{'title':group['title'],'role':plan['fixtures']['personas'][position['persona']],
                'primary':None,'expression':None,'actual_spoken':None,'eligible':False,'history':[],'notice':None,
                'status':'not_submitted','producer':None,'web':{},'bubbles':[],'errors':[]}
            turn_path=root/'positions'/position['id']/'turn.json'
            if turn_path.exists():
                saved=read(turn_path);turn=db.execute('SELECT * FROM turns WHERE id=?',(saved['turn_id'],)).fetchone()
                if not turn:raise ValueError('review_turn_missing')
                case.update(turn_id=turn['id'],status=turn['status'],error_code=turn['error_code'])
                draft=db.execute('SELECT * FROM expression_main_receipts WHERE turn_id=?',(turn['id'],)).fetchone()
                run=db.execute('SELECT * FROM expression_runs WHERE turn_id=?',(turn['id'],)).fetchone()
                pub=db.execute('SELECT * FROM expression_publications WHERE turn_id=?',(turn['id'],)).fetchone()
                if draft and draft['valid']:
                    if sha(draft['body'].encode())!=draft['body_hash'] or sha(draft['raw_text'].encode())!=draft['raw_hash']:
                        raise ValueError('review_main_receipt_changed')
                    case['primary']=draft['body'];case['primary_hash']=draft['body_hash']
                if run:
                    case.update(expression_state=run['state'],expression_attempt_id=run['attempt_id'])
                    raw=bytes(run['raw_response']) if run['raw_response'] is not None else None
                    if raw is not None and sha(raw)!=run['raw_response_hash']:raise ValueError('review_expression_receipt_changed')
                    if run['result_json']:
                        result=json.loads(run['result_json'])
                        if result.get('candidate'):case['expression']=result['candidate']['body']
                    if case['expression'] is not None and raw is not None:
                        original=json.loads(json.loads(raw)['choices'][0]['message']['content'])
                        if original['body']!=case['expression']:raise ValueError('review_expression_body_changed')
                    case['expression_usage']=json.loads(run['usage_json']) if run['usage_json'] else None
                if pub:
                    case.update(producer=pub['producer_kind'],publication_state=pub['state'],publication_reason=pub['reason'])
                    case['bubbles']=[dict(r) for r in db.execute('SELECT ordinal,text FROM expression_utterances WHERE publication_id=? ORDER BY ordinal',(pub['id'],))]
                    event=db.execute('SELECT text FROM events WHERE id=?',(pub['canonical_event_id'],)).fetchone()
                    case['actual_spoken']=event[0] if event else None
                    if event and event[0]!=''.join(r['text'] for r in case['bubbles']):raise ValueError('review_actual_spoken_changed')
                case['eligible']=bool(case['primary'] is not None and case['expression'] is not None and pub and pub['producer_kind']=='artifact' and pub['state']=='complete')
                if case['eligible'] and case['actual_spoken']!=case['expression']:raise ValueError('review_expression_actual_spoken_mismatch')
                case['history']=[dict(r) for r in db.execute('SELECT role,text FROM events WHERE session_id=? AND persona_id=? AND rowid<(SELECT rowid FROM events WHERE id=?) ORDER BY rowid',(turn['session_id'],turn['persona_id'],turn['user_event_id']))]
                notice=read_notice(db,dict(turn));case['notice']=notice['text'] if notice else None
                retrieval=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.conversation.record.'+turn['id'],)).fetchone()
                if retrieval:
                    record=json.loads(retrieval[0])['data'];meta=record['retrieval']
                    case['web']={k:meta.get(k) for k in ('needed','used_previous','web_decision','search_outcome','material_acquired','errors','execution_profile')}
                    case['web']['sources']=[{'title':s['title'],'url':s['url']} for s in record.get('sources',[])]
                for purpose in ('primary','expression','jev','notice','native','tool_search'):
                    outcome=root/'positions'/position['id']/'calls'/(purpose+'.outcome.json')
                    if outcome.exists() and read(outcome)['state']=='error':case['errors'].append({'purpose':purpose,'error_code':read(outcome)['error_code']})
            case['expression_hash']=sha(case['expression'].encode()) if case['expression'] is not None else None
            cases.append(case)
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('review_database_integrity_failed')
    return cases


def billing(root,plan,cases):
    case_map={c['id']:c for c in cases};rows=[];estimate=Decimal(0);tools=Decimal(0)
    unknown_usage=0;unknown_tools=0;known_tokens=0;known_http=0;unconfirmed=0;not_sent=0
    for path in sorted((root/'positions').glob('*/calls/*.dispatch.json')):
        dispatch=read(path);purpose=dispatch['purpose'];outcome=path.with_name(purpose+'.outcome.json')
        row=read(outcome) if outcome.exists() else dispatch|{'state':'unknown'}
        diag=row.get('diagnostics') or {};unsent=row['state']!='returned' and diag.get('request_headers_sent') is False
        if unsent:not_sent+=1
        elif row['state']=='returned' or diag.get('request_headers_sent') is True:known_http+=1
        else:unconfirmed+=1
        measured=case_map[row['position']].get('expression_usage') if purpose=='expression' else row.get('usage')
        row['usage']=measured;row['not_dispatched']=unsent;row['estimated_model_cny']=None
        if purpose=='tool_search':
            amount=(measured or {}).get('estimated_amount_cny')
            if amount is not None:tools+=Decimal(amount)
            elif not unsent:unknown_tools+=1
        else:
            total=(measured or {}).get('total_tokens')
            if type(total) is int:known_tokens+=total
            elif not unsent:unknown_usage+=1
            if purpose!='jev' and isinstance(measured,dict) and type(measured.get('prompt_tokens')) is int and type(measured.get('completion_tokens')) is int:
                amount=(Decimal(measured['prompt_tokens'])*2+Decimal(measured['completion_tokens'])*8)/Decimal(1000000)
                row['estimated_model_cny']=str(amount);estimate+=amount
            if purpose=='native':
                terminal=row['state']=='returned' or (diag.get('native_terminal') in ('completed','failed') and diag.get('stream_complete') is True)
                account=tool_accounting(measured,terminal=terminal);row['tool_accounting']=account
                if account['estimated_tool_amount'] is None:unknown_tools+=1
                else:tools+=Decimal(account['estimated_tool_amount'])
        rows.append(row)
    return {'trial_kind':plan['trial_kind'],'reserved_positions':len(rows),
        'known_real_http_calls':known_http if plan['trial_kind']=='real' else 0,'offline_fixture_calls':known_http if plan['trial_kind']=='offline_fixture' else 0,
        'possible_unconfirmed_http_calls':unconfirmed,'not_dispatched':not_sent,'known_tokens_all_providers':known_tokens,
        'unknown_usage_records':unknown_usage,'unknown_tool_usage_records':unknown_tools,
        'known_deepseek_model_estimate_cny':str(estimate),'known_tool_estimate_cny':str(tools),
        'known_deepseek_and_tools_estimate_cny':str(estimate+tools),'jev_fee_cny':None,'actual_total_cny':None,
        'estimation':'DeepSeek peak rates: prompt CNY2/M at cache miss, completion CNY8/M; known tool receipts separately; Jev and actual total unknown',
        'pricing_source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'by_purpose':{p:sum(r['purpose']==p and not r['not_dispatched'] for r in rows) for p in plan['limits']},
        'rows':sorted(rows,key=lambda r:r['ordinal'])}


PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>E3 扩展全文对照审阅</title><style>
:root{color-scheme:light;--ink:#253a32;--muted:#63746c;--line:#d8e0d8;--paper:#fffefb;--accent:#2d6552}*{box-sizing:border-box}body{margin:0;background:#f1f3ed;color:var(--ink);font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1440px;margin:auto;padding:30px 24px 90px}h1{font-size:32px;line-height:1.3;letter-spacing:.02em;margin:0 0 12px}h2{font-size:23px;line-height:1.4}h3{font-size:17px;margin:0 0 14px}p{margin:10px 0}.intro{max-width:1050px}.muted,small{color:var(--muted)}.toolbar{position:sticky;top:0;z-index:3;background:#f1f3edf5;border-bottom:1px solid var(--line);backdrop-filter:blur(10px);padding:12px 0;margin:20px 0;display:flex;align-items:center;flex-wrap:wrap;gap:9px}button,select{font:inherit;color:inherit;background:var(--paper);border:1px solid #c4d0c4;border-radius:7px;padding:7px 12px;cursor:pointer}button:hover{border-color:var(--accent)}.primary{background:var(--accent);color:white;border-color:var(--accent)}#progress{margin-left:auto;color:var(--accent);font-weight:600}.group{margin:36px 0 14px;padding:5px 0 10px;border-bottom:2px solid #c8d5c7}.role{color:var(--muted);max-width:1050px}.case{background:var(--paper);margin:19px 0;border:1px solid var(--line);border-radius:12px;overflow:hidden;scroll-margin-top:95px}.case-head{padding:19px 24px;background:#f9faf5;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}.badge{font-size:13px;border:1px solid #cbd9c9;color:#536d5d;border-radius:5px;padding:0 7px}.prompt{padding:18px 24px}.prompt strong{color:var(--accent)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0;tab-size:4}.columns{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.answer{padding:24px;min-width:0}.answer+.answer{border-left:1px solid var(--line)}.label{font-size:14px;color:var(--accent);letter-spacing:.05em;margin-bottom:13px}.vote{display:flex;gap:10px;flex-wrap:wrap;padding:17px 24px 4px}.vote button[aria-pressed="true"]{background:#dcebdc;border-color:#6a9379;font-weight:600}.notes{padding:9px 24px 20px}textarea{resize:vertical;min-height:65px;width:100%;border:1px solid #d6ded3;border-radius:7px;background:#fff;font:inherit;padding:10px 12px}details{padding:10px 24px;color:#52695a}summary{cursor:pointer}details pre{color:var(--ink);padding:12px 0}details p{margin:5px 0}.notice{border-left:3px solid #a5bba8;padding-left:13px;margin:10px 0 0}.problem{color:#8b4e2b;background:#faf1e8;padding:10px 20px}a{color:var(--accent)}.hidden{display:none!important}.status{min-height:1.5em;color:var(--muted);font-size:14px}footer{margin-top:45px;color:var(--muted)}@media(max-width:800px){main{padding:22px 13px 60px}h1{font-size:27px}.columns{grid-template-columns:1fr}.answer+.answer{border-left:0;border-top:1px solid var(--line)}.answer,.prompt{padding:18px}.toolbar{position:relative}#progress{width:100%;margin:0}}
</style><main><header><small>PCAR · E3 / 全文审阅</small><h1>主稿与表达：48轮扩展对照</h1><div class="intro"><p id="overview"></p><p>每一轮都比较同一个请求的已保存主稿和独立表达稿。同组后续问题接续的是实际表达稿。全文保持首次结果，甲乙顺序逐轮随机；点击“显示身份”可看主稿／表达稿标签。</p><p>选择更自然、合适且保留任务内容的一份，也可以选“接近”或“两份都有问题”，再记下原因。选择保存在当前浏览器；完成后导出审阅意见。这里的呈现回执来自无界面模拟端，桌面节奏在E4验证。</p></div></header>
<div class="toolbar"><select id="scene" aria-label="场景筛选"><option value="all">全部场景</option></select><select id="role" aria-label="角色筛选"><option value="all">全部角色</option></select><select id="filter" aria-label="审阅筛选"><option value="all">全部轮次</option><option value="different">正文有差异</option><option value="identical">正文完全相同</option><option value="pending">待审阅</option><option value="voted">已选择</option><option value="network">联网链路组</option><option value="problem">异常或未取得独立表达</option></select><button id="reveal">显示身份</button><button id="export" class="primary">导出审阅意见</button><span id="progress"></span></div><div class="status" id="save-status" role="status"></div><div id="content"></div><footer><p id="billing"></p><p>原始调用、请求、主稿与表达回执随文档保存在本地证据目录。前一轮三组对照保持原文件，本轮没有重跑。</p></footer></main>
<script type="application/json" id="payload">__PAYLOAD__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent),key='pcar-e3-expanded:'+data.plan_sha256;
let state={votes:{},identityExposed:false};try{const saved=JSON.parse(localStorage.getItem(key));if(saved&&saved.votes)state={...state,...saved}}catch(e){}
let reveal=false;const byId=Object.fromEntries(data.cases.map(c=>[c.id,c]));
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}
function persist(){try{localStorage.setItem(key,JSON.stringify(state));document.getElementById('save-status').textContent='选择已保存在当前浏览器，可随时导出。'}catch(e){document.getElementById('save-status').textContent='浏览器未允许本地保存，请用“导出审阅意见”保留本次选择。'}}
function update(){const scene=document.getElementById('scene').value,role=document.getElementById('role').value,filter=document.getElementById('filter').value;let count=0;
for(const card of document.querySelectorAll('.case')){const c=byId[card.dataset.id],v=state.votes[c.id];const show=(scene==='all'||c.group_id===scene)&&(role==='all'||c.persona===role)&&(filter==='all'||filter==='different'&&c.eligible&&c.primary!==c.expression||filter==='identical'&&c.eligible&&c.primary===c.expression||filter==='pending'&&c.eligible&&!v?.choice||filter==='voted'&&v?.choice||filter==='network'&&c.network||filter==='problem'&&(!c.eligible||c.errors.length));card.classList.toggle('hidden',!show);if(show)count++;
for(const b of card.querySelectorAll('[data-choice]'))b.setAttribute('aria-pressed',String(v?.choice===b.dataset.choice));for(const label of card.querySelectorAll('.label'))label.textContent=label.dataset.label+(reveal?' · '+label.dataset.source:'');card.querySelector('.identity-details').classList.toggle('hidden',!reveal)}
for(const heading of document.querySelectorAll('.group'))heading.classList.toggle('hidden',![...document.querySelectorAll('.case[data-group="'+heading.dataset.group+'"]')].some(c=>!c.classList.contains('hidden')));
document.getElementById('progress').textContent='已选 '+Object.values(state.votes).filter(v=>v.choice).length+' / '+data.eligible+' · 当前 '+count+' 轮';document.getElementById('reveal').textContent=reveal?'隐藏身份':'显示身份'}
document.getElementById('overview').textContent=data.real?'新增 '+data.cases.length+' 轮，'+data.groups.length+' 类场景、'+Object.keys(data.personas).length+' 种角色风格；'+data.eligible+' 轮取得完整对照，其中 '+data.cases.filter(c=>c.eligible&&c.primary!==c.expression).length+' 轮正文有差异，'+data.cases.filter(c=>c.eligible&&c.primary===c.expression).length+' 轮完全相同。可用筛选器分别查看。':'本页为离线模拟客户端验证，真实模型调用为0。';
for(const group of data.groups){document.getElementById('scene').append(new Option(group.id+' '+group.title,group.id))}for(const [id,r]of Object.entries(data.personas))document.getElementById('role').append(new Option(r.name,id));
const content=document.getElementById('content');for(const group of data.groups){const h=el('div',undefined,'group');h.dataset.group=group.id;h.append(el('h2',group.id+' · '+group.title));h.append(el('p',data.personas[group.persona].name+'：'+data.personas[group.persona].expression_style,'role'));content.append(h);
for(const c of data.cases.filter(c=>c.group_id===group.id)){const card=el('article',undefined,'case');card.dataset.id=c.id;card.dataset.group=c.group_id;const head=el('div',undefined,'case-head');head.append(el('strong',c.id));head.append(el('span',c.network?'联网链路组':'本地交流','badge'));if(c.primary!==null&&c.primary===c.expression)head.append(el('span','两份全文相同','badge'));card.append(head);const prompt=el('div',undefined,'prompt');prompt.append(el('strong','对方说'));prompt.append(el('pre',c.user));if(c.notice){const n=el('div',undefined,'notice');n.append(el('small','本轮已经说出的查找前发言，两份方案共用'));n.append(el('pre',c.notice));prompt.append(n)}card.append(prompt);
if(c.history.length){const history=el('details');history.append(el('summary','查看本轮前文'));for(const m of c.history){history.append(el('small',m.role==='user'?'对方':'角色实际发言'));history.append(el('pre',m.text))}card.append(history)}
if(!c.eligible)card.append(el('div','本轮未取得两份独立有效全文：'+(c.error_code||c.publication_reason||c.status)+'。原始记录保留。','problem'));
if(c.web.needed&&c.web.errors?.length&&c.web.search_outcome!=='completed')card.append(el('div','本轮新增联网查找未完整结束，原有资料和失败状态均已保留；两份回复共用这一状态。','problem'));
const columns=el('div',undefined,'columns');const left=data.left_source[c.id],right=left==='primary'?'expression':'primary';for(const [label,source]of [['方案甲',left],['方案乙',right]]){const answer=el('div',undefined,'answer'),title=el('div',label,'label');title.dataset.label=label;title.dataset.source=source==='primary'?'已保存主稿':'表达稿';answer.append(title);answer.append(el('pre',c[source]??'［未取得独立有效正文］'));columns.append(answer)}card.append(columns);
const vote=el('div',undefined,'vote');for(const [choice,label]of [['left','甲更合适'],['right','乙更合适'],['equal','接近／无明显区别'],['both_problem','两份都有问题']]){const button=el('button',label);button.dataset.choice=choice;button.disabled=!c.eligible;button.addEventListener('click',()=>{state.votes[c.id]={...(state.votes[c.id]||{}),choice,identity_revealed_before_choice:state.identityExposed,updated_at:new Date().toISOString()};persist();update()});vote.append(button)}card.append(vote);const notes=el('div',undefined,'notes'),text=el('textarea');text.setAttribute('aria-label',c.id+'审阅备注');text.placeholder='可记下：哪里更自然、是否多话、是否漏条件或改变语气……';text.value=state.votes[c.id]?.note||'';text.addEventListener('input',()=>{state.votes[c.id]={...(state.votes[c.id]||{}),note:text.value,updated_at:new Date().toISOString()};persist()});notes.append(text);card.append(notes);
const info=el('details',undefined,'identity-details hidden');info.append(el('summary','查看联网、气泡和调用状态'));info.append(el('pre',JSON.stringify({status:c.status,expression_state:c.expression_state,producer:c.producer,web:c.web,errors:c.errors},null,2)));if(c.bubbles.length){info.append(el('p','模拟呈现的实际气泡'));for(const bubble of c.bubbles)info.append(el('pre',bubble.text))}card.append(info);content.append(card)}}
for(const id of ['scene','role','filter'])document.getElementById(id).addEventListener('change',update);
document.getElementById('reveal').addEventListener('click',()=>{reveal=!reveal;if(reveal){state.identityExposed=true;persist()}update()});
document.getElementById('export').addEventListener('click',()=>{const review={version:'expression-E3-expanded-author-review-v1',plan_sha256:data.plan_sha256,exported_at:new Date().toISOString(),identity_exposed:state.identityExposed,cases:data.cases.map(c=>({id:c.id,primary_hash:c.primary_hash||null,expression_hash:c.expression_hash||null,left_source:data.left_source[c.id],...(state.votes[c.id]||{})}))};const url=URL.createObjectURL(new Blob([JSON.stringify(review,null,2)],{type:'application/json;charset=utf-8'})),a=el('a');a.href=url;a.download='E3-扩展审阅意见.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)});
document.getElementById('billing').textContent='已知真实API请求 '+data.billing.known_real_http_calls+' 次；已知tokens '+data.billing.known_tokens_all_providers+'，未知用量记录 '+data.billing.unknown_usage_records+'。DeepSeek与已知工具费保守估算 ¥'+data.billing.known_deepseek_and_tools_estimate_cny+'，Jev费用及实际总额未知。';update();
</script></html>'''


def report(root):
    plan=load(root,check_source=False);cases=collect(root,plan);bill=billing(root,plan,cases);key=read(root/'review-key.json')
    if key['plan_sha256']!=sha((root/'plan.json').read_bytes()):raise ValueError('review_anonymous_key_changed')
    eligible=sum(c['eligible'] for c in cases)
    payload={'plan_sha256':key['plan_sha256'],'real':plan['trial_kind']=='real','cases':cases,'eligible':eligible,
             'groups':plan['fixtures']['groups'],'personas':plan['fixtures']['personas'],'left_source':key['left_source'],'billing':bill}
    data=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    document=root/'E3-48轮主稿与表达全文审阅.html';document.write_text(PAGE.replace('__PAYLOAD__',data),encoding='utf-8')
    lines=['# E3扩展全文对照','',f'计划{len(cases)}轮，可比较全文{eligible}轮。首次结果保持；以下明确标注主稿与表达稿。','']
    for c in cases:
        lines.extend(['## '+c['id']+' '+c['title'],'','人物：'+c['role']['name'],'','对方：'+c['user'],''])
        if c['notice']:lines.extend(['查找前已说：'+c['notice'],''])
        lines.extend(['### 已保存主稿','',c['primary'] if c['primary'] is not None else '［未取得完整主稿］','',
                      '### 表达稿','',c['expression'] if c['expression'] is not None else '［未取得独立有效表达稿］',''])
    (root/'E3-48轮主稿与表达全文.md').write_text('\n'.join(lines),encoding='utf-8')
    (root/'review-data.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (root/'billing.json').write_text(json.dumps(bill,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    summary={'version':'expression-E3-expanded-review-report-v1','trial_kind':plan['trial_kind'],'planned_pairs':len(cases),
        'eligible_pairs':eligible,'groups':len(plan['fixtures']['groups']),'role_styles':len(plan['fixtures']['personas']),
        'identical_text_pairs':sum(c['eligible'] and c['primary']==c['expression'] for c in cases),
        'unknown_expression':sum(c.get('expression_state')=='unknown' for c in cases),
        'web_decision_count':bill['by_purpose']['jev'],'native_request_count':bill['by_purpose']['native'],
        'cache_followup_count':sum(bool(c['web'].get('used_previous') and not c['web'].get('needed')) for c in cases),
        'author_review':'pending','E4_started':False,'html_path':str(document),'billing':{k:v for k,v in bill.items() if k!='rows'}}
    (root/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary
