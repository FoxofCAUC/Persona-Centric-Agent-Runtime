"""Build the E.N5 private review from two bound desktop sessions and their receipts."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.deepseek_native_contract import PRICE_VERSION, tool_accounting
from persona_runtime.knowledge_native_store import load_receipt
from persona_runtime.models import LOCAL_USER_ID, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.turn_input import load_turn_input


TARGET = 15
PHASES = {'conversation_web_decision':'jev', 'conversation_search_notice':'notice',
          'conversation_native':'native', 'conversation_search':'search', 'conversation_fetch':'fetch'}
STAGE_NAMES = {'main':'主回复', 'jev':'Jev 判断', 'notice':'查前发言', 'native':'原生检索',
               'search':'补充搜索', 'fetch':'补充读取'}
COVERAGE = {'topic_switch':'话题切换', 'serious_task':'认真任务', 'followup':'承接前文的追问'}


def require(test, code):
    if not test:
        raise ValueError('en5_report_' + code)


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


def epoch(stamp):
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp)
    return value.timestamp() if value.tzinfo else None


def elapsed(start, end):
    if start is None or end is None or end < start:
        return None
    return round(end-start, 4)


def obj(raw):
    return json.loads(raw or 'null') or {}


def known_count(value):
    return value if type(value) is int and value >= 0 else None


def official(model, name):
    try:
        require_generation_provider(model)
    except ValueError:
        return False
    return model.get('name') == name


def public_http(diag):
    return (diag.get('http_status') == 200 and diag.get('network_route') == 'system_proxy'
            and diag.get('request_headers_sent') is True)


def get_control(db, key):
    row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
    return obj(row[0]).get('data',{}) if row else {}


def model_estimate(stage, model, usage):
    from .corpus_reply_trial_report import estimated_peak
    if stage not in ('main','native','notice') or not (official(model,'deepseek-flash') or official(model,'deepseek-v4-pro')):
        return None
    keys=('prompt_cache_hit_tokens','prompt_cache_miss_tokens','prompt_tokens','completion_tokens')
    if any(known_count(usage.get(k)) is None for k in keys) or usage[keys[0]]+usage[keys[1]]!=usage['prompt_tokens']:
        return None
    return estimated_peak('U' if model['name']=='deepseek-flash' else 'D',usage)


def gen_call(stage, model, attempt_id, status, start, usage, diagnostics, code=None):
    elapsed_ms=diagnostics.get('elapsed_ms')
    total=known_count(usage.get('total_tokens'))
    return {'stage':stage,'attempt_id':attempt_id,'model':model.get('name'),'state':status,
            'provider_identity':fingerprint({k:model.get(k) for k in ('base_url','name')}),
            'started_epoch':start,'elapsed_ms':elapsed_ms if type(elapsed_ms) in (int,float) else None,
            'http_status':diagnostics.get('http_status'),'network_route':diagnostics.get('network_route'),
            'sent':diagnostics.get('request_headers_sent'), 'remote_http':public_http(diagnostics),
            'tokens':total,'error':code,'reference_peak_model_cny':model_estimate(stage,model,usage),
            'estimated_tool_cny':None,'actual_cny':None}


def main_calls(db, tid, body_hash, published_attempt):
    calls=[]
    matched=0
    for row in db.execute('SELECT * FROM attempts WHERE turn_id=? ORDER BY started_epoch',(tid,)):
        model=obj(row['model_snapshot'])
        usage=obj(row['usage_json'])
        physical=usage.get('generation_retry',{}).get('attempts')
        if not physical:
            calls.append(gen_call('main', model,row['id'],row['status'],row['started_epoch'],usage,{},row['error_code']))
            continue
        for attempt in physical:
            diag=attempt.get('diagnostics') or {}
            call=gen_call('main',model,attempt['id'],attempt['status'],attempt.get('started_epoch'),
                          attempt.get('usage') or {},diag,attempt.get('error_code'))
            calls.append(call)
            if (row['id']==published_attempt and row['status']==attempt['status']=='completed' and body_hash
                    and attempt.get('output_fingerprint')==body_hash and official(model,'deepseek-v4-pro')
                    and public_http(diag) and diag.get('stream_complete') is True and diag.get('finish_reason')=='stop'
                    and diag.get('thinking')=='disabled' and diag.get('response_model') in (None,'deepseek-v4-pro')):
                matched+=1
    return calls, matched==1


def web_calls(db, turn):
    calls=[]
    verified_web=[]
    for task in db.execute("""SELECT * FROM organization_tasks WHERE persona_id=? AND session_id=? AND channel='desktop'
           AND json_extract(payload_json,'$.knowledge_processing.root_turn_id')=? ORDER BY rowid""",
           (turn['persona_id'],turn['session_id'],turn['id'])):
        spec=obj(task['payload_json'])['knowledge_processing']
        stage=PHASES.get(spec.get('stage'))
        if not stage:
            continue
        for attempt in db.execute("""SELECT a.*, r.diagnostic_json, r.response_text FROM organization_attempts a
                LEFT JOIN organization_receipts r ON r.attempt_id=a.id WHERE a.task_id=? ORDER BY a.started_epoch""",
                (task['id'],)):
            model=obj(attempt['model_snapshot'])
            receipt=obj(attempt['diagnostic_json']); diag=receipt.get('provider') or {}
            usage=obj(attempt['usage_json'])
            call=gen_call(stage,model,attempt['id'],attempt['status'],attempt['started_epoch'],usage,diag,attempt['error_code'])
            call['task_id']=task['id']
            text=attempt['response_text']
            call['response_sha256']=digest(text) if isinstance(text,str) and receipt.get('response_sha256')==digest(text) else None
            if stage in ('search','fetch'):
                call['estimated_tool_cny']=usage.get('estimated_amount_cny')
            if stage=='notice':
                call['valid_notice']= (task['status']==attempt['status']=='completed' and public_http(diag)
                                        and official(model,'deepseek-v4-pro'))
            if stage=='native':
                verified=False
                if receipt.get('native_receipt_sha256'):
                    try:
                        packet=load_receipt(db,task,attempt['id'],expected_hash=receipt['native_receipt_sha256'])
                    except RuntimeErrorBase:
                        packet={}
                    used=[v.get('type') for s in packet.get('assistant',{}).get('server_tool_calls',[])
                          for v in s.get('calls',[]) if v.get('status')=='completed']
                    call['completed_tools']=dict(Counter(used))
                    call['estimated_tool_cny']=tool_accounting(usage,terminal=packet.get('outcome') in
                                                               ('completed','failed'))['estimated_tool_amount']
                    verified=(call['response_sha256'] is not None and public_http(diag) and task['status']==attempt['status']=='completed'
                              and official(model,'deepseek-flash') and packet.get('outcome')=='completed'
                              and diag.get('thinking')=='enabled' and model.get('_native_web_profile',{}).get('mode')=='deep'
                              and any(k in ('deepseek_web_search','deepseek_read_url') for k in used))
                if verified:
                    urls={row['url'] for step in packet['assistant'].get('server_tool_calls',[])
                          for event in step.get('calls',[]) if event.get('status')=='completed'
                          for row in [*event.get('sources',[]),event.get('page')]
                          if isinstance(row,dict) and isinstance(row.get('url'),str)}
                    verified_web.append((task['id'],attempt['started_epoch'],urls))
            calls.append(call)
    return calls, verified_web


def checked_paint(paints, phase, source_id, body, start):
    row=paints.get(phase)
    if not row or not source_id or not body:
        return None
    at=epoch(row['first_visible_at'])
    if (row.get('phase')==phase and row.get('source_id')==source_id
            and row.get('body_sha256')==digest(body) and at is not None
            and elapsed(start,at) is not None):
        return at
    return None


def web_provenance(db, turn, retrieval, prior_sources, web, extra, pre_at):
    info=retrieval.get('retrieval',{})
    source_ids={r['id'] for r in retrieval.get('sources',[]) if r.get('id')}
    ordered_search=bool(web) and pre_at is not None and all(pre_at<=started for _,started,_ in web)
    urls={url for _,_,found in web for url in found}
    earned={r['id'] for r in retrieval.get('sources',[]) if r.get('id') and r.get('url') in urls}
    acquired=ordered_search and info.get('material_acquired') is True and bool(earned)
    consumed=db.execute('SELECT details_json FROM context_uses WHERE turn_id=? AND persona_id=? AND user_id=? ORDER BY rowid DESC LIMIT 1',
                        (turn['id'],turn['persona_id'],LOCAL_USER_ID)).fetchone()
    context_sources=set((obj(consumed[0]).get('knowledge') or {}).get('source_ids',[])) if consumed else set()
    reuse=(info.get('used_previous') is True and info.get('needed') is False
           and bool(source_ids & prior_sources & context_sources)
           and not any(c['stage'] in ('native','search','fetch') for c in extra))
    if acquired:
        prior_sources.update(earned)
    return ordered_search,acquired,reuse


def read_turn(db, turn, paints, prior_sources, *, displayed_body=None):
    tid=turn['id']; pid=turn['persona_id']; sid=turn['session_id']
    members=load_turn_input(db,turn)
    reply=db.execute("""SELECT id,text FROM events WHERE id=? AND persona_id=? AND session_id=? AND role='persona'""",
                     (turn['reply_event_id'],pid,sid)).fetchone()
    msg=reply['text'] if reply else None
    root=obj(turn['model_snapshot'])
    batch=db.execute('SELECT * FROM reply_batches WHERE turn_id=? AND persona_id=? AND user_id=?',(tid,pid,LOCAL_USER_ID)).fetchall()
    batch=batch[0] if len(batch)==1 else None
    inputs=db.execute('SELECT first_epoch FROM input_batches WHERE id=? AND session_id=? AND persona_id=?',
                      (turn['input_batch_id'],sid,pid)).fetchone()
    accepted=inputs[0] if inputs else None
    notice=get_control(db,'knowledge.search_notice.turn.'+tid)
    pre=db.execute("""SELECT text FROM events WHERE id=? AND persona_id=? AND session_id=? AND role='persona'""",
                   (notice.get('event_id'),pid,sid)).fetchone()
    pre_text=pre[0] if pre else None
    pre_at=checked_paint(paints,'notice',notice.get('notice_id'),pre_text,accepted)
    main_at=checked_paint(paints,'main',turn['reply_event_id'],msg if displayed_body is None else displayed_body,accepted)
    calls,main_verified=main_calls(db,tid,digest(msg) if msg else None,batch['attempt_id'] if batch else None)
    extra,web=web_calls(db,turn); calls.extend(extra)
    retrieval=get_control(db,'knowledge.conversation.record.'+tid)
    same=(retrieval.get('root_turn_id'),retrieval.get('session_id'),retrieval.get('persona_id'))==(tid,sid,pid)
    info=retrieval.get('retrieval',{}) if same else {}
    notice_call=bool(pre_text) and any(c['stage']=='notice' and c.get('valid_notice')
                    and c.get('task_id')==notice.get('task_id') and c['response_sha256']==digest(pre_text) for c in extra)
    ordered_search,usable_web,reuse=web_provenance(db,turn,retrieval if same else {},prior_sources,web,extra,
                                                  pre_at if notice_call and notice.get('fully_presented') is True else None)
    from persona_runtime.dialogue_generation import enabled as direct_enabled
    from persona_runtime.expression_runtime import enabled as rewrite_enabled
    stages=db.execute("""SELECT 1 FROM dialogue_generation_stages s JOIN dialogue_generation_units u
                         ON u.id=s.unit_id WHERE u.turn_id=? LIMIT 1""",(tid,)).fetchone()
    legacy=db.execute('SELECT 1 FROM expression_runs WHERE turn_id=? UNION ALL SELECT 1 FROM expression_main_receipts WHERE turn_id=? LIMIT 1',
                      (tid,tid)).fetchone()
    segments=[r[0] for r in db.execute('SELECT text FROM reply_segments WHERE batch_id=? ORDER BY segment_index',(batch['id'],))] if batch else []
    baseline=bool(batch and official(root,'deepseek-v4-pro') and root.get('_companion_profile',{}).get('version')=='companion-runtime-v8'
                  and turn['reply_mode_snapshot']==batch['mode']=='context_full' and segments==[msg]
                  and msg and batch['raw_fingerprint']==digest(msg) and not stages and not legacy
                  and not direct_enabled(db,tid) and not rewrite_enabled(db,tid))
    masked=[]
    for event in [*(m['id'] for m in members),turn['reply_event_id'],notice.get('event_id')]:
        if event and db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(event,)).fetchone():
            masked.append(event)
    public_events=[get_control(db,'knowledge.native.public_event.'+m['id']) for m in members]
    explicit_public_tasks=[r['task'] for r in public_events if r.get('persona_id')==pid and r.get('session_id')==sid
                           and r.get('channel')=='desktop' and r.get('event_id') in {m['id'] for m in members}]
    if masked:
        members=[]; msg=pre_text=None; explicit_public_tasks=[]

    issues=[]
    if turn['status']!='completed':issues.append('本轮尚未完成')
    if not baseline:issues.append('缺少原 v8 Pro 整泡证据')
    if not main_verified:issues.append('缺少与正文匹配的 Pro 网络回执')
    if not any(c['stage']=='jev' and c['state']=='completed' and c['remote_http'] and c['response_sha256'] for c in extra):
        issues.append('缺少 Jev 网络判断回执')
    if main_at is None:issues.append('缺少主回复首次显示回执')
    if web and not ordered_search:issues.append('检索前的可见预告未核实')
    if notice and (pre_at is None or not notice_call):issues.append('查前发言的模型或显示回执未核实')
    if any(c['state'] in ('running','interrupted','unknown') for c in calls):issues.append('有在途或未核清的模型尝试')
    if masked:issues.append('内容已撤回')
    first=min((x for x in (pre_at,main_at) if x is not None),default=None)
    waits=[c['elapsed_ms'] for c in calls if c['stage']=='main']
    return {'turn_id':tid,'state':turn['status'],'user':[m['text'] for m in members],'notice':pre_text,'body':msg,
            'accepted_epoch':accepted,'notice_visible_epoch':pre_at,'main_visible_epoch':main_at,
            'first_visible_s':elapsed(accepted,first),'main_visible_s':elapsed(accepted,main_at),
            'main_provider_ms':round(sum(waits),3) if waits and all(v is not None for v in waits) else None,
            'baseline':baseline,'explicit_public_tasks':explicit_public_tasks,'main_remote_receipt':main_verified,'real_native':ordered_search,'material_acquired':usable_web,
            'reused_without_search':reuse,'retrieval_outcome':info.get('search_outcome'),
            'retrieval_errors':info.get('errors',[]),'used_previous':info.get('used_previous'),'jev_need_web':(info.get('web_decision') or {}).get('need_web'),'issues':issues,'calls':calls}


def summarize(turns):
    calls = [c for turn in turns for c in turn['calls']]
    routes = {}
    for stage in STAGE_NAMES:
        selected=[c for c in calls if c['stage']==stage]
        if not selected:
            continue
        known=[c['tokens'] for c in selected if c['tokens'] is not None]
        routes[stage]={'attempt_entries':len(selected),'remote_successes':sum(c['remote_http'] for c in selected),
                       'known_tokens':sum(known),'unknown_usage_entries':len(selected)-len(known)}
    tools=[c for c in calls if c['stage'] in ('native','search','fetch')]
    return {'turns':len(turns),'eligible_turns':sum(not row['issues'] for row in turns),
            'web_turns':sum(row['real_native'] for row in turns),'reuse_turns':sum(row['reused_without_search'] for row in turns),
            'no_web_turns':sum(row['jev_need_web'] is False and not row['issues'] for row in turns),
            'routes':routes,'model_reference_peak_cny_known':str(sum((Decimal(c['reference_peak_model_cny']) for c in calls
                if c['reference_peak_model_cny'] is not None),Decimal(0))),
            'model_entries_unpriced':sum(c['reference_peak_model_cny'] is None for c in calls if c['stage'] not in ('search','fetch')),
            'price_reference':PRICE_VERSION,'tool_estimated_cny_known':str(sum((Decimal(str(c['estimated_tool_cny']))
                for c in tools if c['estimated_tool_cny'] is not None),Decimal(0))),
            'unknown_tool_cost_entries':sum(c['estimated_tool_cny'] is None for c in tools),'actual_cny':None}


def collect(home):
    from persona_runtime.en5_display import read_scope,read_receipts
    from persona_runtime.private_personas import safe
    paths=RuntimePaths(Path(home))
    scope=read_scope(paths)
    require(scope and scope.get('purpose')=='desktop-experience','missing_scope')
    binding={k:scope[k] for k in ('study_id','instance_id','persona_id','sessions','mode','baseline','purpose')}
    target=safe(paths.directories()['database']/'runtime.sqlite3',paths.home)
    inputs=None
    providers={}
    root=paths.home.parent
    proof=root/'author-use.private.json'
    material=root/'materials.private.json'
    binding['input_approval_verified']=False
    supported=binding['mode'] in ('author_material_replay','author_desktop')
    if supported and proof.is_file():
        from .en5_experience_setup import validate_use
        material_hash=None
        if binding['mode']=='author_material_replay':
            raw=material.read_bytes()
            material_hash=sha256(raw).hexdigest()
            document=json.loads(raw)
            require(document['version']=='en5-original-inputs-v1' and
                    [s['part'] for s in document['sessions']]==[1,2] and
                    all([t['n'] for t in s['turns']]==list(range(1,TARGET+1)) for s in document['sessions']),
                    'material_positions')
            inputs=document['sessions']
            binding['material_sha256']=material_hash
        active=(root/'activation.private.json').is_file()
        validate_use(root,material_sha256=material_hash,require_live=active)
        approved=json.loads(proof.read_bytes())
        providers={role:fingerprint({k:m[k] for k in ('base_url','name')}) for role,m in approved['providers'].items()}
        binding.update(input_approval_verified=True,activation_verified=active,author_use_sha256=sha256(proof.read_bytes()).hexdigest())
    segments=[]
    with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        for number,sid in enumerate(scope['sessions'],1):
            owner=db.execute('SELECT persona_id,user_id FROM sessions WHERE id=?',(sid,)).fetchone()
            require(owner and tuple(owner)==(scope['persona_id'],LOCAL_USER_ID),'wrong_session_owner')
            paints=read_receipts(paths,scope['study_id'],scope['persona_id'],sid)
            turns=[]; prior_sources=set()
            for row in db.execute("""SELECT * FROM turns WHERE session_id=? AND persona_id=?
                                     AND channel='desktop' ORDER BY rowid LIMIT ?""",(sid,scope['persona_id'],TARGET)):
                turn=read_turn(db,dict(row),paints.get(row['id'],{}),prior_sources)
                roles={'main':'main','notice':'main','jev':'knowledge_router','native':'knowledge_native'}
                if providers and any(c['provider_identity']!=providers[roles[c['stage']]]
                                     for c in turn['calls'] if c['stage'] in roles):
                    turn['issues'].append('本轮用途与已批准的端点或模型不符')
                if inputs:
                    expected=inputs[number-1]['turns'][len(turns)]
                    if [b['text'] for b in expected['bubbles']] != turn['user']:
                        turn['issues'].append('输入与已审原始气泡不符')
                    from persona_runtime.knowledge_native import public_task
                    approved=[public_task(expected['public_task'])] if expected.get('public_task') else []
                    if approved!=turn['explicit_public_tasks']:
                        turn['issues'].append('公开检索任务与已审材料不符')
                turns.append(turn)
            count=db.execute("SELECT count(*) FROM turns WHERE session_id=? AND persona_id=? AND channel='desktop'",
                             (sid,scope['persona_id'])).fetchone()[0]
            segments.append({'number':number,'session_id':sid,'turns':turns,'summary':summarize(turns),'extra_turns':max(0,count-TARGET)})
    return make_report(binding,segments)


def make_report(binding, segments):
    summary=summarize([r for s in segments for r in s['turns']])
    gaps=[]
    if binding['mode'] in ('author_material_replay','author_desktop'):
        if not binding.get('input_approval_verified'):
            gaps.append('本次体验的输入方式及云端用途尚待核对')
        elif not binding.get('activation_verified'):
            gaps.append('本次回放授权尚未激活')
    if binding['mode'] not in ('author_desktop','author_material_replay'):
        gaps.append('本批记录为本地验证模式')
    for seg in segments:
        totals=seg['summary']
        if totals['eligible_turns'] != TARGET:
            gaps.append(f"第{seg['number']}段已核实 {totals['eligible_turns']}/{TARGET} 轮")
    if summary['web_turns'] < 2:
        gaps.append(f"真实联网及先显示的查前发言已核实 {summary['web_turns']}/2 轮")
    if not summary['reuse_turns']:
        gaps.append('尚缺同段追问直接复用此前真实检索资料的回执')
    if not summary['no_web_turns']:
        gaps.append('尚缺 Jev 判定无须联网的已显示回合')
    result={'version':'en5-experience-report-v1','binding':binding,'segments':segments,
            'summary':summary,'engineering_ready':not gaps,'gaps':gaps}
    result['evidence_sha256']=fingerprint(result)
    return result


def assess(report, review):
    for field in ('study_id','instance_id','persona_id'):
        require(review.get(field)==report['binding'][field],'feedback_scope')
    require(review.get('version')=='en5-author-review-v1' and review.get('evidence_sha256')==report['evidence_sha256'],
            'feedback_version')
    require(isinstance(review.get('note',''),str),'feedback_note')
    require(isinstance(review.get('segments'),list) and len(review['segments'])==2,'feedback_segments')
    verdicts={'accept','revise','undecided'}
    all_accept=True
    for seg,row in zip(report['segments'],review['segments'],strict=True):
        require(row.get('session_id')==seg['session_id'] and all(row.get(k) in verdicts for k in ('naturalness','wait')),
                'feedback_segment_scope')
        all_accept &= all(row[k]=='accept' for k in ('naturalness','wait'))
    require(review.get('cost') in verdicts and review.get('overall') in verdicts,'feedback_verdict')
    coverage=review.get('coverage')
    require(isinstance(coverage,dict) and set(coverage)==set(COVERAGE),'feedback_coverage')
    refs={f"{s['number']}/{i}" for s in report['segments'] for i,r in enumerate(s['turns'],1) if not r['issues']}
    require(all(isinstance(v,str) and (v=='' or v in refs) for v in coverage.values()),'feedback_unverified_turn')
    all_accept &= review['cost']==review['overall']=='accept' and all(coverage.values())
    return bool(report['engineering_ready'] and all_accept)


def html_page(report, review):
    segs=report['segments']; summary=report['summary']
    status=('已通过' if review and assess(report,review) else '已记录作者意见' if review else
            '工程证据齐备，等作者审阅' if report['engineering_ready'] else '体验进行中')
    html=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
          '<title>角色 · E.N5 连续桌面体验</title><style>:root{color-scheme:light dark}body{max-width:58rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui}h1,h2{line-height:1.3}section{border-top:1px solid #828c83;margin:1rem 0}summary,button,select{cursor:pointer}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}dt{font-weight:bold}dd{margin:0 0 .6rem}td,th{padding:.4rem;text-align:left}button,textarea,select{font:inherit}label{display:inline-block;margin:.3rem .6rem .3rem 0}textarea{width:90%}</style><main><h1>连续桌面体验</h1><p>',
          escape(status),f"。已核实 {summary['eligible_turns']}/30 轮，真实联网 {summary['web_turns']} 轮，复用此前资料 {summary['reuse_turns']} 轮。</p>"]
    if report['binding']['mode']=='author_material_replay':
        html.append('<p>输入方式为已审真人原文回放。时标来自 Tk 窗口，主观体验由作者在审阅时单独填写。</p>')
    if report['gaps']:
        html.append('<ul>'+''.join('<li>'+escape(x)+'</li>' for x in report['gaps'])+'</ul>')
    html.append('<p>计时从各轮第一条消息受理开始。模型耗时取主回复请求回执；首次可见时间取桌面控件实际显示记录。未取得的时间和金额显示为未知。</p>')
    def wait(v):return '未知' if v is None else f'{v:.2f} 秒'
    for seg in segs:
        s=seg['summary'];html.append(f"<section><h2>第{seg['number']}段</h2><p>已核实 {s['eligible_turns']}/15 轮；联网 {s['web_turns']} 轮，复用 {s['reuse_turns']} 轮。</p>")
        if seg['extra_turns']:
            html.append(f"<p>该会话另有 {seg['extra_turns']} 轮在预定15个位置之后，未纳入本段。</p>")
        for i,t in enumerate(seg['turns'],1):
            notes='；'.join(t['issues']) if t['issues'] else '已核实'
            html.append(f"<details><summary>第{i}轮 · {escape(notes)} · 首次可见 {wait(t['first_visible_s'])}</summary><dl>")
            for label,body in [('用户',t['user']),('查前发言',[t['notice']]),('主回复',[t['body']])]:
                for part in body:
                    if part:
                        html.append('<dt>'+label+'</dt><dd><pre>'+escape(part)+'</pre></dd>')
            model_s=t['main_provider_ms']/1000 if t['main_provider_ms'] is not None else None
            html.append('<dt>等待</dt><dd>主回复模型 '+wait(model_s)+'，主回复可见 '+wait(t['main_visible_s'])+'</dd>')
            if t.get('retrieval_outcome'):
                html.append('<dt>资料状态</dt><dd>'+escape(t['retrieval_outcome'])+
                            ('，沿用本段此前的检索资料' if t['reused_without_search'] else '')+'</dd>')
            for c in t['calls']:
                usage=str(c['tokens'])+' tokens' if c['tokens'] is not None else '用量未知'
                html.append('<dt>'+escape(STAGE_NAMES[c['stage']])+'</dt><dd>'+escape(str(c['model'] or '未知'))+
                            ' · '+escape(c['state'])+' · '+usage+'</dd>')
            html.append('</dl></details>')
        html.append('</section>')
    html.append('<h2>账目</h2><table><tr><th>用途</th><th>尝试记录</th><th>已知 tokens</th><th>用量未知笔数</th></tr>')
    for name,count in summary['routes'].items():
        html.append(f"<tr><td>{STAGE_NAMES[name]}</td><td>{count['attempt_entries']}</td><td>{count['known_tokens']}</td><td>{count['unknown_usage_entries']}</td></tr>")
    html.append(f"</table><p>按研究参考峰段价格计算，已知模型费用小计 ¥{escape(summary['model_reference_peak_cny_known'])}，另有 {summary['model_entries_unpriced']} 笔模型尝试未计价。工具费已知估算小计 ¥{escape(summary['tool_estimated_cny_known'])}，另有 {summary['unknown_tool_cost_entries']} 笔工具费用量未知。实际账单扣费未知。</p>")
    html.append('<h2>作者审阅</h2><form id="review">')
    def field(name,title,options):
        return '<label>'+escape(title)+' <select id="'+name+'">'+''.join(
            '<option value="'+escape(k)+'">'+escape(v)+'</option>' for k,v in options)+'</select></label>'
    opts=[('undecided','尚未判断'),('accept','接受'),('revise','需要改进')]
    for s in segs:
        n=s['number'];html.append('<p>'+field(f'n{n}',f'第{n}段自然度',opts)+field(f'w{n}',f'第{n}段等待',opts)+'</p>')
    html.append(field('cost','费用接受度',opts)+field('overall','整体体验',opts)+'<p>请选择能体现以下场景的实际轮次。</p>')
    choices=[('','待核对')]+[(f"{s['number']}/{i}",f"第{s['number']}段 · 第{i}轮") for s in segs
                             for i,t in enumerate(s['turns'],1) if not t['issues']]
    html.extend(field(k,v,choices) for k,v in COVERAGE.items())
    html.append('<p><label>备注<br><textarea id="note"></textarea></label></p><button type="button" id="out">导出作者意见</button></form></main>')
    scope=report['binding']
    identity={k:scope[k] for k in ('study_id','instance_id','persona_id','sessions')}
    identity.update(version='en5-author-review-v1',evidence_sha256=report['evidence_sha256'])
    html.append('<script type="application/json" id="identity">'+json.dumps(identity,ensure_ascii=False).replace('<',r'\u003c')+'</script>')
    html.append("""<script>const id=JSON.parse(document.getElementById('identity').textContent),val=k=>document.getElementById(k).value;
document.getElementById('out').onclick=()=>{const {sessions,...context}=id;
const data={...context,segments:sessions.map((session_id,i)=>({session_id,naturalness:val('n'+(i+1)),wait:val('w'+(i+1))})),
cost:val('cost'),overall:val('overall'),coverage:{topic_switch:val('topic_switch'),serious_task:val('serious_task'),followup:val('followup')},note:val('note')};
const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');
a.href=url;a.download='E.N5-角色-连续桌面体验审阅意见.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);};</script></html>""")
    return ''.join(html)


def export(home, review_path=None):
    from persona_runtime.private_personas import safe
    home=Path(home)
    report=collect(home)
    folder=safe(home/'exports/en5',home)
    folder.mkdir(parents=True,exist_ok=True)
    opinion=safe(folder/'author-feedback'/report['evidence_sha256'],home)
    review=None
    if review_path is not None:
        raw=Path(review_path).read_bytes()
        review=json.loads(raw)
        assess(report,review)
        opinion.mkdir(parents=True,exist_ok=True)
        original=opinion/(sha256(raw).hexdigest()+'.json')
        try:
            with original.open('xb') as file:file.write(raw)
        except FileExistsError:
            require(original.read_bytes()==raw,'feedback_archive_conflict')
        atomic_json(opinion/'current.json',review)
    elif (opinion/'current.json').is_file():
        review=json.loads((opinion/'current.json').read_text(encoding='utf8'))
        assess(report,review)
    report['author_review']=review
    report['stage_passed']=bool(review and assess(report,review))
    atomic_json(folder/'report.private.json',report)
    fd,path=tempfile.mkstemp(dir=folder,suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as file:file.write(html_page(report,review))
        os.replace(path,folder/'index.html')
    finally:
        if os.path.exists(path):os.unlink(path)
    return folder/'index.html'


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--home',type=Path,required=True)
    cli.add_argument('--review',type=Path)
    args=cli.parse_args()
    print(export(args.home,args.review))
